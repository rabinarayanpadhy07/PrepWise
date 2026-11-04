"""
Serializers for the administrator API. API keys and encryption keys are never
returned: saved OpenAI keys appear only as their last four characters. Interview
answers and resume text are never included; user records show counts instead.
"""

import copy
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.administration.models import AuditLogEntry
from apps.ai_services.models import (
    AIMode,
    AIModelConfig,
    AIPlatformSettings,
    PlatformAPIKey,
    ProviderCallLog,
)
from apps.ai_services.serializers import validate_openai_key
from apps.billing.models import ALLOWANCE_FIELDS, Plan, PlanVersion, Subscription
from apps.payments.models import Invoice, Payment, PaymentOrder, Refund, WebhookEvent

User = get_user_model()

# Period quota -> its daily limit; a daily limit above its period quota is a mistake.
DAILY_PAIRS = [
    ("ai_token_quota", "daily_ai_token_limit"),
    ("interview_quota", "daily_interview_limit"),
    ("voice_seconds_quota", "daily_voice_seconds_limit"),
]
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
# Upper bound on a plan price (₹10,00,000) to catch typos in minor units.
MAX_PRICE_MINOR = 100_000_000


def _django_errors(exc):
    """Convert a Django ValidationError to DRF's format."""
    if hasattr(exc, "message_dict"):
        return serializers.ValidationError(exc.message_dict)
    return serializers.ValidationError(exc.messages)


def _check_daily_limits(attrs, instance):
    errors = {}
    for total_field, daily_field in DAILY_PAIRS:
        total = attrs.get(total_field, getattr(instance, total_field, None))
        daily = attrs.get(daily_field, getattr(instance, daily_field, None))
        if total is not None and daily is not None and daily > total:
            errors[daily_field] = ["The daily limit can't be larger than the period allowance."]
    if errors:
        raise serializers.ValidationError(errors)


class ReasonMixin(serializers.Serializer):
    """An optional note stored in the audit log with the change."""

    reason = serializers.CharField(
        max_length=300, required=False, allow_blank=True, write_only=True
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class AdminUserSerializer(ReasonMixin, serializers.ModelSerializer):
    """User record as seen by administrators. Only `role` and `is_active` are editable."""

    subscription_ends_at = serializers.DateTimeField(read_only=True, default=None)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "role",
            "is_active",
            "date_joined",
            "last_login",
            "sessions_revoked_at",
            "subscription_ends_at",
            "reason",
        ]
        read_only_fields = [
            "id",
            "email",
            "full_name",
            "date_joined",
            "last_login",
            "sessions_revoked_at",
        ]

    def validate(self, attrs):
        request = self.context["request"]
        if self.instance is not None and self.instance.pk == request.user.pk:
            # Prevents an administrator from locking themselves (or the last admin) out.
            raise serializers.ValidationError(
                "You can't change your own role or account status.", code="self_modification"
            )
        return attrs


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class AdminSubscriptionSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    plan_code = serializers.CharField(source="plan.code", read_only=True)
    plan_name = serializers.SerializerMethodField()
    plan_version = serializers.IntegerField(source="plan_version.version", read_only=True)
    order_id = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            "id",
            "user_id",
            "user_email",
            "plan_code",
            "plan_name",
            "plan_version",
            "status",
            "source",
            "starts_at",
            "ends_at",
            *ALLOWANCE_FIELDS,
            "revoked_at",
            "revoke_reason",
            "created_at",
            "order_id",
        ]
        read_only_fields = fields

    def get_plan_name(self, obj) -> str:
        return obj.plan_version.name if obj.plan_version else obj.plan.name

    def get_order_id(self, obj) -> str | None:
        order = getattr(obj, "order", None)
        return str(order.pk) if order else None


class SubscriptionGrantSerializer(ReasonMixin):
    """Grant platform AI access without a payment (support, trials, corrections)."""

    user_id = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_active=True))
    plan_code = serializers.SlugRelatedField(slug_field="code", queryset=Plan.objects.all())
    starts_at = serializers.DateTimeField(required=False)
    duration_days = serializers.IntegerField(min_value=1, max_value=366, required=False)


