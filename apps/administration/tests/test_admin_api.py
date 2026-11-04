"""
Administrator API: authorization on every endpoint, audit logging, validation
of configuration changes, usage/cost reporting, payments, and provider health.
"""

import json
import uuid
from decimal import Decimal

import pytest
from django.urls import URLPattern, reverse
from django.utils import timezone

from apps.administration import urls as admin_urls
from apps.administration.models import AuditLogEntry
from apps.ai_services.errors import AIProviderUnavailable
from apps.ai_services.models import AIMode, AIModelConfig, AIPlatformSettings, ProviderCallLog
from apps.billing.models import Plan, Subscription
from apps.interviews.tests.test_interviews_api import answer, start
from apps.payments import gateway as gateway_module
from apps.payments.models import PaymentOrder
from apps.payments.tests.test_payments import FakeRazorpay, paid_order
from apps.resumes.tests.test_api import make_resume
from apps.usage.models import UsageEvent
from apps.usage.services import current_subscription
from conftest import LARGE_MODEL, PLATFORM_KEY

pytestmark = pytest.mark.django_db

ADMIN = "/api/v1/admin/"
ANSWER_TEXT = "My answer with a concrete example."
SECRETS = [
    PLATFORM_KEY,
    "test-only-gateway-secret",
    "test-only-webhook-secret",
    "dGVzdC1vbmx5LWFlcy1rZXktbnVtYmVyLXR3by0yISE",
]


@pytest.fixture
def admin_client(client_for, admin_user):
    return client_for(admin_user)


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeRazorpay()
    monkeypatch.setattr(gateway_module, "get_gateway", lambda: fake)
    return fake


def entries(action):
    return list(AuditLogEntry.objects.filter(action=action))


# ---------------------------------------------------------------------------
# Authorization on every endpoint
# ---------------------------------------------------------------------------

DUMMY_KWARGS = {
    "pk": 1,
    "code": "platform-30-day",
    "key": "fast",
    "payment_id": "pay_test0001",
}


def admin_endpoints():
    """(url, methods) for every route in the admin API, with placeholder IDs."""
    found = []
    for pattern in admin_urls.urlpatterns:
        assert isinstance(pattern, URLPattern)
        kwargs = {
            name: (uuid.uuid4() if name == "pk" and "orders" in str(pattern.pattern) else value)
            for name, value in DUMMY_KWARGS.items()
            if f"<{name}>" in str(pattern.pattern) or f":{name}>" in str(pattern.pattern)
        }
        url = reverse(f"administration:{pattern.name}", kwargs={"version": "v1", **kwargs})
        view = pattern.callback.view_class
        methods = [m for m in ("get", "post", "patch") if hasattr(view, m)]
        found.append((url, methods))
    return found


def test_every_admin_route_is_covered():
    assert len(admin_endpoints()) == len(admin_urls.urlpatterns) >= 25


@pytest.mark.parametrize(("url", "methods"), admin_endpoints())
def test_anonymous_requests_are_refused(api_client, url, methods):
    for method in methods:
        response = getattr(api_client, method)(url, {}, format="json")
        assert response.status_code == 401, (method, url)


@pytest.mark.parametrize(("url", "methods"), admin_endpoints())
def test_normal_users_get_403_and_no_data(auth_client, subscribe, user, url, methods):
    subscribe(user)
    before = (
        AuditLogEntry.objects.count(),
        Subscription.objects.count(),
        Plan.objects.get().price_minor,
    )
    for method in methods:
        response = getattr(auth_client, method)(
            url, {"price_minor": 1, "role": "admin"}, format="json"
        )
        assert response.status_code == 403, (method, url)
        assert set(response.json()) == {"error"}
    after = (
        AuditLogEntry.objects.count(),
        Subscription.objects.count(),
        Plan.objects.get().price_minor,
    )
    assert before == after


def test_suspended_administrators_lose_access(client_for, make_user):
    former = make_user(email="former@example.com", role="admin")
    client = client_for(former)
    former.is_active = False
    former.save()

    assert client.get(f"{ADMIN}overview/").status_code == 401


