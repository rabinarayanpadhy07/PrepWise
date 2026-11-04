"""
Server-side usage enforcement shared by every metered feature.

All AI is platform AI for subscribers. Usage is limited by the user's current
subscription (period and daily allowances, copied from the plan version they
bought, or granted by an administrator) and by the optional global monthly
spending cap.

Concurrency: per-user checks run while holding a row lock on the user's
current subscription. The spending cap is checked while holding the
AIPlatformSettings row lock. Locks are always taken in that order, and requests
that call a provider reserve their worst case first, so concurrent requests
can't overspend an allowance.

Day and month boundaries use settings.TIME_ZONE.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.ai_services.models import AIPlatformSettings
from apps.billing.models import Subscription
from apps.usage import errors
from apps.usage.models import Kind, Source, UsageEvent, UsageReservation

LIMITS = {
    Kind.AI_TOKENS: (
        "ai_token_quota",
        "daily_ai_token_limit",
        errors.TokenAllowanceExhausted,
        errors.DailyTokenLimitReached,
    ),
    Kind.INTERVIEW: (
        "interview_quota",
        "daily_interview_limit",
        errors.InterviewAllowanceExhausted,
        errors.DailyInterviewLimitReached,
    ),
    Kind.VOICE_SECONDS: (
        "voice_seconds_quota",
        "daily_voice_seconds_limit",
        errors.VoiceAllowanceExhausted,
        errors.DailyVoiceLimitReached,
    ),
}


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------


def day_window(now=None):
    local = timezone.localtime(now or timezone.now())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def month_window(now=None):
    local = timezone.localtime(now or timezone.now())
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


# ---------------------------------------------------------------------------
# Usage totals (committed events + active reservations)
# ---------------------------------------------------------------------------


def _event_sum(field="quantity", **filters):
    return UsageEvent.objects.filter(**filters).aggregate(total=Sum(field))["total"] or 0


def _held_sum(now, field="quantity", **filters):
    total = UsageReservation.objects.filter(
        status=UsageReservation.Status.RESERVED, expires_at__gt=now, **filters
    ).aggregate(total=Sum(field))["total"]
    return total or 0


def period_usage(subscription, kind, now=None):
    """(committed, held) for a subscription period."""
    now = now or timezone.now()
    return (
        _event_sum(subscription=subscription, kind=kind),
        _held_sum(now, subscription=subscription, kind=kind),
    )


def daily_usage(user, kind, now=None):
    """(committed, held) for today."""
    now = now or timezone.now()
    start, end = day_window(now)
    window = {"user": user, "source": Source.PLATFORM, "kind": kind}
    window.update(created_at__gte=start, created_at__lt=end)
    return _event_sum(**window), _held_sum(now, **window)


def month_platform_spend(now=None):
    """Estimated platform-paid spend this month, including in-flight reservations."""
    now = now or timezone.now()
    start, end = month_window(now)
    committed = _event_sum(
        "estimated_cost", source=Source.PLATFORM, created_at__gte=start, created_at__lt=end
    )
    held = _held_sum(now, "reserved_cost", source=Source.PLATFORM)
    return Decimal(committed) + Decimal(held)


# ---------------------------------------------------------------------------
# Subscription status
# ---------------------------------------------------------------------------


def current_subscription(user, *, lock=False, now=None):
    """The active subscription covering `now` that ends soonest, or None."""
    queryset = Subscription.objects.current(now).filter(user=user).order_by("ends_at", "id")
    if lock:
        # Lock only the subscription row: PostgreSQL can't lock the nullable side
        # of the outer join to plan_version.
        queryset = queryset.select_for_update(of=("self",))
    return queryset.select_related("plan", "plan_version").first()


def require_subscription(user, *, lock=False, now=None):
    subscription = current_subscription(user, lock=lock, now=now)
    if subscription is not None:
        return subscription
    had_one = Subscription.objects.filter(
        user=user, status=Subscription.Status.ACTIVE, ends_at__lte=now or timezone.now()
    ).exists()
    raise errors.SubscriptionExpired() if had_one else errors.SubscriptionRequired()


def _limit_details(kind, scope, limit, used, requested, resets_at):
    return {
        "kind": kind,
        "scope": scope,
        "limit": limit,
        "used": used,
        "requested": requested,
        "resets_at": resets_at.isoformat() if resets_at else None,
    }


def check_platform_allowance(subscription, kind, amount, now=None):
    """Raise a UsageError unless `amount` more units fit the period and daily limits."""
    now = now or timezone.now()
    total_field, daily_field, total_error, daily_error = LIMITS[kind]

    limit = getattr(subscription, total_field)
    used = sum(period_usage(subscription, kind, now))
    if used + amount > limit:
        raise total_error(
            limit=_limit_details(kind, "period", limit, used, amount, subscription.ends_at)
        )

    daily_limit = getattr(subscription, daily_field)
    if daily_limit is not None:
        used_today = sum(daily_usage(subscription.user, kind, now))
        if used_today + amount > daily_limit:
            raise daily_error(
                limit=_limit_details(
                    kind, "daily", daily_limit, used_today, amount, day_window(now)[1]
                )
            )


def check_platform_budget(cost, now=None):
    """Lock the platform settings row and check the monthly spending cap (if set)."""
    platform = AIPlatformSettings.load(lock=True)
    if platform.monthly_spend_cap is None:
        return platform
    if month_platform_spend(now) + Decimal(cost) > platform.monthly_spend_cap:
        raise errors.PlatformBudgetExhausted()
    return platform


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


def _reserve(user, *, subscription, source, kind, quantity, now, ttl_seconds, **fields):
    return UsageReservation.objects.create(
        user=user,
        subscription=subscription,
        source=source,
        kind=kind,
        quantity=quantity,
        expires_at=now + timedelta(seconds=ttl_seconds),
        **fields,
    )


def reserve_platform_tokens(user, *, tokens, cost, feature, provider, model, now=None):
    """Hold `tokens` of the user's allowance and `cost` of the monthly budget."""
    now = now or timezone.now()
    with transaction.atomic():
        subscription = require_subscription(user, lock=True, now=now)
        check_platform_allowance(subscription, Kind.AI_TOKENS, tokens, now)
        platform = check_platform_budget(cost, now)
        return _reserve(
            user,
            subscription=subscription,
            source=Source.PLATFORM,
            kind=Kind.AI_TOKENS,
            quantity=tokens,
            reserved_cost=cost,
            feature=feature,
            provider=provider,
            model=model,
            now=now,
            ttl_seconds=platform.reservation_ttl_seconds,
        )


