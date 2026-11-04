"""
Purchase workflow. The frontend never decides prices or payment outcomes:

  1. create_order()    amount comes from the plan's current PlanVersion
  2. verify_checkout() checks the Checkout signature, then fetches the payment
                       from the gateway and applies it
  3. handle_webhook()  verifies the webhook signature, deduplicates by event ID,
                       and applies payments, refunds and disputes

A subscription is activated only by apply_payment() for a captured payment
whose order, amount and currency match. Activation is idempotent.

Refunds and reversals (see docs/payments.md):
  * Full refund (refund.processed reaching the payment amount): the
    subscription bought by that payment is revoked immediately.
  * Partial refund: recorded; access is kept.
  * Dispute (chargeback) opened: recorded; access is kept while it is open.
  * Dispute lost: the subscription is revoked immediately.
  * Dispute won: recorded; nothing else changes.
"""

import hashlib
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException

from apps.billing.models import Subscription
from apps.payments import gateway as gateway_module
from apps.payments.models import (
    Invoice,
    InvoiceCounter,
    Payment,
    PaymentOrder,
    Refund,
    WebhookEvent,
)

logger = logging.getLogger("prepwise.payments")

# An unpaid order is reused for the same plan version for this long, so
# repeated clicks don't create a pile of gateway orders.
ORDER_REUSE_WINDOW = timedelta(hours=1)