def test_admin_flag_cannot_be_set_through_profile_update(auth_client, user):
    auth_client.patch("/api/v1/auth/me/", {"role": "admin", "is_staff": True}, format="json")

    user.refresh_from_db()
    assert user.role == "user"
    assert auth_client.get(f"{ADMIN}overview/").status_code == 403


# ---------------------------------------------------------------------------
# Users: suspension and session revocation
# ---------------------------------------------------------------------------


def test_suspending_a_user_revokes_access_and_is_audited(admin_client, client_for, user):
    victim_client = client_for(user)
    assert victim_client.get("/api/v1/auth/me/").status_code == 200

    response = admin_client.patch(
        f"{ADMIN}users/{user.pk}/", {"is_active": False, "reason": "Abuse report"}, format="json"
    )

    assert response.status_code == 200
    assert victim_client.get("/api/v1/auth/me/").status_code == 401
    [entry] = entries("user.update")
    assert entry.changes == {"is_active": [True, False]}
    assert entry.reason == "Abuse report"
    assert entry.actor_email == "admin@example.com"
    assert entry.target_id == str(user.pk)


def test_revoking_sessions_rejects_existing_tokens_but_allows_new_logins(
    admin_client, client_for, user, api_client
):
    from conftest import PASSWORD

    old_client = client_for(user)
    # Tokens are compared in whole seconds; make the old token clearly older.
    type(user).objects.filter(pk=user.pk).update(sessions_revoked_at=None)

    response = admin_client.post(f"{ADMIN}users/{user.pk}/revoke-sessions/", {}, format="json")

    assert response.status_code == 200
    assert response.json()["sessions_revoked_at"]
    rejected = old_client.get("/api/v1/auth/me/")
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "session_expired"
    user.refresh_from_db()
    assert user.is_active
    assert entries("user.revoke_sessions")

    # A fresh login a second later works.
    import time

    time.sleep(1.1)
    api_client.get("/api/v1/auth/csrf/")
    login = api_client.post(
        "/api/v1/auth/login/",
        {"email": user.email, "password": PASSWORD},
        format="json",
        HTTP_X_CSRFTOKEN=api_client.cookies["csrftoken"].value,
        HTTP_ORIGIN="http://localhost:5173",
    )
    assert login.status_code == 200, login.json()
    fresh = client_for(user)
    fresh.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
    assert fresh.get("/api/v1/auth/me/").status_code == 200


def test_admins_cannot_change_or_revoke_themselves(admin_client, admin_user):
    assert (
        admin_client.patch(
            f"{ADMIN}users/{admin_user.pk}/", {"role": "user"}, format="json"
        ).status_code
        == 400
    )
    assert admin_client.post(f"{ADMIN}users/{admin_user.pk}/revoke-sessions/").status_code == 400


def test_user_list_is_paginated_and_filterable(admin_client, make_user):
    for index in range(25):
        make_user(email=f"person{index}@example.com", is_active=index % 5 != 0)

    page = admin_client.get(f"{ADMIN}users/").json()
    suspended = admin_client.get(f"{ADMIN}users/", {"status": "suspended"}).json()

    assert page["count"] == 26 and len(page["results"]) == 20 and page["next"]
    assert suspended["count"] == 5