def reserve_interview(user, *, now=None):
    """Hold one interview before generating questions; commit once the session exists."""
    now = now or timezone.now()
    platform = AIPlatformSettings.load()
    with transaction.atomic():
        subscription = require_subscription(user, lock=True, now=now)
        check_platform_allowance(subscription, Kind.INTERVIEW, 1, now)
        return _reserve(
            user,
            subscription=subscription,
            source=Source.PLATFORM,
            kind=Kind.INTERVIEW,
            quantity=1,
            feature="interview_session",
            now=now,
            ttl_seconds=platform.reservation_ttl_seconds,
        )


def commit_reservation(reservation, *, quantity=None, **event_fields):
    """
    Record actual usage for a reservation. Idempotent: committing twice returns
    the first event. A reservation that expired while its request was still
    running is still committed, so provider-reported usage is never lost.
    """
    with transaction.atomic():
        reservation = UsageReservation.objects.select_for_update().get(pk=reservation.pk)
        if reservation.status == UsageReservation.Status.COMMITTED:
            return UsageEvent.objects.filter(reservation=reservation).first()
        if reservation.status == UsageReservation.Status.RELEASED:
            raise ValueError("A released reservation can't be committed.")

        quantity = reservation.quantity if quantity is None else quantity
        event = None
        if quantity > 0:
            event_fields.setdefault("feature", reservation.feature)
            event_fields.setdefault("provider", reservation.provider)
            event_fields.setdefault("model", reservation.model)
            event = UsageEvent.objects.create(
                user_id=reservation.user_id,
                subscription_id=reservation.subscription_id,
                reservation=reservation,
                kind=reservation.kind,
                source=reservation.source,
                quantity=quantity,
                **event_fields,
            )
        reservation.status = UsageReservation.Status.COMMITTED
        reservation.settled_at = timezone.now()
        reservation.save(update_fields=["status", "settled_at"])
        return event