class PaymentVerificationFailed(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "payment_verification_failed"
    default_detail = "We couldn't verify this payment. If you were charged, contact support."


class InvalidWebhook(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "invalid_webhook"
    default_detail = "Invalid webhook."


class PlanUnavailable(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "plan_unavailable"
    default_detail = "This plan can't be purchased right now."


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def create_order(user, plan):
    if not plan.is_active:
        raise PlanUnavailable()
    version = plan.current_version
    if version is None:
        raise PlanUnavailable()

    reusable = (
        PaymentOrder.objects.filter(
            user=user,
            plan_version=version,
            status=PaymentOrder.Status.CREATED,
            gateway_order_id__isnull=False,
            created_at__gte=timezone.now() - ORDER_REUSE_WINDOW,
        )
        .order_by("-created_at")
        .first()
    )
    if reusable is not None:
        return reusable

    gateway = gateway_module.get_gateway()
    order = PaymentOrder.objects.create(
        user=user,
        plan_version=version,
        amount_minor=version.price_minor,
        currency=version.currency,
        gateway=gateway.name,
    )
    try:
        data = gateway.create_order(
            amount_minor=order.amount_minor,
            currency=order.currency,
            receipt=order.receipt,
            notes={"prepwise_order": str(order.pk), "plan": version.plan.code},
        )
        if data.get("amount") != order.amount_minor or data.get("currency") != order.currency:
            logger.error("Gateway order %s does not match the requested amount", data.get("id"))
            raise gateway_module.PaymentGatewayError()
    except gateway_module.PaymentGatewayError:
        order.status = PaymentOrder.Status.FAILED
        order.save(update_fields=["status"])
        raise
    order.gateway_order_id = data["id"]
    order.save(update_fields=["gateway_order_id"])
    return order


def verify_checkout(order, *, gateway_order_id, gateway_payment_id, signature):
    """Verify the Checkout response, then apply the payment as fetched from the gateway."""
    if order.status == PaymentOrder.Status.PAID:
        return order
    gateway = gateway_module.get_gateway()
    if gateway_order_id != order.gateway_order_id or not gateway.verify_payment_signature(
        order_id=order.gateway_order_id, payment_id=gateway_payment_id, signature=signature
    ):
        logger.warning("Checkout signature verification failed for order %s", order.pk)
        raise PaymentVerificationFailed()
    payment = gateway.fetch_payment(gateway_payment_id)
    return apply_payment(order, payment)


# ---------------------------------------------------------------------------
# Applying verified gateway data
# ---------------------------------------------------------------------------

_PAYMENT_STATUSES = {
    "authorized": Payment.Status.AUTHORIZED,
    "captured": Payment.Status.CAPTURED,
    "failed": Payment.Status.FAILED,
    "refunded": Payment.Status.REFUNDED,
}


def apply_payment(order, entity):
    """
    Record a gateway payment entity (from a verified source) against `order`,
    and activate the subscription if it is a matching captured payment.
    """
    with transaction.atomic():
        order = PaymentOrder.objects.select_for_update().get(pk=order.pk)
        if entity.get("order_id") != order.gateway_order_id:
            logger.error("Payment %s does not belong to order %s", entity.get("id"), order.pk)
            raise PaymentVerificationFailed()

        payment = _record_payment(order, entity)
        if payment.status != Payment.Status.CAPTURED:
            return order

        matches = payment.amount_minor == order.amount_minor and payment.currency == order.currency
        if not matches:
            logger.error(
                "Captured payment %s does not match order %s; marked for review",
                payment.gateway_payment_id,
                order.pk,
            )
            if order.status == PaymentOrder.Status.CREATED:
                order.status = PaymentOrder.Status.REVIEW
                order.save(update_fields=["status"])
            return order

        if order.status == PaymentOrder.Status.PAID:
            paid_by = order.invoice.gateway_payment_id if hasattr(order, "invoice") else None
            if paid_by and paid_by != payment.gateway_payment_id and not payment.is_duplicate:
                payment.is_duplicate = True
                payment.save(update_fields=["is_duplicate", "updated_at"])
                logger.warning(
                    "Order %s received a second captured payment %s; refund it",
                    order.pk,
                    payment.gateway_payment_id,
                )
            return order
        if order.status == PaymentOrder.Status.REVIEW:
            return order

        _fulfil(order, payment)
        return order


def _record_payment(order, entity):
    status_value = _PAYMENT_STATUSES.get(entity.get("status"), Payment.Status.FAILED)
    defaults = {
        "order": order,
        "amount_minor": int(entity.get("amount") or 0),
        "currency": entity.get("currency") or "",
        "method": (entity.get("method") or "")[:30],
        "error_code": (entity.get("error_code") or "")[:100],
        "error_reason": (entity.get("error_reason") or "")[:200],
    }
    payment, created = Payment.objects.select_for_update().get_or_create(
        gateway_payment_id=entity["id"], defaults={**defaults, "status": status_value}
    )
    if created:
        if status_value == Payment.Status.CAPTURED:
            payment.captured_at = timezone.now()
            payment.save(update_fields=["captured_at"])
        return payment

    # Never move a payment backwards (e.g. a late "authorized" after "captured").
    order_of = [
        Payment.Status.FAILED,
        Payment.Status.AUTHORIZED,
        Payment.Status.CAPTURED,
        Payment.Status.PARTIALLY_REFUNDED,
        Payment.Status.REFUNDED,
    ]
    if order_of.index(status_value) > order_of.index(payment.status):
        payment.status = status_value
        if status_value == Payment.Status.CAPTURED and payment.captured_at is None:
            payment.captured_at = timezone.now()
    payment.method = defaults["method"] or payment.method
    payment.save()
    return payment


def _fulfil(order, payment):
    """Create the subscription and invoice for a verified, matching payment."""
    user_model = get_user_model()
    # Serialise activations per user so stacked renewals get distinct periods.
    user = user_model.objects.select_for_update().get(pk=order.user_id)
    version = order.plan_version
    now = timezone.now()
    latest_end = Subscription.objects.filter(
        user=user, status=Subscription.Status.ACTIVE, ends_at__gt=now
    ).aggregate(latest=Max("ends_at"))["latest"]
    # Manual renewal: a purchase made during an active period starts when it ends.
    starts_at = max(now, latest_end) if latest_end else now
    subscription = Subscription.objects.create(
        user=user,
        plan=version.plan,
        plan_version=version,
        source=Subscription.Source.PAYMENT,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=version.duration_days),
    )
    order.status = PaymentOrder.Status.PAID
    order.paid_at = now
    order.subscription = subscription
    order.save(update_fields=["status", "paid_at", "subscription"])
    _issue_invoice(order, payment, subscription, user)
    logger.info("Order %s paid; subscription %s activated", order.pk, subscription.pk)


def _next_invoice_number(now):
    year = timezone.localtime(now).year
    try:
        with transaction.atomic():
            InvoiceCounter.objects.get_or_create(year=year)
    except IntegrityError:
        pass  # Created concurrently.
    counter = InvoiceCounter.objects.select_for_update().get(year=year)
    counter.last_number += 1
    counter.save(update_fields=["last_number"])
    prefix = settings.INVOICE_SETTINGS["NUMBER_PREFIX"]
    return f"{prefix}-{year}-{counter.last_number:06d}"


def _issue_invoice(order, payment, subscription, user):
    seller = settings.INVOICE_SETTINGS
    now = timezone.now()
    version = order.plan_version
    return Invoice.objects.create(
        order=order,
        user=user,
        number=_next_invoice_number(now),
        issued_at=now,
        buyer_name=user.full_name,
        buyer_email=user.email,
        plan_name=version.name,
        plan_version=version.version,
        duration_days=version.duration_days,
        period_start=subscription.starts_at,
        period_end=subscription.ends_at,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        gateway=order.gateway,
        gateway_payment_id=payment.gateway_payment_id,
        seller_name=seller["SELLER_NAME"],
        seller_address=seller["SELLER_ADDRESS"],
        seller_email=seller["SELLER_EMAIL"],
        seller_tax_id=seller["SELLER_TAX_ID"],
    )


def _revoke(order, reason):
    subscription = order.subscription
    if subscription is None or subscription.status == Subscription.Status.REVOKED:
        return
    subscription.status = Subscription.Status.REVOKED
    subscription.revoked_at = timezone.now()
    subscription.revoke_reason = reason[:200]
    subscription.save(update_fields=["status", "revoked_at", "revoke_reason"])
    logger.info("Subscription %s revoked: %s", subscription.pk, reason)


_REFUND_STATUSES = {
    "processed": Refund.Status.PROCESSED,
    "pending": Refund.Status.PENDING,
    "created": Refund.Status.PENDING,
    "failed": Refund.Status.FAILED,
}


def apply_refund(entity, *, reason="", requested_by=None):
    """Record a refund entity and apply the refund policy. Returns the Refund or None."""
    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update()
            .select_related("order")
            .filter(gateway_payment_id=entity.get("payment_id"))
            .first()
        )
        if payment is None:
            return None
        status_value = _REFUND_STATUSES.get(entity.get("status"), Refund.Status.PENDING)
        refund, created = Refund.objects.select_for_update().get_or_create(
            gateway_refund_id=entity["id"],
            defaults={
                "payment": payment,
                "amount_minor": int(entity.get("amount") or 0),
                "currency": entity.get("currency") or payment.currency,
                "status": status_value,
                "reason": reason[:200],
                "requested_by": requested_by,
            },
        )
        if not created and refund.status != Refund.Status.PROCESSED:
            refund.status = status_value
        if refund.status == Refund.Status.PROCESSED and refund.processed_at is None:
            refund.processed_at = timezone.now()
        refund.save()

        refunded = sum(
            payment.refunds.filter(status=Refund.Status.PROCESSED).values_list(
                "amount_minor", flat=True
            )
        )
        payment.amount_refunded_minor = min(refunded, payment.amount_minor)
        if refunded >= payment.amount_minor:
            payment.status = Payment.Status.REFUNDED
        elif refunded > 0:
            payment.status = Payment.Status.PARTIALLY_REFUNDED
        payment.save(update_fields=["amount_refunded_minor", "status", "updated_at"])

        order = payment.order
        if payment.status == Payment.Status.REFUNDED and not payment.is_duplicate:
            _revoke(order, "Payment refunded in full")
        return refund


def apply_dispute(event_type, entity):
    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update()
            .select_related("order")
            .filter(gateway_payment_id=entity.get("payment_id"))
            .first()
        )
        if payment is None:
            return None
        payment.dispute_status = (entity.get("status") or event_type.rsplit(".", 1)[-1])[:30]
        payment.save(update_fields=["dispute_status", "updated_at"])
        if event_type == "payment.dispute.lost":
            _revoke(payment.order, "Payment dispute lost (chargeback)")
        return payment