def test_user_summary_shows_counts_but_no_private_content(
    admin_client, user, subscribe, ai_config, auth_client, fake_ai
):
    subscribe(user)
    make_resume(user)
    session = start(auth_client, fake_ai).json()
    answer(auth_client, session["id"], 1, text=ANSWER_TEXT)

    response = admin_client.get(f"{ADMIN}users/{user.pk}/summary/")

    assert response.status_code == 200
    body = response.json()
    assert body["counts"]["answers"] == 1
    assert body["counts"]["resumes"] == 1
    assert "credentials" not in body and "byok_usage" not in body
    text = response.content.decode()
    assert ANSWER_TEXT not in text
    assert "Python and Django engineer" not in text  # resume text
    assert "Tell me about challenge" not in text  # interview questions
    for secret in SECRETS:
        assert secret not in text


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def test_admin_can_grant_and_cancel_a_subscription(admin_client, user):
    response = admin_client.post(
        f"{ADMIN}subscriptions/",
        {"user_id": user.pk, "plan_code": "platform-30-day", "duration_days": 7, "reason": "Trial"},
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "admin"
    subscription = Subscription.objects.get(pk=body["id"])
    assert (subscription.ends_at - subscription.starts_at).days == 7
    assert current_subscription(user) == subscription

    cancel = admin_client.patch(
        f"{ADMIN}subscriptions/{subscription.pk}/",
        {"status": "cancelled", "reason": "Granted by mistake"},
        format="json",
    )
    assert cancel.status_code == 200
    assert current_subscription(user) is None
    assert entries("subscription.grant")[0].reason == "Trial"
    assert entries("subscription.update")[0].changes == {"status": ["active", "cancelled"]}


@pytest.mark.parametrize(
    "payload",
    [
        {"ends_at": "2000-01-01T00:00:00Z"},
        {"daily_interview_limit": 1000},
        {"status": "revoked"},
        {"interview_quota": -1},
    ],
)
def test_invalid_subscription_changes_are_rejected(admin_client, user, subscribe, payload):
    subscription = subscribe(user)

    response = admin_client.patch(
        f"{ADMIN}subscriptions/{subscription.pk}/", payload, format="json"
    )

    assert response.status_code == 400
    assert not AuditLogEntry.objects.exists()


def test_revoked_subscriptions_cannot_be_reactivated(admin_client, user, subscribe):
    subscription = subscribe(user, status="revoked")

    response = admin_client.patch(
        f"{ADMIN}subscriptions/{subscription.pk}/", {"status": "active"}, format="json"
    )

    assert response.status_code == 400


def test_cannot_grant_to_a_suspended_user(admin_client, make_user):
    suspended = make_user(email="gone@example.com", is_active=False)

    response = admin_client.post(
        f"{ADMIN}subscriptions/",
        {"user_id": suspended.pk, "plan_code": "platform-30-day"},
        format="json",
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def test_plan_price_and_duration_changes_apply_to_future_purchases(admin_client, user, subscribe):
    existing = subscribe(user)

    response = admin_client.patch(
        f"{ADMIN}plans/platform-30-day/",
        {"price_minor": 600_000, "duration_days": 45, "reason": "New pricing"},
        format="json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["versions"][0]["version"] == 2
    assert body["versions"][0]["price_minor"] == 600_000
    existing.refresh_from_db()
    assert existing.plan_version.price_minor == 500_000
    [entry] = entries("plan.update")
    assert entry.changes == {"price_minor": [500_000, 600_000], "duration_days": [30, 45]}


@pytest.mark.parametrize(
    "payload",
    [
        {"price_minor": 0},
        {"price_minor": 10**10},
        {"duration_days": 0},
        {"duration_days": 400},
        {"daily_ai_token_limit": 10**9},
        {"currency": "USD"},
    ],
)
def test_invalid_plan_changes_are_rejected(admin_client, payload):
    response = admin_client.patch(f"{ADMIN}plans/platform-30-day/", payload, format="json")

    plan = Plan.objects.get()
    if "currency" in payload:
        # Read-only field: ignored, not applied.
        assert plan.currency == "INR"
        return
    assert response.status_code == 400
    assert plan.current_version.version == 1


# ---------------------------------------------------------------------------
# AI models, modes and settings
# ---------------------------------------------------------------------------


def model_payload(**overrides):
    payload = {
        "model_id": "admin-test-model",
        "display_name": "Admin test",
        "max_input_tokens": 50_000,
        "max_output_tokens": 4_000,
        "input_price_per_million": "2.5",
        "output_price_per_million": "10",
    }
    payload.update(overrides)
    return payload


def test_admin_creates_a_model_with_limits_and_prices(admin_client):
    response = admin_client.post(f"{ADMIN}ai/models/", model_payload(), format="json")

    assert response.status_code == 201, response.json()
    model = AIModelConfig.objects.get(model_id="admin-test-model")
    assert (model.provider, model.max_input_tokens, model.max_output_tokens) == (
        "openai",
        50_000,
        4_000,
    )
    assert entries("ai_model.create")[0].changes["model_id"] == [None, "admin-test-model"]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"input_price_per_million": None}, "input_price_per_million"),
        ({"model_id": "has spaces"}, "model_id"),
        ({"max_output_tokens": 0}, "max_output_tokens"),
        ({"input_price_per_million": "-1"}, "input_price_per_million"),
    ],
)
def test_invalid_models_are_rejected(admin_client, overrides, field):
    response = admin_client.post(f"{ADMIN}ai/models/", model_payload(**overrides), format="json")

    assert response.status_code == 400
    assert field in json.dumps(response.json()["error"])


