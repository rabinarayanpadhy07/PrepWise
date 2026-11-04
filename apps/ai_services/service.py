"""
The shared AI service. Feature apps call `run_json_task()`.

All AI is platform AI on OpenAI, available to subscribers only (a paid
plan, or access granted by an administrator). For each request this module:
  1. Checks the user has a current subscription.
  2. Resolves the AI mode (the request's, then the user's preference, then Fast)
     to its administrator-configured OpenAI model.
  3. Builds the candidate chain: the model, then (if the mode allows it) each
     configured fallback that costs no more.
  4. For each candidate whose per-request limits fit: reserves the user's
     allowance and the monthly budget atomically, then calls OpenAI with the
     platform's API keys in priority order. A key at its rate limit for that
     model cools down and the next key is tried; a key OpenAI rejects is set
     aside for an administrator. Provider-reported usage is committed, or the
     reservation released if nothing was used. If every key is limited, or the
     model is unavailable, the next candidate model is tried.
  5. Parses and validates the JSON. Invalid output is an error, never repaired.

Callers may pass `basic`: a function returning built-in, rule-based output
(no AI). It is used for users without a plan, and for subscribers whenever the
AI request fails for any reason. Basic results have source "basic", record no
usage and are labelled as not AI-generated in the UI.
"""

import json
import logging
import math
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from apps.ai_services import providers
from apps.ai_services.crypto import decrypt_api_key
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
    AIServiceError,
    KeyEncryptionUnavailable,
    StoredKeyUnreadable,
)
from apps.ai_services.models import (
    AIMode,
    AIModelConfig,
    AIPlatformSettings,
    PlatformAPIKey,
    ProviderCallLog,
    UserAIPreference,
)
from apps.usage import errors as usage_errors
from apps.usage import services as usage

logger = logging.getLogger("prepwise.ai")

SOURCE_PLATFORM = "platform"
SOURCE_BASIC = "basic"

# Failures worth trying the next candidate model for.
FALLBACK_ERRORS = (AIProviderUnavailable, AIRateLimited, AIModelUnavailable)
# Platform limits a cheaper or smaller fallback might still fit within.
FALLBACK_LIMIT_ERRORS = (
    usage_errors.PlatformBudgetExhausted,
    usage_errors.TokenAllowanceExhausted,
    usage_errors.DailyTokenLimitReached,
    usage_errors.RequestTooLarge,
)
# Conservative prompt-size estimate: ~3 bytes per token overestimates for
# typical English text, so reservations err on the side of holding too much.
BYTES_PER_TOKEN = 3
TOKEN_OVERHEAD = 50
COST_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class AIRequestOptions:
    """The caller's explicit choices. None means "use the saved preference"."""

    ai_mode: str | None = None


@dataclass
class Route:
    mode: AIMode
    candidates: list


@dataclass(frozen=True)
class AITaskResult:
    data: dict
    source: str
    ai_mode: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost: Decimal | None
    used_fallback: bool

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    @property
    def is_basic(self):
        return self.source == SOURCE_BASIC


def basic_result(data):
    """An AITaskResult for built-in, rule-based output (no AI call, no usage)."""
    return AITaskResult(
        data=data,
        source=SOURCE_BASIC,
        ai_mode="",
        provider="",
        model="",
        input_tokens=0,
        output_tokens=0,
        estimated_cost=Decimal("0"),
        used_fallback=False,
    )


def build_provider(provider, *, api_key, model):
    """Single construction point for providers (tests replace this)."""
    return providers.build_provider(
        provider,
        api_key=api_key,
        model=model,
        timeout=settings.AI_SETTINGS["REQUEST_TIMEOUT_SECONDS"],
    )


def estimate_input_tokens(*texts):
    size = sum(len((text or "").encode("utf-8")) for text in texts)
    return math.ceil(size / BYTES_PER_TOKEN) + TOKEN_OVERHEAD


