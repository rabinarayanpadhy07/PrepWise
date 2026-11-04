"""
One-time purchases of a plan through the payment gateway.

  PaymentOrder  created by the backend from the stored plan-version price
  Payment       a gateway payment attempt against an order (verified data only)
  Refund        a full or partial refund of a captured payment
  WebhookEvent  each gateway webhook delivery, deduplicated by event ID
  Invoice       issued once, when a verified payment activates a subscription
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class PaymentOrder(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Awaiting payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Could not be created"
        REVIEW = "review", "Needs review (payment did not match the order)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payment_orders"
    )
    plan_version = models.ForeignKey(
        "billing.PlanVersion", on_delete=models.PROTECT, related_name="orders"
    )
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    gateway = models.CharField(max_length=20)
    gateway_order_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CREATED)
    subscription = models.OneToOneField(
        "billing.Subscription",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="order",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_minor__gt=0), name="payment_order_amount_positive"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["created", "paid", "failed", "review"]),
                name="payment_order_status_valid",
            ),
            models.CheckConstraint(
                condition=~Q(status="paid")
                | (Q(paid_at__isnull=False) & Q(subscription__isnull=False)),
                name="payment_order_paid_has_subscription",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="payment_order_user_idx"),
        ]

    def __str__(self):
        return f"Order {self.pk} ({self.status})"

    @property
    def receipt(self):
        # Gateway receipts are limited to 40 characters.
        return self.pk.hex


class Payment(models.Model):
    class Status(models.TextChoices):
        AUTHORIZED = "authorized", "Authorized"
        CAPTURED = "captured", "Captured"
        FAILED = "failed", "Failed"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially refunded"
        REFUNDED = "refunded", "Refunded"

    order = models.ForeignKey(PaymentOrder, on_delete=models.PROTECT, related_name="payments")
    gateway_payment_id = models.CharField(max_length=64, unique=True)
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=20, choices=Status.choices)
    method = models.CharField(max_length=30, blank=True)
    amount_refunded_minor = models.PositiveIntegerField(default=0)
    # Gateway failure code/reason (enumerated values, no card data).
    error_code = models.CharField(max_length=100, blank=True)
    error_reason = models.CharField(max_length=200, blank=True)
    dispute_status = models.CharField(max_length=30, blank=True)
    # True when this captured payment arrived after the order was already paid
    # by another payment; it should be refunded.
    is_duplicate = models.BooleanField(default=False)
    captured_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        "authorized",
                        "captured",
                        "failed",
                        "partially_refunded",
                        "refunded",
                    ]
                ),
                name="payment_status_valid",
            ),
        ]

    def __str__(self):
        return f"{self.gateway_payment_id} ({self.status})"


class Refund(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    gateway_refund_id = models.CharField(max_length=64, unique=True)
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=20, choices=Status.choices)
    reason = models.CharField(max_length=200, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["pending", "processed", "failed"]),
                name="refund_status_valid",
            ),
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name="refund_amount_positive"),
        ]

    def __str__(self):
        return f"{self.gateway_refund_id} ({self.status})"


class WebhookEvent(models.Model):
    class Status(models.TextChoices):
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed (will be retried by the gateway)"

    gateway = models.CharField(max_length=20)
    event_id = models.CharField(max_length=100, unique=True)
    event_type = models.CharField(max_length=60)
    status = models.CharField(max_length=20, choices=Status.choices)
    payload = models.JSONField()
    attempts = models.PositiveIntegerField(default=1)
    error = models.CharField(max_length=200, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.event_type} {self.event_id} ({self.status})"


class Invoice(models.Model):
    """
    Issued when a verified payment activates a subscription. All details are
    snapshotted so the document never changes. PrepWise does not calculate tax.
    """

    order = models.OneToOneField(PaymentOrder, on_delete=models.PROTECT, related_name="invoice")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    number = models.CharField(max_length=40, unique=True)
    issued_at = models.DateTimeField()
    buyer_name = models.CharField(max_length=150, blank=True)
    buyer_email = models.EmailField()
    plan_name = models.CharField(max_length=100)
    plan_version = models.PositiveIntegerField()
    duration_days = models.PositiveSmallIntegerField()
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    gateway = models.CharField(max_length=20)
    gateway_payment_id = models.CharField(max_length=64)
    seller_name = models.CharField(max_length=150)
    seller_address = models.TextField(blank=True)
    seller_email = models.CharField(max_length=150, blank=True)
    seller_tax_id = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["-issued_at"]

    def __str__(self):
        return self.number


class InvoiceCounter(models.Model):
    """Per-year sequence for gap-free invoice numbers (row-locked while issuing)."""

    year = models.PositiveSmallIntegerField(primary_key=True)
    last_number = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.year}: {self.last_number}"