def test_free_models_are_accepted(admin_client):
    response = admin_client.post(
        f"{ADMIN}ai/models/",
        model_payload(input_price_per_million="0", output_price_per_million="0"),
        format="json",
    )

    assert response.status_code == 201, response.json()


def test_fallback_must_cost_no_more(admin_client, ai_config):
    small = ai_config["small"]

    dearer = admin_client.patch(
        f"{ADMIN}ai/models/{small.pk}/", {"fallback_id": ai_config["large"].pk}, format="json"
    )

    assert dearer.status_code == 400


def test_models_in_use_by_an_enabled_mode_cannot_be_disabled(admin_client, ai_config):
    large = ai_config["large"]

    disable = admin_client.patch(
        f"{ADMIN}ai/models/{large.pk}/", {"is_enabled": False}, format="json"
    )

    assert disable.status_code == 400
    large.refresh_from_db()
    assert large.is_enabled


def test_token_limits_and_fallback_policy_are_editable(admin_client, ai_config):
    response = admin_client.patch(
        f"{ADMIN}ai/models/{ai_config['large'].pk}/",
        {"max_input_tokens": 1000, "max_output_tokens": 500},
        format="json",
    )
    mode = admin_client.patch(f"{ADMIN}ai/modes/fast/", {"allow_fallback": True}, format="json")

    assert response.status_code == 200
    assert mode.status_code == 200
    assert AIMode.objects.get(key="fast").allow_fallback is True
    assert entries("ai_model.update")[0].changes == {
        "max_input_tokens": [200_000, 1000],
        "max_output_tokens": [32_000, 500],
    }


def test_mode_validation(admin_client, ai_config):
    enable_empty = admin_client.patch(
        f"{ADMIN}ai/modes/advanced/", {"is_enabled": True}, format="json"
    )
    ai_config["small"].is_enabled = False
    ai_config["small"].save()
    disabled_model = admin_client.patch(
        f"{ADMIN}ai/modes/advanced/",
        {"model_id": ai_config["small"].pk, "is_enabled": True},
        format="json",
    )
    unknown_model = admin_client.patch(
        f"{ADMIN}ai/modes/advanced/", {"model_id": 999_999}, format="json"
    )

    assert enable_empty.status_code == 400
    assert disabled_model.status_code == 400
    assert unknown_model.status_code == 400


def test_fast_cannot_be_disabled_while_other_modes_are_enabled(admin_client, ai_config):
    ok = admin_client.patch(
        f"{ADMIN}ai/modes/balanced/",
        {"model_id": ai_config["small"].pk, "is_enabled": True},
        format="json",
    )
    assert ok.status_code == 200, ok.json()

    response = admin_client.patch(f"{ADMIN}ai/modes/fast/", {"is_enabled": False}, format="json")

    assert response.status_code == 400


def test_platform_settings_validation_and_audit(admin_client, settings):
    timeout = settings.AI_SETTINGS["REQUEST_TIMEOUT_SECONDS"]
    too_short = admin_client.patch(
        f"{ADMIN}ai/settings/", {"reservation_ttl_seconds": timeout}, format="json"
    )
    bad_currency = admin_client.patch(
        f"{ADMIN}ai/settings/", {"cost_currency": "dollars"}, format="json"
    )
    negative = admin_client.patch(
        f"{ADMIN}ai/settings/", {"monthly_spend_cap": "-5"}, format="json"
    )
    ok = admin_client.patch(
        f"{ADMIN}ai/settings/",
        {
            "monthly_spend_cap": "250.00",
            "voice_max_answer_seconds": 120,
        },
        format="json",
    )

    assert too_short.status_code == bad_currency.status_code == negative.status_code == 400
    assert ok.status_code == 200
    assert AIPlatformSettings.load().monthly_spend_cap == Decimal("250.00")
    [entry] = entries("ai_settings.update")
    assert entry.changes["monthly_spend_cap"][1] == "250.00"


