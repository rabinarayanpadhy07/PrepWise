"""
Purchase workflow against the real Razorpay gateway class with its network
calls replaced. Signatures are computed and verified with real HMAC-SHA256
using test-only secrets (config.settings.test), so verification is exercised.
"""

import hashlib
import hmac
import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.billing.models import Plan, Subscription
from apps.payments import gateway as gateway_module
from apps.payments.gateway import PaymentGatewayError, RazorpayGateway
from apps.payments.models import Invoice, Payment, PaymentOrder, Refund, WebhookEvent
from apps.usage import errors as usage_errors
from apps.usage.services import current_subscription, require_subscription

pytestmark = pytest.mark.django_db

KEY_SECRET = "test-only-gateway-secret"
WEBHOOK_SECRET = "test-only-webhook-secret"
ORDERS = "/api/v1/payments/orders/"
WEBHOOK = "/api/v1/payments/webhooks/razorpay/"
PLAN_CODE = "platform-30-day"


class FakeRazorpay(RazorpayGateway):
    """Real signature checks; order creation, payment fetches and refunds are local."""

    def __init__(self):
        super().__init__(
            key_id="rzp_test_notarealkey",
            key_secret=KEY_SECRET,
            webhook_secret=WEBHOOK_SECRET,
            timeout=5,
        )
        self.orders = []
        self.payments = {}
        self.refunds = []
        self.fail_next = False
        self.order_amount_override = None

    def create_order(self, *, amount_minor, currency, receipt, notes):
        if self.fail_next:
            self.fail_next = False
            raise PaymentGatewayError()
        order_id = f"order_test{len(self.orders) + 1:04d}"
        amount = self.order_amount_override or amount_minor
        self.orders.append(
            {"id": order_id, "amount": amount, "currency": currency, "receipt": receipt}
        )
        return {"id": order_id, "amount": amount, "currency": currency, "status": "created"}

    def fetch_payment(self, payment_id):
        return self.payments[payment_id]

    def refund(self, payment_id, *, amount_minor, notes):
        refund = {
            "id": f"rfnd_test{len(self.refunds) + 1:04d}",
            "payment_id": payment_id,
            "amount": amount_minor,
            "currency": "INR",
            "status": "processed",
        }
        self.refunds.append(refund)
        return refund


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeRazorpay()
    monkeypatch.setattr(gateway_module, "get_gateway", lambda: fake)
    return fake


def checkout_signature(order_id, payment_id, secret=KEY_SECRET):
    message = f"{order_id}|{payment_id}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def payment_entity(order, payment_id="pay_test0001", status="captured", amount=None, **extra):
    return {
        "id": payment_id,
        "entity": "payment",
        "order_id": order.gateway_order_id,
        "amount": order.amount_minor if amount is None else amount,
        "currency": "INR",
        "status": status,
        "method": "upi",
        **extra,
    }


def create_order(client):
    return client.post(ORDERS, {"plan_code": PLAN_CODE}, format="json")


def verify(client, order, payment_id="pay_test0001", signature=None, order_id=None):
    order_id = order_id or order.gateway_order_id
    return client.post(
        f"{ORDERS}{order.pk}/verify/",
        {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature or checkout_signature(order_id, payment_id),
        },
        format="json",
    )


def send_webhook(client, event, entities, *, event_id="evt_1", secret=WEBHOOK_SECRET):
    body = json.dumps(
        {
            "entity": "event",
            "event": event,
            "payload": {k: {"entity": v} for k, v in entities.items()},
        }
    ).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        WEBHOOK,
        data=body,
        content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE=signature,
        HTTP_X_RAZORPAY_EVENT_ID=event_id,
    )