def request_refund(payment, *, amount_minor=None, reason="", requested_by=None):
    """
    Administrator-initiated refund through the gateway. The payment row stays
    locked from the amount check until the refund is recorded, so two requests
    at once (a double click, two administrators) can't both refund it.
    """
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        remaining = payment.amount_minor - payment.amount_refunded_minor
        amount = amount_minor or remaining
        if payment.status not in (Payment.Status.CAPTURED, Payment.Status.PARTIALLY_REFUNDED):
            raise ValueError("Only captured payments can be refunded.")
        if amount <= 0 or amount > remaining:
            raise ValueError("The refund amount exceeds what can still be refunded.")
        entity = gateway_module.get_gateway().refund(
            payment.gateway_payment_id,
            amount_minor=amount,
            notes={"prepwise_order": str(payment.order_id), "reason": reason[:200]},
        )
        entity.setdefault("payment_id", payment.gateway_payment_id)
        return apply_refund(entity, reason=reason, requested_by=requested_by)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def _entity(payload, name):
    return ((payload.get("payload") or {}).get(name) or {}).get("entity") or {}


def _dispatch(event_type, payload):
    """Apply one verified event. Returns True if it was relevant to PrepWise."""
    if event_type in ("payment.authorized", "payment.captured", "payment.failed", "order.paid"):
        entity = _entity(payload, "payment")
        order = PaymentOrder.objects.filter(gateway_order_id=entity.get("order_id")).first()
        if order is None or not entity.get("id"):
            return False
        apply_payment(order, entity)
        return True
    if event_type in ("refund.created", "refund.processed", "refund.failed"):
        return apply_refund(_entity(payload, "refund")) is not None
    if event_type.startswith("payment.dispute."):
        return apply_dispute(event_type, _entity(payload, "dispute")) is not None
    return False