def estimate_cost(model, input_tokens, output_tokens):
    """Cost from administrator-entered prices (0 for free keys)."""
    cost = (
        Decimal(input_tokens) * model.input_price_per_million
        + Decimal(output_tokens) * model.output_price_per_million
    ) / Decimal(1_000_000)
    return cost.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def preference_for(user):
    preference = UserAIPreference.objects.filter(user=user).first()
    return preference or UserAIPreference(user=user)


# ---------------------------------------------------------------------------
# Platform API keys
# ---------------------------------------------------------------------------


def active_keys():
    """Enabled keys OpenAI hasn't rejected, in the order they're tried."""
    return PlatformAPIKey.objects.filter(
        is_enabled=True, status=PlatformAPIKey.Status.ACTIVE
    ).order_by("priority", "id")


def has_usable_keys():
    return active_keys().exists()


def _note_key_error(key, code, **changes):
    PlatformAPIKey.objects.filter(pk=key.pk).update(
        last_error=code, last_error_at=timezone.now(), **changes
    )


def _cool_down(key, model_id, code):
    cooldowns = dict(key.cooldowns or {})
    cooldowns[model_id] = key.next_cooldown(model_id)
    key.cooldowns = cooldowns
    _note_key_error(key, code, cooldowns=cooldowns)
    logger.warning(
        "OpenAI key ****%s is at its limit for %s; cooling down until %s",
        key.key_hint,
        model_id,
        cooldowns[model_id]["until"],
    )


def _reject_key(key, code):
    _note_key_error(key, code, status=PlatformAPIKey.Status.REJECTED)
    logger.error("OpenAI key ****%s was rejected (%s) and has been set aside", key.key_hint, code)


def _key_succeeded(key, model_id):
    changes = {"request_count": F("request_count") + 1, "last_used_at": timezone.now()}
    if model_id in (key.cooldowns or {}):
        cooldowns = dict(key.cooldowns)
        cooldowns.pop(model_id)
        changes["cooldowns"] = cooldowns
    PlatformAPIKey.objects.filter(pk=key.pk).update(**changes)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def resolve_mode(key):
    mode = (
        AIMode.objects.select_related("model__fallback")
        .filter(key=key or AIMode.DEFAULT, is_enabled=True)
        .first()
    )
    if mode is None:
        raise AIModeUnavailable()
    return mode


def _chain(model, mode):
    """The model, then its permitted fallbacks (each costing no more than the last)."""
    candidates, seen = [model], {model.pk}
    current = model
    while mode.allow_fallback and current.fallback_id and current.fallback_id not in seen:
        fallback = AIModelConfig.objects.filter(pk=current.fallback_id).first()
        if fallback is None or not (fallback.is_enabled and fallback.costs_no_more_than(current)):
            break
        candidates.append(fallback)
        seen.add(fallback.pk)
        current = fallback
    return candidates


def resolve_route(user, options=None):
    """The mode and candidate models for this user's request. Subscribers only."""
    options = options or AIRequestOptions()
    usage.require_subscription(user)
    mode = resolve_mode(options.ai_mode or preference_for(user).ai_mode)
    model = mode.model
    if model is None or not model.is_enabled:
        logger.error("AI mode %s has no enabled model", mode.key)
        raise AINotConfigured()
    if not has_usable_keys():
        logger.error("No working OpenAI API key is configured in the admin panel")
        raise AINotConfigured()
    return Route(mode=mode, candidates=_chain(model, mode))


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_json_task(
    user,
    *,
    feature,
    system,
    prompt,
    schema,
    max_output_tokens,
    serializer_class,
    options=None,
    route=None,
    basic=None,
    use_ai=True,
):
    """
    Ask the model for JSON matching `schema`, validate it with `serializer_class`,
    and return an AITaskResult.

    Without `basic`, any failure raises an AIServiceError or UsageError. With
    `basic`, a failure (or `use_ai=False`) returns basic_result(basic()) instead,
    clearly marked as rule-based. AI output is never made up or repaired.
    """
    if basic is not None and not use_ai:
        return basic_result(basic())
    try:
        return _run_ai_task(
            user,
            feature=feature,
            system=system,
            prompt=prompt,
            schema=schema,
            max_output_tokens=max_output_tokens,
            serializer_class=serializer_class,
            options=options,
            route=route,
        )
    except (AIServiceError, usage_errors.UsageError) as exc:
        if basic is None:
            raise
        logger.warning("AI not used for %s (%s); using basic content", feature, exc.default_code)
        return basic_result(basic())