def paid_order(client, user, gateway, payment_id="pay_test0001"):
    response = create_order(client)
    order = PaymentOrder.objects.get(pk=response.json()["id"])
    gateway.payments[payment_id] = payment_entity(order, payment_id)
    assert verify(client, order, payment_id).status_code == 200
    order.refresh_from_db()
    return order


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def test_order_amount_comes_from_the_stored_plan(auth_client, user, gateway):
    response = auth_client.post(
        ORDERS,
        {"plan_code": PLAN_CODE, "amount_minor": 1, "price_minor": 1, "currency": "USD"},
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["amount_minor"], body["currency"], body["status"]) == (500_000, "INR", "created")
    assert gateway.orders[0]["amount"] == 500_000
    checkout = body["checkout"]
    assert checkout["key_id"] == "rzp_test_notarealkey"
    assert checkout["gateway_order_id"] == gateway.orders[0]["id"]
    assert KEY_SECRET not in response.content.decode()
    assert WEBHOOK_SECRET not in response.content.decode()
    assert body["subscription"] is None


def test_repeated_clicks_reuse_the_unpaid_order(auth_client, gateway):
    first = create_order(auth_client).json()["id"]
    second = create_order(auth_client).json()["id"]

    assert first == second
    assert len(gateway.orders) == 1


def test_price_change_applies_to_new_orders_only(auth_client, user, gateway):
    old = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    plan = Plan.objects.get(code=PLAN_CODE)
    plan.price_minor = 650_000
    plan.save()

    new = create_order(auth_client).json()

    assert new["id"] != str(old.pk)
    assert new["amount_minor"] == 650_000
    assert new["plan"]["version"] == 2
    old.refresh_from_db()
    assert old.amount_minor == 500_000


def test_gateway_order_with_a_different_amount_is_refused(auth_client, gateway):
    gateway.order_amount_override = 100

    response = create_order(auth_client)

    assert response.status_code == 503
    assert PaymentOrder.objects.get().status == "failed"


def test_gateway_outage_does_not_leave_a_usable_order(auth_client, gateway):
    gateway.fail_next = True

    response = create_order(auth_client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payment_gateway_unavailable"
    assert PaymentOrder.objects.get().status == "failed"


def test_payments_unavailable_without_gateway_configuration(auth_client, settings):
    settings.PAYMENT_SETTINGS = {**settings.PAYMENT_SETTINGS, "PROVIDER": ""}

    response = create_order(auth_client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payments_unavailable"


def test_inactive_or_unknown_plans_cannot_be_bought(auth_client, gateway):
    assert auth_client.post(ORDERS, {"plan_code": "nope"}, format="json").status_code == 404
    Plan.objects.update(is_active=False)
    assert create_order(auth_client).status_code == 404


def test_orders_require_authentication(api_client):
    assert create_order(api_client).status_code == 401


def test_users_cannot_read_or_verify_other_users_orders(
    auth_client, client_for, other_user, gateway
):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    intruder = client_for(other_user)
    gateway.payments["pay_test0001"] = payment_entity(order)

    assert intruder.get(f"{ORDERS}{order.pk}/").status_code == 404
    assert verify(intruder, order).status_code == 404
    assert not Subscription.objects.exists()


# ---------------------------------------------------------------------------
# Checkout verification
# ---------------------------------------------------------------------------


def test_verified_payment_activates_the_subscription(auth_client, user, gateway):
    order = paid_order(auth_client, user, gateway)

    assert order.status == "paid"
    subscription = order.subscription
    assert subscription.user == user
    assert subscription.source == "payment"
    assert subscription.ends_at - subscription.starts_at == timedelta(days=30)
    assert subscription.plan_version == order.plan_version
    assert current_subscription(user) == subscription
    invoice = Invoice.objects.get(order=order)
    assert (invoice.amount_minor, invoice.currency) == (500_000, "INR")
    assert invoice.number.startswith("PW-")
    assert invoice.gateway_payment_id == "pay_test0001"

    body = auth_client.get(f"{ORDERS}{order.pk}/").json()
    assert body["status"] == "paid"
    assert body["checkout"] is None
    assert body["invoice"]["number"] == invoice.number


def test_invalid_checkout_signature_is_rejected(auth_client, user, gateway):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    gateway.payments["pay_test0001"] = payment_entity(order)

    forged = checkout_signature(order.gateway_order_id, "pay_test0001", secret="wrong")
    response = verify(auth_client, order, signature=forged)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "payment_verification_failed"
    order.refresh_from_db()
    assert order.status == "created"
    assert not Subscription.objects.exists()
    assert not Payment.objects.exists()


def test_signature_for_another_order_is_rejected(auth_client, user, gateway):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])

    response = verify(auth_client, order, order_id="order_someone_else")

    assert response.status_code == 400
    assert not Subscription.objects.exists()