def handle_webhook(body, *, signature, event_id):
    """
    Verify and apply a webhook delivery. Returns the WebhookEvent. A repeated
    delivery of a processed event is a no-op. Processing errors are recorded
    and re-raised so the gateway retries the delivery.
    """
    gateway = gateway_module.get_gateway()
    if not gateway.verify_webhook_signature(body, signature):
        logger.warning("Rejected a webhook with an invalid signature")
        raise InvalidWebhook()
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise InvalidWebhook() from exc
    if not isinstance(payload, dict):
        raise InvalidWebhook()

    event_type = str(payload.get("event") or "")[:60]
    event_id = (event_id or "")[:100] or hashlib.sha256(body).hexdigest()

    try:
        with transaction.atomic():
            event, created = WebhookEvent.objects.get_or_create(
                event_id=event_id,
                defaults={
                    "gateway": gateway.name,
                    "event_type": event_type,
                    "status": WebhookEvent.Status.FAILED,
                    "payload": payload,
                    "attempts": 0,
                },
            )
    except IntegrityError:
        event = WebhookEvent.objects.get(event_id=event_id)

    with transaction.atomic():
        # Serialise concurrent deliveries of the same event.
        event = WebhookEvent.objects.select_for_update().get(pk=event.pk)
        if event.status in (WebhookEvent.Status.PROCESSED, WebhookEvent.Status.IGNORED):
            return event
        event.attempts += 1
        try:
            with transaction.atomic():
                relevant = _dispatch(event_type, payload)
        except Exception as exc:
            event.status = WebhookEvent.Status.FAILED
            event.error = type(exc).__name__[:200]
            event.save(update_fields=["status", "error", "attempts"])
            logger.exception("Webhook %s (%s) failed", event_id, event_type)
            failed = exc
        else:
            event.status = (
                WebhookEvent.Status.PROCESSED if relevant else WebhookEvent.Status.IGNORED
            )
            event.error = ""
            event.processed_at = timezone.now()
            event.save(update_fields=["status", "error", "attempts", "processed_at"])
            return event
    raise failed