# ---------------------------------------------------------------------------
# Usage and estimated costs
# ---------------------------------------------------------------------------


def test_usage_reports_tokens_and_estimated_costs(
    admin_client, user, other_user, subscribe, ai_config, fake_ai, client_for
):
    subscribe(user)
    subscribe(other_user)
    start(client_for(user), fake_ai)  # 1200 in + 800 out on the large model
    start(client_for(user), fake_ai)

    summary = admin_client.get(f"{ADMIN}usage/summary/", {"days": 7}).json()

    assert summary["platform"]["tokens"] == 4000
    assert "byok" not in summary
    assert Decimal(str(summary["platform"]["estimated_cost"])) == Decimal("0.052")
    assert summary["platform"]["interviews"] == 2
    assert {row["model"] for row in summary["by_model"]} == {LARGE_MODEL}
    assert summary["daily"][0]["platform_tokens"] == 4000

    per_user = admin_client.get(f"{ADMIN}usage/users/", {"ordering": "platform_cost"}).json()
    assert [row["email"] for row in per_user["results"]] == ["user@example.com"]
    assert per_user["results"][0]["platform_tokens"] == 4000
    assert "byok_tokens" not in per_user["results"][0]


def test_usage_window_is_validated(admin_client):
    assert admin_client.get(f"{ADMIN}usage/summary/", {"days": 0}).status_code == 400
    assert admin_client.get(f"{ADMIN}usage/users/", {"days": "abc"}).status_code == 400


def test_per_user_usage_is_paginated(admin_client, make_user, subscribe):
    for index in range(25):
        person = make_user(email=f"u{index}@example.com")
        UsageEvent.objects.create(
            user=person,
            subscription=subscribe(person),
            kind="ai_tokens",
            source="platform",
            quantity=10 + index,
        )

    page = admin_client.get(f"{ADMIN}usage/users/", {"ordering": "platform_tokens"}).json()

    assert page["count"] == 25 and len(page["results"]) == 20
    assert page["results"][0]["platform_tokens"] == 34


# ---------------------------------------------------------------------------
# Payments, invoices and refunds
# ---------------------------------------------------------------------------