class SubscriptionUpdateSerializer(ReasonMixin, serializers.ModelSerializer):
    """Adjust an existing subscription: end date, status, and its own allowances."""

    status = serializers.ChoiceField(
        choices=[Subscription.Status.ACTIVE, Subscription.Status.CANCELLED], required=False
    )

    class Meta:
        model = Subscription
        fields = ["status", "ends_at", *ALLOWANCE_FIELDS, "reason"]
        extra_kwargs = {field: {"required": False} for field in ["ends_at", *ALLOWANCE_FIELDS]}

    def validate(self, attrs):
        instance = self.instance
        if instance.status == Subscription.Status.REVOKED:
            raise serializers.ValidationError(
                "Revoked subscriptions (refunds, lost disputes) can't be changed. Grant a new one."
            )
        ends_at = attrs.get("ends_at", instance.ends_at)
        if ends_at <= instance.starts_at:
            raise serializers.ValidationError({"ends_at": ["The end must be after the start."]})
        if ends_at - instance.starts_at > timedelta(days=3 * 366):
            raise serializers.ValidationError({"ends_at": ["A period can't exceed three years."]})
        _check_daily_limits(attrs, instance)
        return attrs


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


class PlanVersionSerializer(serializers.ModelSerializer):
    subscription_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = PlanVersion
        fields = [
            "version",
            "name",
            "currency",
            "price_minor",
            "duration_days",
            *ALLOWANCE_FIELDS,
            "created_at",
            "subscription_count",
        ]
        read_only_fields = fields


class AdminPlanSerializer(ReasonMixin, serializers.ModelSerializer):
    """
    Editing price, duration or allowances creates a new plan version; only
    future purchases use it. Existing orders and subscriptions keep theirs.
    """

    versions = serializers.SerializerMethodField()
    price_minor = serializers.IntegerField(min_value=1, max_value=MAX_PRICE_MINOR, required=False)
    duration_days = serializers.IntegerField(min_value=1, max_value=366, required=False)

    class Meta:
        model = Plan
        fields = [
            "code",
            "name",
            "description",
            "currency",
            "price_minor",
            "duration_days",
            "renewal_mode",
            *ALLOWANCE_FIELDS,
            "is_active",
            "display_order",
            "versions",
            "updated_at",
            "reason",
        ]
        read_only_fields = ["code", "currency", "renewal_mode", "versions", "updated_at"]

    def get_versions(self, obj) -> list[dict]:
        versions = obj.versions.order_by("-version")[:10]
        return PlanVersionSerializer(versions, many=True).data

    def validate(self, attrs):
        _check_daily_limits(attrs, self.instance)
        return attrs


# ---------------------------------------------------------------------------
# AI models, modes and platform settings
# ---------------------------------------------------------------------------


def _modes_using(model):
    """Enabled modes served by `model`."""
    return list(AIMode.objects.filter(is_enabled=True, model=model))