def _run_ai_task(
    user, *, feature, system, prompt, schema, max_output_tokens, serializer_class, options, route
):
    route = route or resolve_route(user, options)
    estimated_input = estimate_input_tokens(system, prompt, json.dumps(schema))
    platform = AIPlatformSettings.load()
    first_error = None

    for index, model in enumerate(route.candidates):
        is_last = index == len(route.candidates) - 1
        output_cap = min(max_output_tokens, model.max_output_tokens)
        try:
            if estimated_input > model.max_input_tokens:
                raise usage_errors.RequestTooLarge()
            reservation = usage.reserve_platform_tokens(
                user,
                tokens=estimated_input + output_cap,
                cost=estimate_cost(model, estimated_input, output_cap),
                feature=feature,
                provider=model.provider,
                model=model.model_id,
            )
        except FALLBACK_LIMIT_ERRORS as exc:
            if first_error is None:
                first_error = exc
            if is_last:
                # Report the limit the preferred model hit.
                raise first_error from None
            continue

        attempt = {"model": model, "feature": feature, "fallback": index > 0}
        try:
            response, key, started = _call_with_keys(
                attempt, system=system, prompt=prompt, schema=schema, max_output_tokens=output_cap
            )
        except FALLBACK_ERRORS as exc:
            usage.release_reservation(reservation)
            if is_last:
                raise
            logger.warning(
                "AI call for %s failed (%s) on %s; trying the configured fallback",
                feature,
                exc.default_code,
                model.model_id,
            )
            continue
        except BaseException:
            usage.release_reservation(reservation)
            raise

        cost = estimate_cost(model, response.input_tokens, response.output_tokens)
        usage.commit_reservation(
            reservation,
            quantity=response.total_tokens,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            estimated_cost=cost,
            cost_currency=platform.cost_currency,
            provider=response.provider,
            model=response.model,
        )
        logger.info(
            "AI call for %s via %s used %s input + %s output tokens",
            feature,
            response.model,
            response.input_tokens,
            response.output_tokens,
        )
        try:
            data = _parse(response, feature, serializer_class)
        except (AIDeclined, AIInvalidResponse) as exc:
            _log_call(attempt, key, exc.default_code, started)
            raise
        _log_call(attempt, key, ProviderCallLog.SUCCESS, started)
        return AITaskResult(
            data=data,
            source=SOURCE_PLATFORM,
            ai_mode=route.mode.key,
            provider=response.provider,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            estimated_cost=cost,
            used_fallback=index > 0,
        )
    raise AIProviderUnavailable()  # unreachable: the last candidate always returns or raises


