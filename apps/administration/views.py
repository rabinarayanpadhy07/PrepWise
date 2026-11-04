"""
Administrator API (/api/v1/admin/). Every view requires an active user with
the administrator role (ADMIN_PERMISSIONS); other users get 403 and anonymous
requests 401. Every change is recorded in the audit log.

Never exposed here: OpenAI API keys (only their last four characters), gateway secrets,
encryption keys, interview answers, resume text, or webhook payloads.
"""

import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import (
    Avg,
    Count,
    DecimalField,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, TruncDate
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.generics import (
    ListAPIView,
    ListCreateAPIView,
    RetrieveUpdateAPIView,
    get_object_or_404,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdminRole
from apps.accounts.security import revoke_all_refresh_tokens
from apps.administration import audit
from apps.administration.models import AuditLogEntry
from apps.administration.serializers import (
    AdminAIModelSerializer,
    AdminAIModeSerializer,
    AdminAIPlatformSettingsSerializer,
    AdminOrderSerializer,
    AdminPlanSerializer,
    AdminPlatformKeyCreateSerializer,
    AdminPlatformKeySerializer,
    AdminPlatformKeyUpdateSerializer,
    AdminSubscriptionSerializer,
    AdminUserSerializer,
    AdminWebhookEventSerializer,
    AuditLogEntrySerializer,
    ProviderCallLogSerializer,
    RefundRequestSerializer,
    SubscriptionGrantSerializer,
    SubscriptionUpdateSerializer,
)
from apps.ai_services import service as ai_service
from apps.ai_services.crypto import decrypt_api_key, encrypt_api_key
from apps.ai_services.errors import AIServiceError
from apps.ai_services.models import (
    OPENAI,
    PROVIDER_VALUES,
    AIMode,
    AIModelConfig,
    AIPlatformSettings,
    PlatformAPIKey,
    Provider,
    ProviderCallLog,
)
from apps.billing.models import ALLOWANCE_FIELDS, Plan, Subscription
from apps.interviews.models import AnswerEvaluation, InterviewAnswer, InterviewSession
from apps.payments import services as payment_services
from apps.payments.invoices import render_invoice_pdf
from apps.payments.models import Invoice, Payment, PaymentOrder, Refund, WebhookEvent
from apps.resumes.models import Resume, ResumeAnalysis
from apps.usage import services as usage
from apps.usage.models import Kind, Source, UsageEvent

User = get_user_model()

ADMIN_PERMISSIONS = [IsAuthenticated, IsAdminRole]
MAX_WINDOW_DAYS = 366


class AdminAPIView(APIView):
    permission_classes = ADMIN_PERMISSIONS


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "This change isn't possible in the current state."


def _search(request):
    return request.query_params.get("search", "").strip()[:100]


def _window(request):
    """(start, end) from ?days=N (default 30, at most 366), ending now."""
    raw = request.query_params.get("days", "30")
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValidationError({"days": ["Enter a whole number of days."]}) from exc
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise ValidationError({"days": [f"Choose between 1 and {MAX_WINDOW_DAYS} days."]})
    end = timezone.now()
    return end - timedelta(days=days), end, days


def _save_with_audit(request, serializer, action, fields):
    """Save a validated update serializer and audit the fields that changed."""
    before = audit.snapshot(serializer.instance, fields)
    reason = serializer.validated_data.pop("reason", "")
    with transaction.atomic():
        instance = serializer.save()
        changes = audit.diff(before, audit.snapshot(instance, fields))
        if changes:
            audit.record(request, action, instance, changes, reason=reason)
    return instance


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@extend_schema(tags=["administration"], responses=OpenApiTypes.OBJECT)
class OverviewView(AdminAPIView):
    """Headline numbers computed from stored records. Nothing here is sampled or estimated
    except platform AI spend, which is labelled as an estimate."""

    def get(self, request, *args, **kwargs):
        now = timezone.now()
        month_start, _ = usage.month_window(now)
        platform = AIPlatformSettings.load()
        paid = PaymentOrder.objects.filter(
            status=PaymentOrder.Status.PAID, paid_at__gte=month_start
        )
        refunded = Refund.objects.filter(
            status=Refund.Status.PROCESSED, processed_at__gte=month_start
        ).aggregate(total=Coalesce(Sum("amount_minor"), 0))["total"]
        calls = ProviderCallLog.objects.filter(created_at__gte=now - timedelta(hours=24))
        call_count = calls.count()
        error_count = calls.exclude(outcome=ProviderCallLog.SUCCESS).count()
        return Response(
            {
                "users": {
                    "total": User.objects.count(),
                    "active": User.objects.filter(is_active=True).count(),
                    "suspended": User.objects.filter(is_active=False).count(),
                    "new_this_month": User.objects.filter(date_joined__gte=month_start).count(),
                },
                "subscriptions": {
                    "active": Subscription.objects.current(now).values("user").distinct().count(),
                    "upcoming": Subscription.objects.upcoming(now).count(),
                },
                "revenue_this_month": {
                    # All plans are INR today; amounts are what the gateway captured.
                    "currency": "INR",
                    "paid_orders": paid.count(),
                    "gross_minor": paid.aggregate(total=Coalesce(Sum("amount_minor"), 0))["total"],
                    "refunded_minor": refunded,
                },
                "platform_ai": {
                    "currency": platform.cost_currency,
                    "monthly_spend_cap": platform.monthly_spend_cap,
                    "estimated_spend_this_month": usage.month_platform_spend(now),
                },
                "provider_calls_24h": {"total": call_count, "errors": error_count},
                "payments_needing_review": PaymentOrder.objects.filter(
                    status=PaymentOrder.Status.REVIEW
                ).count()
                + Payment.objects.filter(is_duplicate=True)
                .exclude(status=Payment.Status.REFUNDED)
                .count(),
            }
        )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def _users_queryset():
    current = Subscription.objects.current().filter(user=OuterRef("pk")).order_by("-ends_at")
    return User.objects.annotate(
        subscription_ends_at=Subquery(current.values("ends_at")[:1]),
    )


@extend_schema(
    tags=["administration"],
    parameters=[
        OpenApiParameter("search", str, description="Filter by email or name."),
        OpenApiParameter("role", str, enum=["user", "admin"]),
        OpenApiParameter("status", str, enum=["active", "suspended"]),
    ],
)
class AdminUserListView(ListAPIView):
    serializer_class = AdminUserSerializer
    permission_classes = ADMIN_PERMISSIONS

    def get_queryset(self):
        queryset = _users_queryset().order_by("-date_joined", "-id")
        search = _search(self.request)
        if search:
            queryset = queryset.filter(Q(email__icontains=search) | Q(full_name__icontains=search))
        role = self.request.query_params.get("role")
        if role in User.Role.values:
            queryset = queryset.filter(role=role)
        state = self.request.query_params.get("status")
        if state == "active":
            queryset = queryset.filter(is_active=True)
        elif state == "suspended":
            queryset = queryset.filter(is_active=False)
        return queryset


USER_FIELDS = ["role", "is_active"]


@extend_schema(tags=["administration"])
class AdminUserDetailView(RetrieveUpdateAPIView):
    serializer_class = AdminUserSerializer
    permission_classes = ADMIN_PERMISSIONS
    http_method_names = ["get", "patch", "options"]

    def get_queryset(self):
        return _users_queryset()

    def perform_update(self, serializer):
        user = _save_with_audit(self.request, serializer, "user.update", USER_FIELDS)
        if not user.is_active:
            # Suspended users lose every session immediately (access tokens are
            # also rejected for inactive users on each request).
            revoke_all_refresh_tokens(user)


@extend_schema(tags=["administration"], responses=OpenApiTypes.OBJECT)
class AdminUserSummaryView(AdminAPIView):
    """Account facts for support: counts, masked keys, subscriptions and usage.
    Interview answers and resume text are deliberately not included."""

    def get(self, request, pk, *args, **kwargs):
        user = get_object_or_404(User, pk=pk)
        now = timezone.now()
        subscriptions = Subscription.objects.filter(user=user).select_related(
            "plan", "plan_version", "order", "user"
        )[:20]
        totals = (
            UsageEvent.objects.filter(user=user)
            .values("source", "kind")
            .annotate(quantity=Sum("quantity"), estimated_cost=Sum("estimated_cost"))
            .order_by("source", "kind")
        )
        return Response(
            {
                "counts": {
                    "resumes": Resume.objects.filter(owner=user).count(),
                    "analyses": ResumeAnalysis.objects.filter(owner=user).count(),
                    "interviews": InterviewSession.objects.filter(owner=user).count(),
                    "answers": InterviewAnswer.objects.filter(session__owner=user).count(),
                    "evaluations": AnswerEvaluation.objects.filter(session__owner=user).count(),
                    "orders": PaymentOrder.objects.filter(user=user).count(),
                },
                "subscriptions": AdminSubscriptionSerializer(subscriptions, many=True).data,
                "current_usage": usage.subscription_summary(
                    usage.current_subscription(user, now=now), now
                ),
                "lifetime_usage": list(totals),
            }
        )


@extend_schema(
    tags=["administration"],
    request=serializers.Serializer,
    responses=AdminUserSerializer,
)
class AdminRevokeSessionsView(AdminAPIView):
    """Sign the user out everywhere now: refresh and access tokens stop working."""

    def post(self, request, pk, *args, **kwargs):
        user = get_object_or_404(_users_queryset(), pk=pk)
        if user.pk == request.user.pk:
            raise ValidationError("Use Log out to end your own sessions.")
        reason = str(request.data.get("reason", ""))[:300]
        with transaction.atomic():
            user.sessions_revoked_at = timezone.now()
            user.save(update_fields=["sessions_revoked_at"])
            revoke_all_refresh_tokens(user)
            audit.record(request, "user.revoke_sessions", user, reason=reason)
        return Response(AdminUserSerializer(user, context={"request": request}).data)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

SUBSCRIPTION_FIELDS = ["status", "ends_at", *ALLOWANCE_FIELDS]


@extend_schema(
    tags=["administration"],
    parameters=[
        OpenApiParameter("search", str, description="User email."),
        OpenApiParameter("user", int),
        OpenApiParameter(
            "state", str, enum=["current", "upcoming", "ended", "revoked", "cancelled"]
        ),
    ],
)
class AdminSubscriptionListView(ListCreateAPIView):
    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminSubscriptionSerializer

    def get_queryset(self):
        now = timezone.now()
        queryset = Subscription.objects.select_related("user", "plan", "plan_version", "order")
        search = _search(self.request)
        if search:
            queryset = queryset.filter(user__email__icontains=search)
        user_id = self.request.query_params.get("user", "")
        if user_id.isdigit():
            queryset = queryset.filter(user_id=int(user_id))
        state = self.request.query_params.get("state")
        if state == "current":
            queryset = queryset.current(now)
        elif state == "upcoming":
            queryset = queryset.upcoming(now)
        elif state == "ended":
            queryset = queryset.filter(status=Subscription.Status.ACTIVE, ends_at__lte=now)
        elif state in (Subscription.Status.REVOKED, Subscription.Status.CANCELLED):
            queryset = queryset.filter(status=state)
        return queryset.order_by("-created_at", "-id")

    @extend_schema(
        request=SubscriptionGrantSerializer, responses={201: AdminSubscriptionSerializer}
    )
    def post(self, request, *args, **kwargs):
        serializer = SubscriptionGrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        plan = data["plan_code"]
        version = plan.current_version
        starts_at = data.get("starts_at") or timezone.now()
        duration = data.get("duration_days") or version.duration_days
        with transaction.atomic():
            subscription = Subscription.objects.create(
                user=data["user_id"],
                plan=plan,
                plan_version=version,
                source=Subscription.Source.ADMIN,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(days=duration),
            )
            audit.record(
                request,
                "subscription.grant",
                subscription,
                audit.snapshot(subscription, ["user", "starts_at", "ends_at", *ALLOWANCE_FIELDS]),
                reason=data.get("reason", ""),
            )
        subscription = Subscription.objects.select_related("user", "plan", "plan_version").get(
            pk=subscription.pk
        )
        return Response(
            AdminSubscriptionSerializer(subscription).data, status=status.HTTP_201_CREATED
        )


@extend_schema(tags=["administration"])
class AdminSubscriptionDetailView(RetrieveUpdateAPIView):
    permission_classes = ADMIN_PERMISSIONS
    http_method_names = ["get", "patch", "options"]
    queryset = Subscription.objects.select_related("user", "plan", "plan_version")

    def get_serializer_class(self):
        if self.request.method == "PATCH":
            return SubscriptionUpdateSerializer
        return AdminSubscriptionSerializer

    @extend_schema(request=SubscriptionUpdateSerializer, responses=AdminSubscriptionSerializer)
    def patch(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = SubscriptionUpdateSerializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        subscription = _save_with_audit(
            request, serializer, "subscription.update", SUBSCRIPTION_FIELDS
        )
        return Response(AdminSubscriptionSerializer(subscription).data)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

PLAN_FIELDS = [
    "name",
    "description",
    "price_minor",
    "duration_days",
    *ALLOWANCE_FIELDS,
    "is_active",
    "display_order",
]


@extend_schema(tags=["administration"])
class AdminPlanListView(ListAPIView):
    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminPlanSerializer
    pagination_class = None
    queryset = Plan.objects.prefetch_related("versions").order_by("display_order", "id")


@extend_schema(tags=["administration"])
class AdminPlanDetailView(RetrieveUpdateAPIView):
    """Changes apply to future purchases only (a new plan version is created)."""

    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminPlanSerializer
    lookup_field = "code"
    http_method_names = ["get", "patch", "options"]
    queryset = Plan.objects.all()

    def perform_update(self, serializer):
        _save_with_audit(self.request, serializer, "plan.update", PLAN_FIELDS)


# ---------------------------------------------------------------------------
# AI configuration
# ---------------------------------------------------------------------------

MODEL_FIELDS = [
    "model_id",
    "display_name",
    "is_enabled",
    "max_input_tokens",
    "max_output_tokens",
    "input_price_per_million",
    "output_price_per_million",
    "fallback",
]
MODE_FIELDS = [
    "description",
    "is_enabled",
    "model",
    "allow_fallback",
    "sort_order",
]
SETTINGS_FIELDS = [
    "cost_currency",
    "monthly_spend_cap",
    "voice_max_answer_seconds",
    "reservation_ttl_seconds",
]


def _test_key(api_key):
    """Ask OpenAI whether it accepts `api_key`. Returns (outcome code, latency ms)."""
    started = time.monotonic()
    try:
        ai_service.build_provider(OPENAI, api_key=api_key, model="").verify_key()
        outcome = ProviderCallLog.SUCCESS
    except AIServiceError as exc:
        outcome = exc.default_code
    return outcome, int((time.monotonic() - started) * 1000)


class _KeyTestThrottleMixin:
    """Calls that reach OpenAI share the "ai" rate limit."""

    throttle_scope = "ai"

    def get_throttles(self):
        if self.request.method == "POST":
            return [ScopedRateThrottle()]
        return super().get_throttles()


@extend_schema(tags=["administration"])
class AdminPlatformKeyListView(_KeyTestThrottleMixin, AdminAPIView):
    """
    The platform's OpenAI API keys, in the order they're used. POST adds a key
    after OpenAI accepts it; it's stored encrypted and never returned or logged.
    """

    @extend_schema(responses=AdminPlatformKeySerializer(many=True))
    def get(self, request, *args, **kwargs):
        keys = PlatformAPIKey.objects.select_related("created_by")
        return Response(AdminPlatformKeySerializer(keys, many=True).data)

    @extend_schema(
        request=AdminPlatformKeyCreateSerializer, responses={201: AdminPlatformKeySerializer}
    )
    def post(self, request, *args, **kwargs):
        body = AdminPlatformKeyCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        api_key = body.validated_data["api_key"]
        if PlatformAPIKey.objects.filter(key_hint=api_key[-4:]).exists() and any(
            _decrypts_to(stored, api_key) for stored in PlatformAPIKey.objects.all()
        ):
            raise Conflict("This key has already been added.")
        outcome, _latency = _test_key(api_key)
        if outcome != ProviderCallLog.SUCCESS:
            raise ValidationError({"api_key": [_KEY_TEST_MESSAGES.get(outcome, _KEY_TEST_FAILED)]})

        with transaction.atomic():
            last = PlatformAPIKey.objects.order_by("-priority").values_list("priority", flat=True)
            key = PlatformAPIKey.objects.create(
                label=body.validated_data.get("label", "").strip(),
                encrypted_key=encrypt_api_key(api_key),
                key_hint=api_key[-4:],
                priority=(last.first() or 0) + 1,
                created_by=request.user,
            )
            audit.record(
                request,
                "platform_key.create",
                key,
                # Only the last four characters are recorded, never the key.
                {"key_hint": [None, key.key_hint], "label": [None, key.label]},
                reason=body.validated_data.get("reason", ""),
                label=str(key),
            )
        return Response(AdminPlatformKeySerializer(key).data, status=status.HTTP_201_CREATED)


def _decrypts_to(stored, api_key):
    try:
        return decrypt_api_key(stored.encrypted_key) == api_key
    except AIServiceError:
        return False


_KEY_TEST_FAILED = "OpenAI didn't accept this key. Check it and try again."
_KEY_TEST_MESSAGES = {
    "ai_key_rejected": "OpenAI rejected this key. Check that it's a valid OpenAI API key.",
    "ai_rate_limited": "OpenAI says this key is over its limit or out of credit. Check the key's billing and try again.",  # noqa: E501
    "ai_provider_unavailable": "Couldn't reach OpenAI to check the key. Try again shortly.",
}

KEY_FIELDS = ["label", "is_enabled", "priority", "status", "cooldowns"]


@extend_schema(tags=["administration"])
class AdminPlatformKeyDetailView(AdminAPIView):
    """Change a key's label, order or enabled state (PATCH), or remove it (DELETE)."""

    def _key(self, pk):
        key = PlatformAPIKey.objects.select_related("created_by").filter(pk=pk).first()
        if key is None:
            raise NotFound("API key not found.")
        return key

    @extend_schema(request=AdminPlatformKeyUpdateSerializer, responses=AdminPlatformKeySerializer)
    def patch(self, request, pk, *args, **kwargs):
        body = AdminPlatformKeyUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        with transaction.atomic():
            key = PlatformAPIKey.objects.select_for_update().get(pk=self._key(pk).pk)
            before = audit.snapshot(key, KEY_FIELDS)
            for field in ("label", "is_enabled", "priority"):
                if field in data:
                    setattr(key, field, data[field].strip() if field == "label" else data[field])
            if data.get("reactivate"):
                key.status = PlatformAPIKey.Status.ACTIVE
                key.cooldowns = {}
            if data.get("clear_cooldowns"):
                key.cooldowns = {}
            key.save()
            changes = audit.diff(before, audit.snapshot(key, KEY_FIELDS))
            if changes:
                audit.record(
                    request,
                    "platform_key.update",
                    key,
                    changes,
                    reason=data.get("reason", ""),
                    label=str(key),
                )
        return Response(AdminPlatformKeySerializer(key).data)

    @extend_schema(request=None, responses={204: None})
    def delete(self, request, pk, *args, **kwargs):
        key = self._key(pk)
        with transaction.atomic():
            audit.record(
                request,
                "platform_key.delete",
                key,
                {"key_hint": [key.key_hint, None]},
                reason=request.query_params.get("reason", "")[:300],
                label=str(key),
            )
            key.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["administration"], request=None, responses=AdminPlatformKeySerializer)
class AdminPlatformKeyTestView(_KeyTestThrottleMixin, AdminAPIView):
    """Ask OpenAI whether a saved key still works, and update its status."""

    def post(self, request, pk, *args, **kwargs):
        key = PlatformAPIKey.objects.filter(pk=pk).first()
        if key is None:
            raise NotFound("API key not found.")
        try:
            api_key = decrypt_api_key(key.encrypted_key)
        except AIServiceError as exc:
            outcome, latency = exc.default_code, 0
        else:
            outcome, latency = _test_key(api_key)
        ProviderCallLog.objects.create(
            provider=OPENAI, api_key=key, feature="key_check", outcome=outcome, latency_ms=latency
        )
        now = timezone.now()
        if outcome == ProviderCallLog.SUCCESS:
            key.status = PlatformAPIKey.Status.ACTIVE
            key.last_error = ""
            key.last_error_at = None
        else:
            if outcome in ("ai_key_rejected", "stored_key_unreadable"):
                key.status = PlatformAPIKey.Status.REJECTED
            key.last_error = outcome
            key.last_error_at = now
        key.save(update_fields=["status", "last_error", "last_error_at", "updated_at"])
        audit.record(
            request, "platform_key.test", key, {"outcome": [None, outcome]}, label=str(key)
        )
        return Response(
            {**AdminPlatformKeySerializer(key).data, "ok": outcome == ProviderCallLog.SUCCESS}
        )


@extend_schema(tags=["administration"])
class AdminAIModelListView(ListCreateAPIView):
    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminAIModelSerializer
    pagination_class = None
    queryset = AIModelConfig.objects.order_by("provider", "display_name")

    def perform_create(self, serializer):
        reason = serializer.validated_data.pop("reason", "")
        with transaction.atomic():
            model = serializer.save()
            audit.record(
                self.request,
                "ai_model.create",
                model,
                audit.diff({}, audit.snapshot(model, ["provider", *MODEL_FIELDS])),
                reason=reason,
            )


@extend_schema(tags=["administration"])
class AdminAIModelDetailView(RetrieveUpdateAPIView):
    """Models are disabled rather than deleted, so past usage keeps its reference."""

    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminAIModelSerializer
    http_method_names = ["get", "patch", "options"]
    queryset = AIModelConfig.objects.all()

    def perform_update(self, serializer):
        _save_with_audit(self.request, serializer, "ai_model.update", MODEL_FIELDS)


@extend_schema(tags=["administration"])
class AdminAIModeListView(ListAPIView):
    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminAIModeSerializer
    pagination_class = None
    queryset = AIMode.objects.order_by("sort_order", "id")


@extend_schema(tags=["administration"])
class AdminAIModeDetailView(RetrieveUpdateAPIView):
    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminAIModeSerializer
    lookup_field = "key"
    http_method_names = ["get", "patch", "options"]
    queryset = AIMode.objects.all()

    def perform_update(self, serializer):
        _save_with_audit(self.request, serializer, "ai_mode.update", MODE_FIELDS)


@extend_schema(tags=["administration"])
class AdminAISettingsView(RetrieveUpdateAPIView):
    """Global platform limits: optional spending cap, voice limits, reservations."""

    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminAIPlatformSettingsSerializer
    http_method_names = ["get", "patch", "options"]

    def get_object(self):
        return AIPlatformSettings.load()

    def perform_update(self, serializer):
        _save_with_audit(self.request, serializer, "ai_settings.update", SETTINGS_FIELDS)


# ---------------------------------------------------------------------------
# Usage and estimated costs
# ---------------------------------------------------------------------------

_COST = DecimalField(max_digits=18, decimal_places=6)


@extend_schema(
    tags=["administration"],
    parameters=[
        OpenApiParameter("days", int, description="Window ending now (1-366, default 30).")
    ],
    responses=OpenApiTypes.OBJECT,
)
class AdminUsageSummaryView(AdminAPIView):
    """Aggregate usage from the ledger. Token counts are provider-reported; costs are
    estimates from configured prices (models priced at 0 count as 0)."""

    def get(self, request, *args, **kwargs):
        start, end, days = _window(request)
        events = UsageEvent.objects.filter(created_at__gte=start, created_at__lt=end)
        tokens = events.filter(kind=Kind.AI_TOKENS)

        def totals(source):
            rows = tokens.filter(source=source).aggregate(
                calls=Count("id"),
                tokens=Coalesce(Sum("quantity"), 0),
                input_tokens=Coalesce(Sum("input_tokens"), 0),
                output_tokens=Coalesce(Sum("output_tokens"), 0),
                estimated_cost=Sum("estimated_cost"),
            )
            other = events.filter(source=source)
            rows["interviews"] = other.filter(kind=Kind.INTERVIEW).aggregate(
                total=Coalesce(Sum("quantity"), 0)
            )["total"]
            rows["voice_seconds"] = other.filter(kind=Kind.VOICE_SECONDS).aggregate(
                total=Coalesce(Sum("quantity"), 0)
            )["total"]
            return rows

        by_model = list(
            tokens.values("source", "provider", "model")
            .annotate(
                calls=Count("id"),
                tokens=Sum("quantity"),
                estimated_cost=Sum("estimated_cost"),
            )
            .order_by("source", "-tokens")
        )
        by_feature = list(
            tokens.values("source", "feature")
            .annotate(
                calls=Count("id"), tokens=Sum("quantity"), estimated_cost=Sum("estimated_cost")
            )
            .order_by("source", "-tokens")
        )
        daily = (
            tokens.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(
                platform_tokens=Coalesce(Sum("quantity", filter=Q(source=Source.PLATFORM)), 0),
                platform_estimated_cost=Sum("estimated_cost", filter=Q(source=Source.PLATFORM)),
            )
            .order_by("day")
        )
        platform = AIPlatformSettings.load()
        return Response(
            {
                "window": {"start": start, "end": end, "days": days},
                "cost_currency": platform.cost_currency,
                "platform": totals(Source.PLATFORM),
                "by_model": by_model,
                "by_feature": by_feature,
                "daily": list(daily),
                "budget": {
                    "monthly_spend_cap": platform.monthly_spend_cap,
                    "estimated_spend_this_month": usage.month_platform_spend(),
                },
            }
        )


class AdminUserUsageSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    full_name = serializers.CharField()
    platform_tokens = serializers.IntegerField()
    platform_estimated_cost = serializers.DecimalField(max_digits=18, decimal_places=6)
    platform_interviews = serializers.IntegerField()
    voice_seconds = serializers.IntegerField()


@extend_schema(
    tags=["administration"],
    parameters=[
        OpenApiParameter("days", int),
        OpenApiParameter("search", str),
        OpenApiParameter("ordering", str, enum=["platform_cost", "platform_tokens"]),
    ],
)
class AdminUserUsageView(ListAPIView):
    """Per-user usage in the window (paginated). Users without usage are omitted."""

    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminUserUsageSerializer
    ORDERING = {
        "platform_cost": "-platform_estimated_cost",
        "platform_tokens": "-platform_tokens",
    }

    def get_queryset(self):
        start, end, _ = _window(self.request)
        in_window = Q(usage_events__created_at__gte=start, usage_events__created_at__lt=end)

        def total(field, **filters):
            condition = in_window & Q(**{f"usage_events__{k}": v for k, v in filters.items()})
            output = _COST if field == "estimated_cost" else None
            kwargs = {"output_field": output} if output else {}
            return Coalesce(Sum(f"usage_events__{field}", filter=condition), Value(0), **kwargs)

        queryset = User.objects.annotate(
            platform_tokens=total("quantity", source=Source.PLATFORM, kind=Kind.AI_TOKENS),
            platform_estimated_cost=total("estimated_cost", source=Source.PLATFORM),
            platform_interviews=total("quantity", source=Source.PLATFORM, kind=Kind.INTERVIEW),
            voice_seconds=total("quantity", kind=Kind.VOICE_SECONDS),
            events=Count("usage_events", filter=in_window),
        ).filter(events__gt=0)
        search = _search(self.request)
        if search:
            queryset = queryset.filter(email__icontains=search)
        ordering = self.ORDERING.get(
            self.request.query_params.get("ordering", ""), "-platform_estimated_cost"
        )
        return queryset.order_by(ordering, "id")


# ---------------------------------------------------------------------------
# Payments, invoices and refunds
# ---------------------------------------------------------------------------


def _orders():
    return PaymentOrder.objects.select_related("user", "plan_version", "invoice").prefetch_related(
        "payments__refunds__requested_by"
    )


@extend_schema(
    tags=["administration"],
    parameters=[
        OpenApiParameter("search", str, description="User email or gateway order ID."),
        OpenApiParameter("status", str, enum=["created", "paid", "failed", "review"]),
        OpenApiParameter("refunded", bool),
    ],
)
class AdminOrderListView(ListAPIView):
    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminOrderSerializer

    def get_queryset(self):
        queryset = _orders()
        search = _search(self.request)
        if search:
            queryset = queryset.filter(
                Q(user__email__icontains=search) | Q(gateway_order_id__icontains=search)
            )
        order_status = self.request.query_params.get("status")
        if order_status in PaymentOrder.Status.values:
            queryset = queryset.filter(status=order_status)
        if self.request.query_params.get("refunded") == "true":
            queryset = queryset.filter(payments__amount_refunded_minor__gt=0).distinct()
        return queryset.order_by("-created_at")


@extend_schema(tags=["administration"], responses=AdminOrderSerializer)
class AdminOrderDetailView(AdminAPIView):
    def get(self, request, pk, *args, **kwargs):
        return Response(AdminOrderSerializer(get_object_or_404(_orders(), pk=pk)).data)


@extend_schema(
    tags=["administration"], request=RefundRequestSerializer, responses=AdminOrderSerializer
)
class AdminRefundView(AdminAPIView):
    """Refund a captured payment through the gateway. A full refund revokes the
    subscription it bought (see docs/payments.md)."""

    throttle_scope = "payments"

    def get_throttles(self):
        return [ScopedRateThrottle()]

    def post(self, request, payment_id, *args, **kwargs):
        payment = get_object_or_404(Payment, gateway_payment_id=payment_id)
        serializer = RefundRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            refund = payment_services.request_refund(
                payment,
                amount_minor=data.get("amount_minor"),
                reason=data["reason"],
                requested_by=request.user,
            )
        except ValueError as exc:
            raise ValidationError({"amount_minor": [str(exc)]}) from exc
        audit.record(
            request,
            "payment.refund",
            payment,
            {
                "refund_id": [None, refund.gateway_refund_id if refund else None],
                "amount_minor": [None, refund.amount_minor if refund else data.get("amount_minor")],
                "status": [None, refund.status if refund else None],
            },
            reason=data["reason"],
            label=f"{payment.gateway_payment_id} ({payment.order.user.email})",
        )
        return Response(
            AdminOrderSerializer(get_object_or_404(_orders(), pk=payment.order_id)).data
        )


@extend_schema(
    tags=["administration"],
    responses={(200, "application/pdf"): OpenApiResponse(OpenApiTypes.BINARY)},
)
class AdminInvoicePDFView(AdminAPIView):
    def get(self, request, pk, *args, **kwargs):
        invoice = get_object_or_404(Invoice, pk=pk)
        response = HttpResponse(render_invoice_pdf(invoice), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{invoice.number}.pdf"'
        response["Cache-Control"] = "private, no-store"
        return response


@extend_schema(
    tags=["administration"],
    parameters=[OpenApiParameter("status", str, enum=["processed", "ignored", "failed"])],
)
class AdminWebhookEventListView(ListAPIView):
    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AdminWebhookEventSerializer

    def get_queryset(self):
        queryset = WebhookEvent.objects.defer("payload").order_by("-received_at")
        event_status = self.request.query_params.get("status")
        if event_status in WebhookEvent.Status.values:
            queryset = queryset.filter(status=event_status)
        return queryset


# ---------------------------------------------------------------------------
# Provider health
# ---------------------------------------------------------------------------


@extend_schema(tags=["administration"], responses=OpenApiTypes.OBJECT)
class AdminProviderHealthView(AdminAPIView):
    """Call outcomes per provider over the last 24 hours, from sanitized call logs."""

    def get(self, request, *args, **kwargs):
        since = timezone.now() - timedelta(hours=24)
        result = []
        for provider in PROVIDER_VALUES:
            calls = ProviderCallLog.objects.filter(provider=provider, created_at__gte=since)
            stats = calls.aggregate(
                total=Count("id"),
                errors=Count("id", filter=~Q(outcome=ProviderCallLog.SUCCESS)),
                average_latency_ms=Avg("latency_ms", filter=Q(outcome=ProviderCallLog.SUCCESS)),
            )
            outcomes = list(calls.values("outcome").annotate(count=Count("id")).order_by("-count"))
            everything = ProviderCallLog.objects.filter(provider=provider)
            last_error = everything.exclude(outcome=ProviderCallLog.SUCCESS).first()
            result.append(
                {
                    "provider": provider,
                    "label": Provider(provider).label,
                    "platform_key_configured": ai_service.has_usable_keys(),
                    "keys": {
                        "total": PlatformAPIKey.objects.count(),
                        "usable": ai_service.active_keys().count(),
                    },
                    "last_24h": {
                        **stats,
                        "error_rate": round(stats["errors"] / stats["total"], 4)
                        if stats["total"]
                        else None,
                        "outcomes": outcomes,
                    },
                    "last_success_at": everything.filter(outcome=ProviderCallLog.SUCCESS).aggregate(
                        at=Max("created_at")
                    )["at"],
                    "last_error": {"outcome": last_error.outcome, "at": last_error.created_at}
                    if last_error
                    else None,
                }
            )
        return Response(result)


@extend_schema(
    tags=["administration"],
    parameters=[
        OpenApiParameter("provider", str, enum=PROVIDER_VALUES),
        OpenApiParameter("errors_only", bool),
    ],
)
class AdminProviderCallListView(ListAPIView):
    """Sanitized call log: outcome codes and timings only, no content or identities."""

    permission_classes = ADMIN_PERMISSIONS
    serializer_class = ProviderCallLogSerializer

    def get_queryset(self):
        queryset = ProviderCallLog.objects.all()
        provider = self.request.query_params.get("provider")
        if provider in PROVIDER_VALUES:
            queryset = queryset.filter(provider=provider)
        if self.request.query_params.get("errors_only") == "true":
            queryset = queryset.exclude(outcome=ProviderCallLog.SUCCESS)
        return queryset


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@extend_schema(
    tags=["administration"],
    parameters=[
        OpenApiParameter("action", str, description="Exact action or prefix, e.g. 'plan.'."),
        OpenApiParameter("target_type", str),
        OpenApiParameter("target_id", str),
        OpenApiParameter("search", str, description="Actor email."),
    ],
)
class AdminAuditLogView(ListAPIView):
    permission_classes = ADMIN_PERMISSIONS
    serializer_class = AuditLogEntrySerializer

    def get_queryset(self):
        params = self.request.query_params
        queryset = AuditLogEntry.objects.all()
        action = params.get("action", "").strip()[:60]
        if action:
            queryset = queryset.filter(action__startswith=action)
        if params.get("target_type"):
            queryset = queryset.filter(target_type=params["target_type"][:60])
        if params.get("target_id"):
            queryset = queryset.filter(target_id=params["target_id"][:64])
        search = _search(self.request)
        if search:
            queryset = queryset.filter(actor_email__icontains=search)
        return queryset