def test_failed_payment_does_not_activate(auth_client, user, gateway):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    gateway.payments["pay_test0001"] = payment_entity(
        order, status="failed", error_code="BAD_REQUEST_ERROR", error_reason="payment_failed"
    )

    response = verify(auth_client, order)

    assert response.status_code == 200
    assert response.json()["status"] == "created"
    payment = Payment.objects.get()
    assert (payment.status, payment.error_reason) == ("failed", "payment_failed")
    assert not Subscription.objects.exists()
    with pytest.raises(usage_errors.SubscriptionRequired):
        require_subscription(user)


def test_authorized_but_uncaptured_payment_does_not_activate(auth_client, gateway):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    gateway.payments["pay_test0001"] = payment_entity(order, status="authorized")

    assert verify(auth_client, order).json()["status"] == "created"
    assert not Subscription.objects.exists()


def test_amount_mismatch_is_held_for_review(auth_client, gateway):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    gateway.payments["pay_test0001"] = payment_entity(order, amount=100)

    response = verify(auth_client, order)

    assert response.json()["status"] == "review"
    assert not Subscription.objects.exists()


def test_payment_from_another_order_is_rejected(auth_client, gateway):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    gateway.payments["pay_test0001"] = {**payment_entity(order), "order_id": "order_other"}

    assert verify(auth_client, order).status_code == 400
    assert not Subscription.objects.exists()


def test_verifying_twice_is_idempotent(auth_client, user, gateway):
    order = paid_order(auth_client, user, gateway)

    assert verify(auth_client, order).status_code == 200
    assert Subscription.objects.count() == 1
    assert Invoice.objects.count() == 1


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def test_webhook_activates_a_paid_order(api_client, auth_client, user, gateway):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])

    response = send_webhook(api_client, "payment.captured", {"payment": payment_entity(order)})

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    order.refresh_from_db()
    assert order.status == "paid"
    assert current_subscription(user) == order.subscription