def _call_with_keys(attempt, *, system, prompt, schema, max_output_tokens):
    """
    Call OpenAI for one model, rotating through the platform's keys. Returns
    (response, key, started). A rate-limited key cools down and a rejected key
    is set aside, then the next key is tried. Raises AIRateLimited when every
    key is limited for this model (the caller may try a fallback model), and
    AINotConfigured when no key works at all.
    """
    model = attempt["model"]
    now = timezone.now()
    keys = list(active_keys())
    if not keys:
        logger.error("No working OpenAI API key is configured in the admin panel")
        raise AINotConfigured()
    ready = [key for key in keys if key.cooling_until(model.model_id, now) is None]
    if not ready:
        logger.warning("Every OpenAI key is cooling down for %s", model.model_id)
        raise AIRateLimited()

    rate_limited = False
    for key in ready:
        try:
            api_key = decrypt_api_key(key.encrypted_key)
        except StoredKeyUnreadable as exc:
            _reject_key(key, exc.default_code)
            continue
        except KeyEncryptionUnavailable as exc:
            raise AINotConfigured() from exc

        started = time.monotonic()
        try:
            response = build_provider(
                model.provider, api_key=api_key, model=model.model_id
            ).generate_json(
                system=system, prompt=prompt, schema=schema, max_output_tokens=max_output_tokens
            )
        except AIRateLimited as exc:
            _log_call(attempt, key, exc.default_code, started)
            _cool_down(key, model.model_id, exc.default_code)
            rate_limited = True
            continue
        except AICredentialsRejected as exc:
            _log_call(attempt, key, exc.default_code, started)
            _reject_key(key, exc.default_code)
            continue
        except (AIProviderUnavailable, AIModelUnavailable) as exc:
            _log_call(attempt, key, exc.default_code, started)
            _note_key_error(key, exc.default_code)
            raise
        except AIRequestRejected as exc:
            # A malformed request or misconfigured model: not the user's fault.
            _log_call(attempt, key, exc.default_code, started)
            logger.error(
                "OpenAI rejected the request for %s (%s)", attempt["feature"], model.model_id
            )
            raise AIProviderUnavailable() from exc
        except Exception:
            _log_call(attempt, key, "unexpected_error", started)
            raise
        _key_succeeded(key, model.model_id)
        return response, key, started

    if rate_limited:
        raise AIRateLimited()
    logger.error("No working OpenAI API key: every key was rejected")
    raise AINotConfigured()


def _log_call(attempt, key, outcome, started):
    """Record a sanitized provider call outcome for health monitoring."""
    model = attempt["model"]
    ProviderCallLog.objects.create(
        provider=model.provider,
        model=model.model_id,
        api_key=key,
        feature=attempt["feature"],
        outcome=outcome,
        latency_ms=int((time.monotonic() - started) * 1000),
        used_fallback=attempt["fallback"],
    )


def _parse(response, feature, serializer_class):
    if response.finish == providers.FINISH_REFUSAL:
        raise AIDeclined()
    if response.finish == providers.FINISH_MAX_TOKENS:
        logger.warning("AI response for feature %s was cut off at the output limit", feature)
        raise AIInvalidResponse()
    if response.finish != providers.FINISH_COMPLETE:
        logger.warning("AI response for feature %s ended unexpectedly", feature)
        raise AIInvalidResponse()
    try:
        data = json.loads(response.text)
    except (TypeError, ValueError) as exc:
        logger.warning("AI response for feature %s was not valid JSON", feature)
        raise AIInvalidResponse() from exc
    if not isinstance(data, dict):
        raise AIInvalidResponse()

    serializer = serializer_class(data=data)
    if not serializer.is_valid():
        # Log field names only: values may contain resume or answer content.
        logger.warning(
            "AI response failed validation for feature %s (fields: %s)",
            feature,
            ", ".join(sorted(serializer.errors)),
        )
        raise AIInvalidResponse()
    return serializer.validated_data


# ---------------------------------------------------------------------------
# Status for the settings page and AI option pickers
# ---------------------------------------------------------------------------


def _mode_queryset():
    return AIMode.objects.select_related("model").filter(
        is_enabled=True, model__isnull=False, model__is_enabled=True
    )


def platform_status(user, now=None):
    """Why AI is or isn't usable for this user right now."""
    if usage.current_subscription(user, now=now) is None:
        try:
            usage.require_subscription(user, now=now)
        except usage_errors.SubscriptionExpired:
            return "subscription_expired"
        except usage_errors.SubscriptionRequired:
            return "subscription_required"
    if not (_mode_queryset().exists() and has_usable_keys()):
        return "not_configured"
    platform = AIPlatformSettings.load()
    cap = platform.monthly_spend_cap
    if cap is not None and usage.month_platform_spend(now) >= cap:
        return "budget_exhausted"
    return "available"


def mode_options():
    """Enabled AI modes that have a usable model."""
    return [
        {"key": mode.key, "label": mode.get_key_display(), "description": mode.description}
        for mode in _mode_queryset()
    ]
