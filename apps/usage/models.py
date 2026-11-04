import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Kind(models.TextChoices):
    AI_TOKENS = "ai_tokens", "AI tokens"
    INTERVIEW = "interview", "Interview session"
    VOICE_SECONDS = "voice_seconds", "Voice seconds"


class Source(models.TextChoices):
    PLATFORM = "platform", "Platform AI (subscription)"


KIND_VALUES = [choice.value for choice in Kind]


class UsageEvent(models.Model):
    """
    Append-only ledger of metered usage. Quotas are computed by summing events
    (plus active reservations), so the ledger is the single source of truth.

    For AI calls, `input_tokens` / `output_tokens` are what the provider reported.
    `estimated_cost` is computed from administrator-entered prices and is an
    estimate, not the provider's invoice.
    """

    Kind = Kind
    Source = Source

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="usage_events"
    )
    # Platform usage always belongs to the subscription it was charged against.
    subscription = models.ForeignKey(
        "billing.Subscription",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="usage_events",
    )
    reservation = models.OneToOneField(
        "usage.UsageReservation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="event",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    source = models.CharField(max_length=20, choices=Source.choices)
    quantity = models.PositiveBigIntegerField()
    input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    output_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    estimated_cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    cost_currency = models.CharField(max_length=3, blank=True)
    feature = models.CharField(max_length=50, blank=True)
    provider = models.CharField(max_length=30, blank=True)
    model = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="usage_event_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=KIND_VALUES), name="usage_event_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(source__in=["platform"]), name="usage_event_source_valid"
            ),
            models.CheckConstraint(
                condition=Q(subscription__isnull=False),
                name="usage_event_platform_has_subscription",
            ),
        ]
        indexes = [
            models.Index(fields=["subscription", "kind"], name="usage_event_sub_kind_idx"),
            models.Index(
                fields=["user", "kind", "source", "created_at"], name="usage_event_user_kind_idx"
            ),
            models.Index(fields=["source", "created_at"], name="usage_event_source_time_idx"),
        ]

    def __str__(self):
        return f"{self.user_id} {self.kind} {self.quantity} ({self.source})"


class UsageReservation(models.Model):
    """
    Allowance held for a request in flight. Platform-paid AI calls reserve their
    worst case (estimated input + maximum output) before calling the provider,
    then commit the provider-reported usage or release the hold on failure.
    Reservations that are never settled stop counting after `expires_at`.
    """

    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        COMMITTED = "committed", "Committed"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="usage_reservations"
    )
    subscription = models.ForeignKey(
        "billing.Subscription",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="usage_reservations",
    )
    source = models.CharField(max_length=20, choices=Source.choices)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    quantity = models.PositiveBigIntegerField()
    reserved_cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    feature = models.CharField(max_length=50, blank=True)
    provider = models.CharField(max_length=30, blank=True)
    model = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RESERVED)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="usage_reservation_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=KIND_VALUES), name="usage_reservation_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["reserved", "committed", "released", "expired"]),
                name="usage_reservation_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(subscription__isnull=False),
                name="usage_reservation_platform_has_subscription",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="usage_res_status_exp_idx"),
            models.Index(fields=["subscription", "status"], name="usage_res_sub_status_idx"),
            models.Index(fields=["user", "status"], name="usage_res_user_status_idx"),
        ]

    def __str__(self):
        return f"{self.kind} {self.quantity} {self.status}"


class IdempotencyRecord(models.Model):
    """
    Remembers client request keys for expensive operations (starting an
    interview, creating an analysis) so a retried request returns the original
    result instead of calling the AI provider again.
    """

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="idempotency_records"
    )
    scope = models.CharField(max_length=50)
    key = models.CharField(max_length=64)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_PROGRESS)
    resource_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "scope", "key"], name="usage_idempotency_unique"
            ),
        ]

    def __str__(self):
        return f"{self.scope}:{self.key} {self.status}"