def test_admin_lists_orders_and_refunds_a_payment(admin_client, auth_client, user, gateway):
    order = paid_order(auth_client, user, gateway)

    listing = admin_client.get(f"{ADMIN}payments/orders/", {"status": "paid"}).json()
    assert listing["count"] == 1
    assert listing["results"][0]["payments"][0]["refundable_minor"] == 500_000

    response = admin_client.post(
        f"{ADMIN}payments/payments/pay_test0001/refund/",
        {"reason": "Customer request"},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["payments"][0]["status"] == "refunded"
    assert current_subscription(user) is None
    [entry] = entries("payment.refund")
    assert entry.reason == "Customer request"
    assert entry.changes["amount_minor"][1] == 500_000
    order.refresh_from_db()
    assert order.invoice


def test_refunds_are_validated(admin_client, auth_client, user, gateway):
    paid_order(auth_client, user, gateway)
    url = f"{ADMIN}payments/payments/pay_test0001/refund/"

    assert (
        admin_client.post(url, {"amount_minor": 999_999, "reason": "x"}, format="json").status_code
        == 400
    )
    assert admin_client.post(url, {"amount_minor": 100}, format="json").status_code == 400  # reason
    assert gateway.refunds == []


def test_admin_downloads_any_invoice(admin_client, auth_client, user, gateway):
    order = paid_order(auth_client, user, gateway)

    response = admin_client.get(f"{ADMIN}payments/invoices/{order.invoice.pk}/pdf/")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_webhook_events_omit_payloads(admin_client, api_client, auth_client, user, gateway):
    from apps.payments.tests.test_payments import payment_entity, send_webhook

    order = PaymentOrder.objects.get(
        pk=auth_client.post(
            "/api/v1/payments/orders/", {"plan_code": "platform-30-day"}, format="json"
        ).json()["id"]
    )
    send_webhook(api_client, "payment.captured", {"payment": payment_entity(order)})

    response = admin_client.get(f"{ADMIN}payments/webhooks/")

    row = response.json()["results"][0]
    assert row["status"] == "processed"
    assert "payload" not in row
    assert "upi" not in response.content.decode()


# ---------------------------------------------------------------------------
# Provider health and sanitized call logs
# ---------------------------------------------------------------------------


def test_provider_calls_are_logged_without_content(
    admin_client, user, subscribe, ai_config, fake_ai, client_for
):
    subscribe(user)
    client = client_for(user)
    start(client, fake_ai)
    fake_ai.fail(AIProviderUnavailable())
    client.post("/api/v1/interviews/", {"role_title": "PM", "mode": "text"}, format="json")

    logs = list(
        ProviderCallLog.objects.order_by("created_at").values("outcome", "model", "api_key")
    )
    key = ai_config["key"].pk
    assert logs == [
        {"outcome": "success", "model": LARGE_MODEL, "api_key": key},
        {"outcome": "ai_provider_unavailable", "model": LARGE_MODEL, "api_key": key},
    ]

    [health] = admin_client.get(f"{ADMIN}health/providers/").json()
    assert health["provider"] == "openai"
    assert health["platform_key_configured"] is True
    assert health["keys"] == {"total": 1, "usable": 1}
    assert health["last_24h"]["total"] == 2
    assert health["last_24h"]["errors"] == 1
    assert health["last_error"]["outcome"] == "ai_provider_unavailable"

    errors = admin_client.get(f"{ADMIN}health/calls/", {"errors_only": "true"})
    assert errors.json()["count"] == 1
    text = errors.content.decode()
    assert "Product Manager" not in text and user.email not in text


# ---------------------------------------------------------------------------
# Audit log and secret hygiene
# ---------------------------------------------------------------------------


def test_audit_log_is_filterable_and_immutable(admin_client, user):
    admin_client.patch(f"{ADMIN}users/{user.pk}/", {"role": "admin"}, format="json")
    admin_client.patch(f"{ADMIN}plans/platform-30-day/", {"description": "Updated"}, format="json")

    plans = admin_client.get(f"{ADMIN}audit-log/", {"action": "plan."}).json()
    everything = admin_client.get(f"{ADMIN}audit-log/").json()

    assert plans["count"] == 1
    assert everything["count"] == 2
    assert everything["results"][0]["actor_email"] == "admin@example.com"
    entry = AuditLogEntry.objects.first()
    with pytest.raises(ValueError):
        entry.save()
    with pytest.raises(ValueError):
        entry.delete()


def test_no_admin_response_contains_secrets_or_private_content(
    admin_client, user, subscribe, ai_config, auth_client, fake_ai, gateway
):
    subscribe(user)
    session = start(auth_client, fake_ai).json()
    answer(auth_client, session["id"], 1, text=ANSWER_TEXT)
    paid_order(auth_client, user, gateway)

    for url, methods in admin_endpoints():
        if "get" not in methods or url.endswith("/pdf/") or "/1/" in url or "orders/" in url[-45:]:
            continue
        response = admin_client.get(url)
        assert response.status_code in (200, 404), url
        text = response.content.decode()
        for secret in [*SECRETS, ANSWER_TEXT, "SECRET-GUIDANCE"]:
            assert secret not in text, (url, secret)
    # The user summary specifically.
    text = admin_client.get(f"{ADMIN}users/{user.pk}/summary/").content.decode()
    for secret in [*SECRETS, ANSWER_TEXT]:
        assert secret not in text


def test_overview_reports_real_numbers(admin_client, auth_client, user, gateway):
    paid_order(auth_client, user, gateway)

    body = admin_client.get(f"{ADMIN}overview/").json()

    assert body["users"]["total"] == 2
    assert body["subscriptions"]["active"] == 1
    assert body["revenue_this_month"]["gross_minor"] == 500_000
    assert body["revenue_this_month"]["paid_orders"] == 1
    assert body["provider_calls_24h"] == {"total": 0, "errors": 0}
    assert timezone.now()
