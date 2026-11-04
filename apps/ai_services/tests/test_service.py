"""
The AI service: subscription checks, mode and model routing, rotation across
the platform's OpenAI keys, permitted fallbacks, quota reservation and
reconciliation, cost recording, and errors. Every OpenAI call goes to the
FakeProvider from conftest.py.
"""

import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import serializers

from apps.ai_services import providers, service
from apps.ai_services.errors import (
    AICredentialsRejected,
    AIDeclined,
    AIInvalidResponse,
    AIModelUnavailable,
    AIModeUnavailable,
    AINotConfigured,
    AIProviderUnavailable,
    AIRateLimited,
    AIRequestRejected,
)
from apps.ai_services.models import AIMode, AIPlatformSettings, PlatformAPIKey, ProviderCallLog
from apps.ai_services.service import AIRequestOptions, run_json_task
from apps.usage import errors as usage_errors
from apps.usage.models import UsageEvent, UsageReservation
from conftest import LARGE_MODEL, PLATFORM_KEY, SECOND_PLATFORM_KEY, SMALL_MODEL

pytestmark = pytest.mark.django_db

SCHEMA = {"type": "object"}
PRIVATE_PROMPT = "private resume text: Jane Doe, 555-0100"


class OutputSerializer(serializers.Serializer):
    answer = serializers.CharField(max_length=50)


def run(user, options=None, **overrides):
    kwargs = {
        "feature": "test_feature",
        "system": "system",
        "prompt": PRIVATE_PROMPT,
        "schema": SCHEMA,
        "max_output_tokens": 1000,
        "serializer_class": OutputSerializer,
        "options": options,
    }
    kwargs.update(overrides)
    return run_json_task(user, **kwargs)


def reservations(**filters):
    return list(UsageReservation.objects.filter(**filters).values_list("status", flat=True))


def used_keys(fake_ai):
    return [call["api_key"] for call in fake_ai.calls]


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_ai_requires_an_active_plan(user, ai_config, fake_ai):
    with pytest.raises(usage_errors.SubscriptionRequired):
        run(user)
    assert fake_ai.calls == []


def test_request_uses_the_platform_key_and_is_charged_to_the_subscription(
    user, subscribe, ai_config, fake_ai
):
    subscription = subscribe(user)
    fake_ai.respond({"answer": "ok"}, input_tokens=1200, output_tokens=800)

    result = run(user)

    call = fake_ai.calls[0]
    assert (call["provider"], call["model"], call["api_key"]) == (
        "openai",
        LARGE_MODEL,
        PLATFORM_KEY,
    )
    assert result.data == {"answer": "ok"}
    assert (result.source, result.ai_mode, result.used_fallback) == ("platform", "fast", False)
    event = UsageEvent.objects.get()
    assert (event.source, event.subscription, event.kind) == ("platform", subscription, "ai_tokens")
    assert (event.input_tokens, event.output_tokens, event.quantity) == (1200, 800, 2000)
    # 1200 * 5 / 1M + 800 * 25 / 1M, from the administrator-entered prices.
    assert event.estimated_cost == Decimal("0.026000")
    assert event.cost_currency == "USD"
    assert (event.provider, event.model, event.feature) == ("openai", LARGE_MODEL, "test_feature")
    assert reservations() == ["committed"]
    key = PlatformAPIKey.objects.get()
    assert (key.request_count, key.last_used_at is not None) == (1, True)
    assert ProviderCallLog.objects.get().api_key == key