def test_webhook_with_an_invalid_signature_is_rejected(api_client, auth_client, gateway):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])

    response = send_webhook(
        api_client, "payment.captured", {"payment": payment_entity(order)}, secret="forged"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_webhook"
    assert not WebhookEvent.objects.exists()
    assert not Subscription.objects.exists()


def test_webhook_without_a_signature_is_rejected(api_client, gateway):
    response = api_client.post(WEBHOOK, data=b"{}", content_type="application/json")

    assert response.status_code == 400


def test_duplicate_webhook_deliveries_are_processed_once(api_client, auth_client, gateway):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    entities = {"payment": payment_entity(order)}

    for _ in range(3):
        assert send_webhook(api_client, "payment.captured", entities).status_code == 200
    # A different event for the same payment (order.paid) is also harmless.
    send_webhook(api_client, "order.paid", entities, event_id="evt_2")
    # And so is the checkout handler arriving after the webhook.
    gateway.payments["pay_test0001"] = payment_entity(order)
    assert verify(auth_client, order).status_code == 200

    assert Subscription.objects.count() == 1
    assert Invoice.objects.count() == 1
    assert Payment.objects.count() == 1
    assert WebhookEvent.objects.get(event_id="evt_1").attempts == 1


def test_late_authorized_event_does_not_downgrade_a_captured_payment(
    api_client, auth_client, gateway
):
    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    send_webhook(api_client, "payment.captured", {"payment": payment_entity(order)})

    send_webhook(
        api_client,
        "payment.authorized",
        {"payment": payment_entity(order, status="authorized")},
        event_id="evt_late",
    )

    assert Payment.objects.get().status == "captured"


def test_second_captured_payment_for_a_paid_order_is_flagged(
    api_client, auth_client, user, gateway
):
    order = paid_order(auth_client, user, gateway)

    send_webhook(
        api_client,
        "payment.captured",
        {"payment": payment_entity(order, payment_id="pay_test0002")},
        event_id="evt_dup",
    )

    assert Payment.objects.get(gateway_payment_id="pay_test0002").is_duplicate is True
    assert Subscription.objects.count() == 1


def test_unrelated_events_are_acknowledged_and_ignored(api_client, gateway):
    response = send_webhook(api_client, "subscription.charged", {"subscription": {"id": "sub_x"}})

    assert response.json() == {"status": "ignored"}


def test_failed_processing_is_retried_on_the_next_delivery(
    api_client, auth_client, gateway, monkeypatch
):
    from apps.payments import services

    order = PaymentOrder.objects.get(pk=create_order(auth_client).json()["id"])
    entities = {"payment": payment_entity(order)}
    original = services._dispatch

    def broken(*args):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(services, "_dispatch", broken)
    response = send_webhook(api_client, "payment.captured", entities)
    assert response.status_code == 500
    event = WebhookEvent.objects.get()
    assert (event.status, event.error) == ("failed", "RuntimeError")

    monkeypatch.setattr(services, "_dispatch", original)
    assert send_webhook(api_client, "payment.captured", entities).status_code == 200
    event.refresh_from_db()
    assert (event.status, event.attempts) == ("processed", 2)
    assert Subscription.objects.count() == 1


# ---------------------------------------------------------------------------
# Refunds and reversals
# ---------------------------------------------------------------------------


def refund_entity(refund_id="rfnd_1", amount=500_000, status="processed"):
    return {
        "id": refund_id,
        "payment_id": "pay_test0001",
        "amount": amount,
        "currency": "INR",
        "status": status,
    }


def test_full_refund_revokes_access_immediately(api_client, auth_client, user, gateway):
    order = paid_order(auth_client, user, gateway)

    send_webhook(api_client, "refund.processed", {"refund": refund_entity()}, event_id="evt_r")

    subscription = Subscription.objects.get(pk=order.subscription_id)
    assert subscription.status == "revoked"
    assert subscription.revoke_reason == "Payment refunded in full"
    assert Payment.objects.get().status == "refunded"
    assert current_subscription(user) is None
    assert auth_client.get("/api/v1/billing/subscription/").json()["status"] == "revoked"


def test_partial_refund_keeps_access(api_client, auth_client, user, gateway):
    paid_order(auth_client, user, gateway)

    send_webhook(
        api_client, "refund.processed", {"refund": refund_entity(amount=100_000)}, event_id="evt_r"
    )

    payment = Payment.objects.get()
    assert (payment.status, payment.amount_refunded_minor) == ("partially_refunded", 100_000)
    assert current_subscription(user) is not None


def test_pending_refund_does_not_revoke_until_processed(api_client, auth_client, user, gateway):
    paid_order(auth_client, user, gateway)

    send_webhook(
        api_client, "refund.created", {"refund": refund_entity(status="pending")}, event_id="e1"
    )
    assert current_subscription(user) is not None

    send_webhook(api_client, "refund.processed", {"refund": refund_entity()}, event_id="e2")
    assert current_subscription(user) is None
    assert Refund.objects.count() == 1


def test_lost_dispute_revokes_access_but_an_open_one_does_not(
    api_client, auth_client, user, gateway
):
    paid_order(auth_client, user, gateway)
    dispute = {"id": "disp_1", "payment_id": "pay_test0001", "status": "open"}

    send_webhook(api_client, "payment.dispute.created", {"dispute": dispute}, event_id="d1")
    assert current_subscription(user) is not None
    assert Payment.objects.get().dispute_status == "open"

    send_webhook(
        api_client,
        "payment.dispute.lost",
        {"dispute": {**dispute, "status": "lost"}},
        event_id="d2",
    )
    assert current_subscription(user) is None


def test_admin_refund_command_refunds_through_the_gateway(auth_client, user, gateway):
    paid_order(auth_client, user, gateway)
    out = StringIO()

    call_command("refund_payment", "pay_test0001", "--reason", "Requested", stdout=out)

    assert gateway.refunds[0]["amount"] == 500_000
    assert "processed" in out.getvalue()
    assert current_subscription(user) is None
    assert Refund.objects.get().reason == "Requested"


# ---------------------------------------------------------------------------
# Expiry and manual renewal
# ---------------------------------------------------------------------------


def test_access_ends_at_expiry_and_nothing_renews_automatically(auth_client, user, gateway):
    order = paid_order(auth_client, user, gateway)
    ends_at = order.subscription.ends_at

    assert current_subscription(user, now=ends_at - timedelta(seconds=1)) is not None
    assert current_subscription(user, now=ends_at) is None
    with pytest.raises(usage_errors.SubscriptionExpired):
        require_subscription(user, now=ends_at + timedelta(days=1))
    assert Subscription.objects.count() == 1


def test_expired_status_is_reported(auth_client, user, gateway):
    order = paid_order(auth_client, user, gateway)
    past = timezone.now() - timedelta(days=60)
    Subscription.objects.filter(pk=order.subscription_id).update(
        starts_at=past, ends_at=past + timedelta(days=30)
    )

    body = auth_client.get("/api/v1/billing/subscription/").json()

    assert body["status"] == "expired"
    assert body["current"] is None
    assert body["payments_enabled"] is True


def test_manual_renewal_during_an_active_period_starts_when_it_ends(auth_client, user, gateway):
    first = paid_order(auth_client, user, gateway, "pay_test0001")
    second = paid_order(auth_client, user, gateway, "pay_test0002")

    assert second.pk != first.pk
    assert second.subscription.starts_at == first.subscription.ends_at
    assert current_subscription(user) == first.subscription

    status = auth_client.get("/api/v1/billing/subscription/").json()
    assert status["status"] == "active"
    assert [item["id"] for item in status["upcoming"]] == [second.subscription_id]
    invoice = Invoice.objects.get(order=second)
    assert invoice.period_start == second.subscription.starts_at


def test_renewal_after_expiry_starts_now(auth_client, user, gateway):
    first = paid_order(auth_client, user, gateway, "pay_test0001")
    past = timezone.now() - timedelta(days=40)
    Subscription.objects.filter(pk=first.subscription_id).update(
        starts_at=past, ends_at=past + timedelta(days=30)
    )

    second = paid_order(auth_client, user, gateway, "pay_test0002")

    assert abs(second.subscription.starts_at - timezone.now()) < timedelta(minutes=1)


def test_existing_purchase_keeps_its_terms_after_a_plan_change(auth_client, user, gateway):
    order = paid_order(auth_client, user, gateway)
    plan = Plan.objects.get(code=PLAN_CODE)
    plan.price_minor = 900_000
    plan.duration_days = 10
    plan.interview_quota = 1
    plan.save()

    subscription = Subscription.objects.get(pk=order.subscription_id)
    assert subscription.ends_at - subscription.starts_at == timedelta(days=30)
    assert subscription.interview_quota == 60
    assert Invoice.objects.get(order=order).amount_minor == 500_000


# ---------------------------------------------------------------------------
# History and invoices
# ---------------------------------------------------------------------------


def test_payment_history_lists_only_the_users_attempts(
    auth_client, client_for, user, other_user, gateway
):
    paid_order(auth_client, user, gateway)
    create_order(client_for(other_user))

    history = auth_client.get("/api/v1/payments/history/").json()

    assert history["count"] == 1
    entry = history["results"][0]
    assert entry["status"] == "paid"
    assert entry["payments"][0]["gateway_payment_id"] == "pay_test0001"
    assert entry["invoice"]["number"].startswith("PW-")
    assert client_for(other_user).get("/api/v1/payments/history/").json()["count"] == 0


def test_invoice_pdf_is_downloadable_by_its_owner_only(
    auth_client, client_for, user, other_user, gateway
):
    order = paid_order(auth_client, user, gateway)
    url = f"/api/v1/payments/invoices/{order.invoice.pk}/pdf/"

    response = auth_client.get(url)

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "private, no-store"
    assert response.content.startswith(b"%PDF")
    assert order.invoice.number in response["Content-Disposition"]
    assert client_for(other_user).get(url).status_code == 404


def test_invoice_numbers_are_sequential(auth_client, user, gateway):
    first = paid_order(auth_client, user, gateway, "pay_test0001").invoice.number
    second = paid_order(auth_client, user, gateway, "pay_test0002").invoice.number

    assert int(second.rsplit("-", 1)[1]) == int(first.rsplit("-", 1)[1]) + 1