def release_reservation(reservation):
    """Give back an unused hold (the request failed before using anything)."""
    UsageReservation.objects.filter(
        pk=reservation.pk,
        status__in=[UsageReservation.Status.RESERVED, UsageReservation.Status.EXPIRED],
    ).update(status=UsageReservation.Status.RELEASED, settled_at=timezone.now())


def expire_stale_reservations(now=None):
    """Housekeeping: mark holds past their expiry. Totals already ignore them."""
    now = now or timezone.now()
    return UsageReservation.objects.filter(
        status=UsageReservation.Status.RESERVED, expires_at__lte=now
    ).update(status=UsageReservation.Status.EXPIRED)


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------


def charge_voice(user, *, seconds, now=None):
    """Check and record recorded voice time in one locked transaction."""
    now = now or timezone.now()
    platform = AIPlatformSettings.load()
    seconds = max(1, min(int(seconds), platform.voice_max_answer_seconds))
    with transaction.atomic():
        subscription = require_subscription(user, lock=True, now=now)
        check_platform_allowance(subscription, Kind.VOICE_SECONDS, seconds, now)
        return UsageEvent.objects.create(
            user=user,
            subscription=subscription,
            kind=Kind.VOICE_SECONDS,
            source=Source.PLATFORM,
            quantity=seconds,
            feature="voice_answer",
        )


def voice_recording_limit(user, now=None):
    """
    Longest voice answer the user may record now. Subscribers with plan voice
    time left are held to what's left; everyone else (no plan, or plan voice
    time used up) may record up to one answer's maximum, uncharged.
    """
    available = voice_seconds_available(user, now=now)
    return available if available > 0 else AIPlatformSettings.load().voice_max_answer_seconds


def voice_seconds_available(user, now=None):
    """Seconds the user may still record now, capped at one answer's maximum."""
    now = now or timezone.now()
    subscription = current_subscription(user, now=now)
    if subscription is None:
        return 0
    remaining = _remaining(subscription, Kind.VOICE_SECONDS, now)
    return min(remaining, AIPlatformSettings.load().voice_max_answer_seconds)


def _remaining(subscription, kind, now):
    total_field, daily_field, _, _ = LIMITS[kind]
    remaining = getattr(subscription, total_field) - sum(period_usage(subscription, kind, now))
    daily_limit = getattr(subscription, daily_field)
    if daily_limit is not None:
        used_today = sum(daily_usage(subscription.user, kind, now))
        remaining = min(remaining, daily_limit - used_today)
    return max(remaining, 0)


# ---------------------------------------------------------------------------
# Summaries for the dashboard and settings
# ---------------------------------------------------------------------------


def subscription_summary(subscription, now=None):
    """Plan terms, period and daily usage for a platform subscription."""
    if subscription is None:
        return None
    now = now or timezone.now()
    reset_at = day_window(now)[1]
    usage = {}
    for kind, (total_field, daily_field, _, _) in LIMITS.items():
        committed, held = period_usage(subscription, kind, now)
        limit = getattr(subscription, total_field)
        entry = {
            "period": {
                "limit": limit,
                "used": committed,
                "in_progress": held,
                "remaining": max(limit - committed - held, 0),
            },
            "daily": None,
        }
        daily_limit = getattr(subscription, daily_field)
        if daily_limit is not None:
            d_committed, d_held = daily_usage(subscription.user, kind, now)
            entry["daily"] = {
                "limit": daily_limit,
                "used": d_committed,
                "in_progress": d_held,
                "remaining": max(daily_limit - d_committed - d_held, 0),
                "resets_at": reset_at,
            }
        usage[kind] = entry
    return {
        "id": subscription.pk,
        "plan_code": subscription.plan.code,
        "plan_name": subscription.plan_version.name
        if subscription.plan_version
        else subscription.plan.name,
        "source": subscription.source,
        "starts_at": subscription.starts_at,
        "ends_at": subscription.ends_at,
        "usage": usage,
    }