class AdminAIModelSerializer(ReasonMixin, serializers.ModelSerializer):
    fallback_id = serializers.PrimaryKeyRelatedField(
        source="fallback",
        queryset=AIModelConfig.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = AIModelConfig
        fields = [
            "id",
            "model_id",
            "display_name",
            "is_enabled",
            "max_input_tokens",
            "max_output_tokens",
            "input_price_per_million",
            "output_price_per_million",
            "fallback_id",
            "created_at",
            "updated_at",
            "reason",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_model_id(self, value):
        value = value.strip()
        if not MODEL_ID_PATTERN.match(value):
            raise serializers.ValidationError(
                "Enter the model identifier exactly as OpenAI documents it, e.g. gpt-5-mini."
            )
        return value

    def validate(self, attrs):
        reason = attrs.pop("reason", None)
        # Validate a copy: the real instance must keep its old values until save,
        # so the audit log can record what changed.
        instance = copy.copy(self.instance) if self.instance is not None else AIModelConfig()
        for field, value in attrs.items():
            setattr(instance, field, value)
        try:
            instance.full_clean(validate_unique=True, validate_constraints=True)
        except DjangoValidationError as exc:
            raise _django_errors(exc) from exc

        if self.instance is not None:
            used_by = _modes_using(self.instance)
            for mode in used_by:
                if not instance.is_enabled:
                    raise serializers.ValidationError(
                        {"is_enabled": [f"{mode} mode uses this model. Change the mode first."]}
                    )
        if reason is not None:
            attrs["reason"] = reason
        return attrs


class AdminAIModeSerializer(ReasonMixin, serializers.ModelSerializer):
    label = serializers.CharField(source="get_key_display", read_only=True)
    model_id = serializers.PrimaryKeyRelatedField(
        source="model", queryset=AIModelConfig.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = AIMode
        fields = [
            "key",
            "label",
            "description",
            "is_enabled",
            "model_id",
            "allow_fallback",
            "sort_order",
            "reason",
        ]
        read_only_fields = ["key", "label"]

    def validate(self, attrs):
        reason = attrs.pop("reason", None)
        mode = copy.copy(self.instance)
        for field, value in attrs.items():
            setattr(mode, field, value)
        try:
            mode.full_clean()
        except DjangoValidationError as exc:
            raise _django_errors(exc) from exc

        if mode.is_enabled:
            if mode.model is None:
                raise serializers.ValidationError(
                    {"is_enabled": ["Choose a model before enabling this mode."]}
                )
            if not mode.model.is_enabled:
                raise serializers.ValidationError(
                    {"is_enabled": [f"{mode.model.display_name} is disabled. Enable it first."]}
                )
        elif (
            mode.key == AIMode.DEFAULT
            and AIMode.objects.filter(is_enabled=True).exclude(key=mode.key).exists()
        ):
            raise serializers.ValidationError(
                {"is_enabled": ["Fast is the default mode. Disable the other modes first."]}
            )
        if reason is not None:
            attrs["reason"] = reason
        return attrs


class AdminAIPlatformSettingsSerializer(ReasonMixin, serializers.ModelSerializer):
    class Meta:
        model = AIPlatformSettings
        fields = [
            "cost_currency",
            "monthly_spend_cap",
            "voice_max_answer_seconds",
            "reservation_ttl_seconds",
            "updated_at",
            "reason",
        ]
        read_only_fields = ["updated_at"]

    def validate_cost_currency(self, value):
        value = value.strip().upper()
        if not CURRENCY_PATTERN.match(value):
            raise serializers.ValidationError("Use a three-letter ISO currency code, e.g. USD.")
        return value

    def validate_voice_max_answer_seconds(self, value):
        if not 10 <= value <= 3600:
            raise serializers.ValidationError("Choose between 10 and 3,600 seconds.")
        return value

    def validate_reservation_ttl_seconds(self, value):
        timeout = settings.AI_SETTINGS["REQUEST_TIMEOUT_SECONDS"]
        if value <= timeout:
            raise serializers.ValidationError(
                f"Must be longer than the AI request timeout ({timeout} seconds), or "
                "reservations could expire while their request is still running."
            )
        return value


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


class AdminRefundSerializer(serializers.ModelSerializer):
    requested_by_email = serializers.EmailField(
        source="requested_by.email", read_only=True, default=None
    )

    class Meta:
        model = Refund
        fields = [
            "gateway_refund_id",
            "amount_minor",
            "currency",
            "status",
            "reason",
            "requested_by_email",
            "created_at",
            "processed_at",
        ]
        read_only_fields = fields


class AdminPaymentSerializer(serializers.ModelSerializer):
    refunds = AdminRefundSerializer(many=True, read_only=True)
    refundable_minor = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "gateway_payment_id",
            "status",
            "amount_minor",
            "currency",
            "method",
            "amount_refunded_minor",
            "refundable_minor",
            "error_code",
            "error_reason",
            "dispute_status",
            "is_duplicate",
            "captured_at",
            "created_at",
            "refunds",
        ]
        read_only_fields = fields

    def get_refundable_minor(self, obj) -> int:
        if obj.status not in (Payment.Status.CAPTURED, Payment.Status.PARTIALLY_REFUNDED):
            return 0
        return obj.amount_minor - obj.amount_refunded_minor


class AdminInvoiceBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = ["id", "number", "issued_at"]
        read_only_fields = fields


class AdminOrderSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    plan_name = serializers.CharField(source="plan_version.name", read_only=True)
    plan_version = serializers.IntegerField(source="plan_version.version", read_only=True)
    invoice = serializers.SerializerMethodField()
    subscription_id = serializers.IntegerField(read_only=True, allow_null=True)
    payments = AdminPaymentSerializer(many=True, read_only=True)

    class Meta:
        model = PaymentOrder
        fields = [
            "id",
            "user_id",
            "user_email",
            "plan_name",
            "plan_version",
            "amount_minor",
            "currency",
            "status",
            "gateway",
            "gateway_order_id",
            "subscription_id",
            "invoice",
            "payments",
            "created_at",
            "paid_at",
        ]
        read_only_fields = fields

    def get_invoice(self, obj) -> dict | None:
        invoice = getattr(obj, "invoice", None)
        return AdminInvoiceBriefSerializer(invoice).data if invoice else None


class RefundRequestSerializer(serializers.Serializer):
    amount_minor = serializers.IntegerField(min_value=1, required=False)
    reason = serializers.CharField(max_length=200)


class AdminWebhookEventSerializer(serializers.ModelSerializer):
    """Delivery metadata only. Payloads contain customer details and are not exposed."""

    class Meta:
        model = WebhookEvent
        fields = [
            "event_id",
            "event_type",
            "status",
            "attempts",
            "error",
            "received_at",
            "processed_at",
        ]
        read_only_fields = fields


class AdminPlatformKeySerializer(serializers.ModelSerializer):
    """A saved OpenAI API key: never the key itself, only its last four characters."""

    created_by_email = serializers.EmailField(
        source="created_by.email", read_only=True, default=None
    )
    cooling_down = serializers.SerializerMethodField()

    class Meta:
        model = PlatformAPIKey
        fields = [
            "id",
            "label",
            "key_hint",
            "is_enabled",
            "priority",
            "status",
            "cooling_down",
            "request_count",
            "last_used_at",
            "last_error",
            "last_error_at",
            "created_at",
            "created_by_email",
        ]
        read_only_fields = fields

    def get_cooling_down(self, obj) -> list:
        """Models this key is resting from after hitting OpenAI's limit: [{model, until}]."""
        rows = []
        for model_id in sorted(obj.cooldowns or {}):
            until = obj.cooling_until(model_id)
            if until is not None:
                rows.append({"model": model_id, "until": until})
        return rows


class AdminPlatformKeyCreateSerializer(ReasonMixin, serializers.Serializer):
    api_key = serializers.CharField(
        write_only=True, max_length=400, trim_whitespace=True, style={"input_type": "password"}
    )
    label = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")

    def validate_api_key(self, value):
        return validate_openai_key(value)


class AdminPlatformKeyUpdateSerializer(ReasonMixin, serializers.Serializer):
    """Change a key's label, order or enabled state; `reactivate` clears a rejection."""

    label = serializers.CharField(max_length=80, required=False, allow_blank=True)
    is_enabled = serializers.BooleanField(required=False)
    priority = serializers.IntegerField(required=False, min_value=0, max_value=100_000)
    reactivate = serializers.BooleanField(required=False, default=False)
    clear_cooldowns = serializers.BooleanField(required=False, default=False)


# ---------------------------------------------------------------------------
# Monitoring and audit
# ---------------------------------------------------------------------------


class ProviderCallLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProviderCallLog
        fields = [
            "id",
            "provider",
            "model",
            "api_key_id",
            "feature",
            "outcome",
            "latency_ms",
            "used_fallback",
            "created_at",
        ]
        read_only_fields = fields


class AuditLogEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLogEntry
        fields = [
            "id",
            "actor_email",
            "action",
            "target_type",
            "target_id",
            "target_label",
            "changes",
            "reason",
            "request_id",
            "created_at",
        ]
        read_only_fields = fields