def test_ai_is_not_configured_without_a_usable_key(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    PlatformAPIKey.objects.update(is_enabled=False)

    with pytest.raises(AINotConfigured):
        run(user)
    assert fake_ai.calls == []


def test_no_spending_cap_means_no_platform_budget_limit(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    AIPlatformSettings.objects.update(monthly_spend_cap=None)
    fake_ai.respond({"answer": "ok"})

    assert run(user).data == {"answer": "ok"}


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


def test_rate_limited_key_cools_down_and_the_next_key_is_used(
    user, subscribe, ai_config, platform_key, fake_ai
):
    subscribe(user)
    second = platform_key(SECOND_PLATFORM_KEY)
    fake_ai.fail(AIRateLimited())
    fake_ai.respond({"answer": "ok"})

    result = run(user)

    assert used_keys(fake_ai) == [PLATFORM_KEY, SECOND_PLATFORM_KEY]
    assert result.used_fallback is False
    first = ai_config["key"]
    first.refresh_from_db()
    assert first.cooling_until(LARGE_MODEL) is not None
    assert first.cooldowns[LARGE_MODEL]["hits"] == 1
    assert first.last_error == "ai_rate_limited"
    second.refresh_from_db()
    assert second.request_count == 1
    assert reservations() == ["committed"]

    # While the first key cools down, requests go straight to the second.
    fake_ai.respond({"answer": "ok"})
    run(user)
    assert used_keys(fake_ai)[-1] == SECOND_PLATFORM_KEY


def test_cooldowns_are_per_model(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    key = ai_config["key"]
    key.cooldowns = {SMALL_MODEL: key.next_cooldown(SMALL_MODEL)}
    key.save()
    fake_ai.respond({"answer": "ok"})

    run(user)  # the large model isn't affected

    assert used_keys(fake_ai) == [PLATFORM_KEY]


def test_repeated_limits_back_off_exponentially(ai_config):
    key = ai_config["key"]
    now = timezone.now()
    first = key.next_cooldown(LARGE_MODEL, now)
    key.cooldowns = {LARGE_MODEL: first}
    second = key.next_cooldown(LARGE_MODEL, now)
    assert (first["hits"], second["hits"]) == (1, 2)
    assert key.cooling_until(LARGE_MODEL, now) == now + timedelta(seconds=60)
    key.cooldowns = {LARGE_MODEL: {**second, "hits": 30}}
    capped = key.next_cooldown(LARGE_MODEL, now)
    assert capped["until"] == (now + timedelta(seconds=3600)).isoformat()


def test_success_clears_the_models_cooldown(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    key = ai_config["key"]
    past = timezone.now() - timedelta(minutes=5)
    key.cooldowns = {LARGE_MODEL: {"until": past.isoformat(), "hits": 3}}
    key.save()
    fake_ai.respond({"answer": "ok"})

    run(user)

    key.refresh_from_db()
    assert LARGE_MODEL not in key.cooldowns


def test_every_key_limited_is_reported_as_busy(user, subscribe, ai_config, platform_key, fake_ai):
    subscribe(user)
    platform_key(SECOND_PLATFORM_KEY)
    fake_ai.fail(AIRateLimited())
    fake_ai.fail(AIRateLimited())

    with pytest.raises(AIRateLimited):
        run(user)
    assert used_keys(fake_ai) == [PLATFORM_KEY, SECOND_PLATFORM_KEY]
    assert reservations() == ["released"]

    # Both keys are now cooling down: OpenAI isn't called at all.
    with pytest.raises(AIRateLimited):
        run(user)
    assert len(fake_ai.calls) == 2


def test_rejected_key_is_set_aside_and_the_next_key_is_used(
    user, subscribe, ai_config, platform_key, fake_ai
):
    subscribe(user)
    platform_key(SECOND_PLATFORM_KEY)
    fake_ai.fail(AICredentialsRejected())
    fake_ai.respond({"answer": "ok"})

    run(user)

    assert used_keys(fake_ai) == [PLATFORM_KEY, SECOND_PLATFORM_KEY]
    first = ai_config["key"]
    first.refresh_from_db()
    assert (first.status, first.last_error) == ("rejected", "ai_key_rejected")
    assert [key.pk for key in service.active_keys()] != [first.pk]


def test_all_keys_rejected_means_not_configured(user, subscribe, ai_config, fake_ai, caplog):
    subscribe(user)
    fake_ai.fail(AICredentialsRejected())

    with pytest.raises(AINotConfigured):
        run(user)
    assert "every key was rejected" in caplog.text
    assert reservations() == ["released"]
    assert not service.has_usable_keys()


def test_keys_are_tried_in_priority_order(user, subscribe, ai_config, platform_key, fake_ai):
    subscribe(user)
    platform_key(SECOND_PLATFORM_KEY, priority=0)
    fake_ai.respond({"answer": "ok"})

    run(user)

    assert used_keys(fake_ai) == [SECOND_PLATFORM_KEY]


def test_disabled_and_unreadable_keys_are_skipped(
    user, subscribe, ai_config, platform_key, fake_ai
):
    subscribe(user)
    PlatformAPIKey.objects.filter(pk=ai_config["key"].pk).update(encrypted_key="v1.gone.a.b")
    platform_key("sk-test-disabled-key-not-real-00000000", is_enabled=False)
    platform_key(SECOND_PLATFORM_KEY)
    fake_ai.respond({"answer": "ok"})

    run(user)

    assert used_keys(fake_ai) == [SECOND_PLATFORM_KEY]
    assert PlatformAPIKey.objects.get(pk=ai_config["key"].pk).status == "rejected"


@pytest.mark.parametrize("error", [AIProviderUnavailable(), AIModelUnavailable()])
def test_outages_do_not_rotate_keys(user, subscribe, ai_config, platform_key, fake_ai, error):
    subscribe(user)
    platform_key(SECOND_PLATFORM_KEY)
    fake_ai.fail(error)

    with pytest.raises(type(error)):
        run(user)
    assert used_keys(fake_ai) == [PLATFORM_KEY]
    assert reservations() == ["released"]


def test_rejected_request_is_not_blamed_on_the_user(user, subscribe, ai_config, fake_ai, caplog):
    subscribe(user)
    fake_ai.fail(AIRequestRejected())

    with pytest.raises(AIProviderUnavailable):
        run(user)
    assert "OpenAI rejected the request" in caplog.text
    assert reservations() == ["released"]


def test_unexpected_errors_release_the_reservation(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    fake_ai.fail(RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        run(user)
    assert reservations() == ["released"]


# ---------------------------------------------------------------------------
# AI modes
# ---------------------------------------------------------------------------


def test_fast_is_the_default_mode_and_disabled_modes_are_refused(
    user, subscribe, ai_config, fake_ai
):
    subscribe(user)

    with pytest.raises(AIModeUnavailable):
        run(user, AIRequestOptions(ai_mode="advanced"))
    assert fake_ai.calls == []


def test_each_mode_maps_to_its_configured_model(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    AIMode.objects.filter(key="balanced").update(is_enabled=True, model=ai_config["small"])
    fake_ai.respond({"answer": "ok"})

    result = run(user, AIRequestOptions(ai_mode="balanced"))

    assert fake_ai.calls[0]["model"] == SMALL_MODEL
    assert result.ai_mode == "balanced"


def test_saved_preference_is_used_when_the_request_has_no_choice(
    user, subscribe, ai_config, fake_ai
):
    from apps.ai_services.models import UserAIPreference

    subscribe(user)
    AIMode.objects.filter(key="balanced").update(is_enabled=True, model=ai_config["small"])
    UserAIPreference.objects.create(user=user, ai_mode="balanced")
    fake_ai.respond({"answer": "ok"})

    assert run(user).ai_mode == "balanced"


def test_output_limit_is_clamped_to_the_models_maximum(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    ai_config["large"].max_output_tokens = 600
    ai_config["large"].save()
    fake_ai.respond({"answer": "ok"})

    run(user, max_output_tokens=5000)

    assert fake_ai.calls[0]["max_output_tokens"] == 600


def test_oversized_input_is_refused_before_calling_openai(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    ai_config["large"].max_input_tokens = 10
    ai_config["large"].save()

    with pytest.raises(usage_errors.RequestTooLarge):
        run(user)
    assert fake_ai.calls == []
    assert not UsageReservation.objects.exists()


# ---------------------------------------------------------------------------
# Fallback to a model that costs no more
# ---------------------------------------------------------------------------


def enable_fallback(ai_config):
    ai_config["fast"].allow_fallback = True
    ai_config["fast"].save()


def test_no_fallback_unless_the_mode_allows_it(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    fake_ai.fail(AIProviderUnavailable())

    with pytest.raises(AIProviderUnavailable):
        run(user)
    assert [call["model"] for call in fake_ai.calls] == [LARGE_MODEL]


def test_permitted_fallback_is_used_after_a_transient_failure(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    enable_fallback(ai_config)
    fake_ai.fail(AIProviderUnavailable())
    fake_ai.respond({"answer": "ok"}, input_tokens=100, output_tokens=100)

    result = run(user)

    assert [call["model"] for call in fake_ai.calls] == [LARGE_MODEL, SMALL_MODEL]
    assert result.used_fallback is True
    assert result.model == SMALL_MODEL
    event = UsageEvent.objects.get()
    assert event.model == SMALL_MODEL
    assert event.estimated_cost == Decimal("0.000600")  # small model's prices
    assert sorted(reservations()) == ["committed", "released"]


def test_fallback_model_is_used_when_every_key_is_limited_for_the_primary(
    user, subscribe, ai_config, fake_ai
):
    subscribe(user)
    enable_fallback(ai_config)
    fake_ai.fail(AIRateLimited())
    fake_ai.respond({"answer": "ok"})

    result = run(user)

    # Rate limits are per model, so the same key works for the fallback.
    assert [(c["model"], c["api_key"]) for c in fake_ai.calls] == [
        (LARGE_MODEL, PLATFORM_KEY),
        (SMALL_MODEL, PLATFORM_KEY),
    ]
    assert result.used_fallback is True


@pytest.mark.parametrize(
    "change",
    [
        {"is_enabled": False},
        # Costs more than the primary model.
        {"input_price_per_million": Decimal("6"), "output_price_per_million": Decimal("25")},
    ],
)
def test_fallback_is_skipped_when_not_permitted(user, subscribe, ai_config, fake_ai, change):
    subscribe(user)
    enable_fallback(ai_config)
    for field, value in change.items():
        setattr(ai_config["small"], field, value)
    ai_config["small"].save()
    fake_ai.fail(AIProviderUnavailable())

    with pytest.raises(AIProviderUnavailable):
        run(user)
    assert len(fake_ai.calls) == 1


def test_free_models_may_fall_back_to_each_other(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    enable_fallback(ai_config)
    for model in (ai_config["large"], ai_config["small"]):
        model.input_price_per_million = model.output_price_per_million = Decimal("0")
        model.save()
    fake_ai.fail(AIProviderUnavailable())
    fake_ai.respond({"answer": "ok"})

    assert run(user).model == SMALL_MODEL


def test_fallback_must_fit_its_own_limits(user, subscribe, ai_config, fake_ai):
    """A request too large for the primary falls back only if the fallback accepts it."""
    subscribe(user)
    enable_fallback(ai_config)
    ai_config["large"].max_input_tokens = 10
    ai_config["large"].save()
    ai_config["small"].max_input_tokens = 10
    ai_config["small"].save()

    with pytest.raises(usage_errors.RequestTooLarge):
        run(user)
    assert fake_ai.calls == []


def test_daily_token_limit_falls_back_to_a_smaller_reservation(user, subscribe, ai_config, fake_ai):
    # The large model reserves up to 20,000 output tokens, the small one 8,000.
    subscribe(user, daily_ai_token_limit=12_000)
    fake_ai.respond({"answer": "ok"})

    with pytest.raises(usage_errors.DailyTokenLimitReached) as info:
        run(user, max_output_tokens=20_000)
    assert info.value.error_extra["limit"]["scope"] == "daily"
    assert fake_ai.calls == []

    enable_fallback(ai_config)
    result = run(user, max_output_tokens=20_000)
    assert result.model == SMALL_MODEL
    assert fake_ai.calls[0]["max_output_tokens"] == 8_000


def test_budget_exhaustion_blocks_ai_unless_a_cheaper_fallback_fits(
    user, subscribe, ai_config, fake_ai
):
    subscribe(user)
    # Worst case on the large model: ~0.50 USD; on the small model: ~0.04 USD.
    AIPlatformSettings.objects.update(monthly_spend_cap=Decimal("0.20"))
    fake_ai.respond({"answer": "ok"})

    with pytest.raises(usage_errors.PlatformBudgetExhausted):
        run(user, max_output_tokens=20_000)
    assert fake_ai.calls == []

    enable_fallback(ai_config)
    assert run(user, max_output_tokens=20_000).model == SMALL_MODEL


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def test_refusal_is_reported_and_tokens_still_recorded(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    fake_ai.respond("", finish=providers.FINISH_REFUSAL, input_tokens=50, output_tokens=5)

    with pytest.raises(AIDeclined):
        run(user)
    assert UsageEvent.objects.get().quantity == 55


def test_truncated_response_is_rejected(user, subscribe, ai_config, fake_ai):
    subscribe(user)
    fake_ai.respond('{"answer": "o', finish=providers.FINISH_MAX_TOKENS)

    with pytest.raises(AIInvalidResponse):
        run(user)
    assert UsageEvent.objects.count() == 1


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"answer": ""}', '{"other": 1}'])
def test_unusable_responses_are_rejected_not_repaired(user, subscribe, ai_config, fake_ai, text):
    subscribe(user)
    fake_ai.respond(text)

    with pytest.raises(AIInvalidResponse):
        run(user)


def test_logs_contain_field_names_not_content(user, subscribe, ai_config, fake_ai, caplog):
    subscribe(user)
    caplog.set_level(logging.DEBUG)
    fake_ai.respond({"answer": "Jane Doe " * 20})

    with pytest.raises(AIInvalidResponse):
        run(user)
    assert "answer" in caplog.text
    assert "Jane Doe" not in caplog.text
    assert "555-0100" not in caplog.text
    assert PLATFORM_KEY not in caplog.text


def test_errors_and_logs_never_include_the_key(user, subscribe, ai_config, fake_ai, caplog):
    subscribe(user)
    caplog.set_level(logging.DEBUG)
    fake_ai.fail(AIRateLimited())

    with pytest.raises(AIRateLimited) as info:
        run(user)
    assert PLATFORM_KEY not in str(info.value.detail)
    assert PLATFORM_KEY not in caplog.text
    assert PLATFORM_KEY[-4:] in caplog.text  # the hint identifies the key


def test_provider_is_built_with_the_configured_timeout(monkeypatch, settings):
    settings.AI_SETTINGS = {**settings.AI_SETTINGS, "REQUEST_TIMEOUT_SECONDS": 42}
    captured = {}

    def fake_build(provider, **kwargs):
        captured.update(kwargs, provider=provider)
        return object()

    monkeypatch.setattr(providers, "build_provider", fake_build)
    service.build_provider("openai", api_key="k", model="m")

    assert captured == {"provider": "openai", "api_key": "k", "model": "m", "timeout": 42}


# ---------------------------------------------------------------------------
# Model configuration validation
# ---------------------------------------------------------------------------


def test_admin_validation_requires_a_fallback_that_costs_no_more(ai_config):
    from django.core.exceptions import ValidationError

    small = ai_config["small"]
    small.fallback = ai_config["large"]  # dearer
    with pytest.raises(ValidationError) as info:
        small.full_clean()
    assert "fallback" in info.value.message_dict


def test_enabled_mode_needs_a_model(ai_config):
    from django.core.exceptions import ValidationError

    mode = AIMode.objects.get(key="balanced")
    mode.is_enabled = True
    with pytest.raises(ValidationError):
        mode.full_clean()
