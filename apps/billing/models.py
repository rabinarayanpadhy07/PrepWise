from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Max, Q
from django.utils import timezone

# Terms that are snapshotted into a PlanVersion. Changing any of them on a Plan
# creates a new version; existing orders and subscriptions keep theirs.
VERSIONED_FIELDS = (
    "name",
    "currency",
    "price_minor",
    "duration_days",
    "renewal_mode",
    "ai_token_quota",
    "daily_ai_token_limit",
    "interview_quota",
    "daily_interview_limit",
    "voice_seconds_quota",
    "daily_voice_seconds_limit",
)

# Allowances a subscription carries (copied from its plan version).
ALLOWANCE_FIELDS = VERSIONED_FIELDS[5:]


class Currency(models.TextChoices):
    INR = "INR", "Indian rupee"


class RenewalMode(models.TextChoices):
    MANUAL = "manual", "Manual renewal"


class Allowances(models.Model):
    """Platform-paid allowances shared by plans, plan versions and subscriptions."""

    ai_token_quota = models.PositiveBigIntegerField(
        help_text="Platform AI tokens (input + output) included per period."
    )
    daily_ai_token_limit = models.PositiveBigIntegerField(
        null=True, blank=True, help_text="Platform AI tokens per day. Empty means no daily limit."
    )
    interview_quota = models.PositiveIntegerField(
        help_text="Platform-paid mock interviews included per period."
    )
    daily_interview_limit = models.PositiveIntegerField(
        null=True, blank=True, help_text="Interviews per day. Empty means no daily limit."
    )
    voice_seconds_quota = models.PositiveIntegerField(
        help_text="Seconds of recorded voice answers included per period."
    )
    daily_voice_seconds_limit = models.PositiveIntegerField(
        null=True, blank=True, help_text="Voice seconds per day. Empty means no daily limit."
    )

    class Meta:
        abstract = True


class Plan(Allowances):
    """
    A purchasable plan. Prices are in minor units (paise). Plans are paid
    upfront for a fixed number of days and renew only when the user buys again.

    Editing the price, duration or allowances creates a new PlanVersion; only
    future purchases use it.
    """

    Currency = Currency
    RenewalMode = RenewalMode

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.INR)
    price_minor = models.PositiveIntegerField(help_text="Price in minor units, e.g. paise.")
    duration_days = models.PositiveSmallIntegerField()
    renewal_mode = models.CharField(
        max_length=20, choices=RenewalMode.choices, default=RenewalMode.MANUAL
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(price_minor__gt=0), name="billing_plan_price_positive"
            ),
            models.CheckConstraint(
                condition=Q(duration_days__gte=1) & Q(duration_days__lte=366),
                name="billing_plan_duration_range",
            ),
            models.CheckConstraint(
                condition=Q(currency__in=["INR"]), name="billing_plan_currency_valid"
            ),
            models.CheckConstraint(
                condition=Q(renewal_mode__in=["manual"]), name="billing_plan_renewal_mode_valid"
            ),
        ]
        indexes = [
            models.Index(
                fields=["is_active", "display_order"], name="billing_plan_active_order_idx"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            self.ensure_current_version()

    def ensure_current_version(self):
        """Return the version matching the plan's terms, creating one if they changed."""
        latest = self.versions.order_by("-version").first()
        terms = {field: getattr(self, field) for field in VERSIONED_FIELDS}
        if latest is not None and all(getattr(latest, f) == v for f, v in terms.items()):
            return latest
        number = (self.versions.aggregate(top=Max("version"))["top"] or 0) + 1
        return PlanVersion.objects.create(plan=self, version=number, **terms)

    @property
    def current_version(self):
        return self.versions.order_by("-version").first()


class PlanVersion(Allowances):
    """Immutable snapshot of a plan's commercial terms at one point in time."""

    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveIntegerField()
    name = models.CharField(max_length=100)
    currency = models.CharField(max_length=3, choices=Currency.choices)
    price_minor = models.PositiveIntegerField()
    duration_days = models.PositiveSmallIntegerField()
    renewal_mode = models.CharField(max_length=20, choices=RenewalMode.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["plan", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "version"], name="billing_plan_version_unique"),
            models.CheckConstraint(
                condition=Q(price_minor__gt=0), name="billing_plan_version_price_positive"
            ),
        ]

    def __str__(self):
        return f"{self.plan.code} v{self.version}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("Plan versions are immutable. Edit the plan instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Plan versions can't be deleted.")


class SubscriptionQuerySet(models.QuerySet):
    def current(self, at=None):
        at = at or timezone.now()
        return self.filter(status=Subscription.Status.ACTIVE, starts_at__lte=at, ends_at__gt=at)

    def upcoming(self, at=None):
        at = at or timezone.now()
        return self.filter(status=Subscription.Status.ACTIVE, starts_at__gt=at)


class Subscription(Allowances):
    """
    A paid (or administrator-granted) period of platform AI access. There is no
    automatic renewal: each purchase creates a new row. A purchase made while a
    subscription is running starts when that one ends.

    Allowances are copied from the plan version when the row is created, so
    later plan edits never change what an existing subscriber paid for.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled by an administrator"
        REVOKED = "revoked", "Revoked (refund or payment reversal)"

    class Source(models.TextChoices):
        ADMIN = "admin", "Granted by an administrator"
        PAYMENT = "payment", "Payment"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    plan_version = models.ForeignKey(
        PlanVersion, on_delete=models.PROTECT, null=True, blank=True, related_name="subscriptions"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.ADMIN)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(blank=True)
    # Left blank in the admin, these are copied from the plan version on creation.
    ai_token_quota = models.PositiveBigIntegerField(blank=True)
    interview_quota = models.PositiveIntegerField(blank=True)
    voice_seconds_quota = models.PositiveIntegerField(blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SubscriptionQuerySet.as_manager()

    class Meta:
        ordering = ["-starts_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="billing_subscription_period_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["active", "cancelled", "revoked"]),
                name="billing_subscription_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(source__in=["admin", "payment"]),
                name="billing_subscription_source_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status", "ends_at"], name="billing_sub_user_active_idx"),
        ]

    def __str__(self):
        return f"{self.user} - {self.plan.code} until {self.ends_at:%Y-%m-%d}"

    def save(self, *args, **kwargs):
        if self.plan_id and self.plan_version_id is None:
            self.plan_version = self.plan.current_version
        terms = self.plan_version
        if terms is not None:
            if self.ends_at is None:
                self.ends_at = self.starts_at + timedelta(days=terms.duration_days)
            if self._state.adding:
                for field in ALLOWANCE_FIELDS:
                    if getattr(self, field) is None:
                        setattr(self, field, getattr(terms, field))
        super().save(*args, **kwargs)

    @property
    def is_current(self):
        now = timezone.now()
        return self.status == self.Status.ACTIVE and self.starts_at <= now < self.ends_at
