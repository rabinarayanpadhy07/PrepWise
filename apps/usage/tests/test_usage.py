"""
Server-side usage enforcement: allowances, daily resets, reservations,
reconciliation, the global spending cap, idempotency keys, and concurrency.
"""

import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from apps.ai_services.models import AIPlatformSettings
from apps.billing.models import Subscription
from apps.usage import errors, idempotency
from apps.usage import services as usage
from apps.usage.models import Kind, Source, UsageEvent, UsageReservation

pytestmark = pytest.mark.django_db


@pytest.fixture
def budget(db):
    def _set(cap="100", **fields):
        AIPlatformSettings.objects.update_or_create(
            pk=1,
            defaults={"monthly_spend_cap": Decimal(cap) if cap is not None else None, **fields},
        )

    _set()
    return _set


def reserve_tokens(user, tokens, cost="0.01", now=None):
    return usage.reserve_platform_tokens(
        user,
        tokens=tokens,
        cost=Decimal(cost),
        feature="test",
        provider="openai",
        model="test-model",
        now=now,
    )


def record(user, subscription, kind, quantity, *, source=Source.PLATFORM, at=None, cost=None):
    event = UsageEvent.objects.create(
        user=user,
        subscription=subscription if source == Source.PLATFORM else None,
        kind=kind,
        source=source,
        quantity=quantity,
        estimated_cost=cost,
    )
    if at is not None:
        UsageEvent.objects.filter(pk=event.pk).update(created_at=at)
    return event


# ---------------------------------------------------------------------------
# Subscription status
# ---------------------------------------------------------------------------


def test_platform_usage_requires_a_subscription(user, budget):
    with pytest.raises(errors.SubscriptionRequired) as info:
        reserve_tokens(user, 10)
    assert info.value.default_code == "subscription_required"


def test_expired_subscription_is_reported_as_expired(user, subscribe, budget):
    now = timezone.now()
    subscribe(user, starts_at=now - timedelta(days=31), ends_at=now - timedelta(seconds=1))

    with pytest.raises(errors.SubscriptionExpired):
        reserve_tokens(user, 10)


def test_subscription_stops_working_exactly_at_expiry(user, subscribe, budget):
    subscription = subscribe(user)
    reserve_tokens(user, 10, now=subscription.ends_at - timedelta(seconds=1))

    with pytest.raises(errors.SubscriptionExpired):
        reserve_tokens(user, 10, now=subscription.ends_at)


@pytest.mark.parametrize("state", [Subscription.Status.REVOKED, Subscription.Status.CANCELLED])
def test_revoked_or_cancelled_subscriptions_give_no_access(user, subscribe, budget, state):
    subscribe(user, status=state)

    with pytest.raises(errors.SubscriptionRequired):
        reserve_tokens(user, 10)


def test_queued_renewal_takes_over_when_the_current_period_ends(user, subscribe, budget):
    current = subscribe(user, ai_token_quota=100)
    renewal = subscribe(user, starts_at=current.ends_at, ai_token_quota=100)
    reserve_tokens(user, 100)

    with pytest.raises(errors.TokenAllowanceExhausted):
        reserve_tokens(user, 1)
    later = reserve_tokens(user, 100, now=current.ends_at + timedelta(minutes=1))
    assert later.subscription == renewal


# ---------------------------------------------------------------------------
# Period and daily allowances
# ---------------------------------------------------------------------------


def test_token_allowance_boundary(user, subscribe, budget):
    subscribe(user, ai_token_quota=1000)

    reserve_tokens(user, 999)
    reserve_tokens(user, 1)  # exactly at the limit
    with pytest.raises(errors.TokenAllowanceExhausted) as info:
        reserve_tokens(user, 1)
    limit = info.value.error_extra["limit"]
    assert limit == {
        "kind": "ai_tokens",
        "scope": "period",
        "limit": 1000,
        "used": 1000,
        "requested": 1,
        "resets_at": limit["resets_at"],
    }


def test_daily_token_limit_resets_at_midnight(user, subscribe, budget):
    subscription = subscribe(user, daily_ai_token_limit=500)
    today_start, tomorrow = usage.day_window()
    record(user, subscription, Kind.AI_TOKENS, 500, at=today_start + timedelta(minutes=1))

    with pytest.raises(errors.DailyTokenLimitReached) as info:
        reserve_tokens(user, 1)
    assert info.value.error_extra["limit"]["resets_at"] == tomorrow.isoformat()

    # Yesterday's usage doesn't count today.
    UsageEvent.objects.update(created_at=today_start - timedelta(minutes=1))
    reserve_tokens(user, 500)


def test_interview_allowance_and_daily_limit(user, subscribe, budget):
    subscribe(user, interview_quota=3, daily_interview_limit=2)

    # Started interviews commit their reservation (see interviews.services).
    usage.commit_reservation(usage.reserve_interview(user))
    usage.commit_reservation(usage.reserve_interview(user))
    with pytest.raises(errors.DailyInterviewLimitReached):
        usage.reserve_interview(user)

    tomorrow = usage.day_window()[1] + timedelta(hours=1)
    usage.commit_reservation(usage.reserve_interview(user, now=tomorrow))
    with pytest.raises(errors.InterviewAllowanceExhausted):
        usage.reserve_interview(user, now=tomorrow)


def test_voice_allowance_is_charged_and_clamped(user, subscribe, budget):
    budget(voice_max_answer_seconds=60)
    subscribe(user, voice_seconds_quota=100)

    event = usage.charge_voice(user, seconds=500)
    assert event.quantity == 60  # one answer's maximum
    assert usage.voice_seconds_available(user) == 40
    usage.charge_voice(user, seconds=40)
    with pytest.raises(errors.VoiceAllowanceExhausted):
        usage.charge_voice(user, seconds=1)
    assert usage.voice_seconds_available(user) == 0


# ---------------------------------------------------------------------------
# Reservations and reconciliation
# ---------------------------------------------------------------------------


def test_reservations_hold_allowance_until_settled(user, subscribe, budget):
    subscribe(user, ai_token_quota=1000)
    held = reserve_tokens(user, 800)

    with pytest.raises(errors.TokenAllowanceExhausted):
        reserve_tokens(user, 300)

    usage.commit_reservation(held, quantity=200, input_tokens=150, output_tokens=50)
    reserve_tokens(user, 800)  # unused part of the hold was given back


def test_released_reservations_free_the_allowance(user, subscribe, budget):
    subscribe(user, ai_token_quota=1000)
    held = reserve_tokens(user, 1000)

    usage.release_reservation(held)

    reserve_tokens(user, 1000)
    assert not UsageEvent.objects.exists()


def test_commit_is_idempotent_and_records_one_event(user, subscribe, budget):
    subscribe(user)
    held = reserve_tokens(user, 500)

    first = usage.commit_reservation(held, quantity=300)
    second = usage.commit_reservation(held, quantity=999)

    assert first == second
    assert UsageEvent.objects.get().quantity == 300


def test_released_reservation_cannot_be_committed(user, subscribe, budget):
    subscribe(user)
    held = reserve_tokens(user, 500)
    usage.release_reservation(held)

    with pytest.raises(ValueError):
        usage.commit_reservation(held, quantity=10)


def test_commit_with_zero_usage_records_nothing(user, subscribe, budget):
    subscribe(user)
    held = reserve_tokens(user, 500)

    assert usage.commit_reservation(held, quantity=0) is None
    assert not UsageEvent.objects.exists()


def test_abandoned_reservations_stop_counting_after_expiry(user, subscribe, budget):
    budget(reservation_ttl_seconds=60)
    subscribe(user, ai_token_quota=1000)
    held = reserve_tokens(user, 1000)

    later = timezone.now() + timedelta(seconds=61)
    reserve_tokens(user, 1000, now=later)

    out = StringIO()
    UsageReservation.objects.filter(pk=held.pk).update(expires_at=timezone.now())
    call_command("expire_usage_reservations", stdout=out)
    assert "Expired 1 reservation(s)" in out.getvalue()
    held.refresh_from_db()
    assert held.status == UsageReservation.Status.EXPIRED


def test_late_commit_after_expiry_still_records_actual_usage(user, subscribe, budget):
    subscribe(user)
    held = reserve_tokens(user, 500)
    usage.expire_stale_reservations(now=timezone.now() + timedelta(days=1))

    event = usage.commit_reservation(held, quantity=420)

    assert event.quantity == 420


# ---------------------------------------------------------------------------
# Global platform spending cap
# ---------------------------------------------------------------------------


def test_spending_cap_counts_committed_and_in_flight_platform_cost(
    user, other_user, subscribe, budget
):
    budget("1.00")
    subscribe(user)
    subscription = subscribe(other_user)
    record(other_user, subscription, Kind.AI_TOKENS, 10, cost=Decimal("0.60"))
    reserve_tokens(user, 10, cost="0.30")

    with pytest.raises(errors.PlatformBudgetExhausted) as info:
        reserve_tokens(user, 10, cost="0.11")
    assert info.value.status_code == 503
    reserve_tokens(user, 10, cost="0.10")  # exactly at the cap


def test_spending_cap_ignores_previous_months(user, subscribe, budget):
    budget("1.00")
    subscription = subscribe(user)
    month_start = usage.month_window()[0]
    record(
        user,
        subscription,
        Kind.AI_TOKENS,
        10,
        cost=Decimal("50"),
        at=month_start - timedelta(minutes=1),
    )

    reserve_tokens(user, 10, cost="1.00")


def test_no_spending_cap_means_no_platform_budget_limit(user, subscribe, budget):
    budget(None)
    subscription = subscribe(user)
    record(user, subscription, Kind.AI_TOKENS, 10, cost=Decimal("1000000"))

    reserve_tokens(user, 10, cost="500")


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def test_subscription_summary_reports_period_daily_and_in_progress(user, subscribe, budget):
    subscription = subscribe(user, ai_token_quota=1000, daily_ai_token_limit=400)
    record(user, subscription, Kind.AI_TOKENS, 100)
    reserve_tokens(user, 50)

    tokens = usage.subscription_summary(subscription)["usage"]["ai_tokens"]

    assert tokens["period"] == {"limit": 1000, "used": 100, "in_progress": 50, "remaining": 850}
    assert tokens["daily"]["remaining"] == 250
    assert tokens["daily"]["limit"] == 400


# ---------------------------------------------------------------------------
# Idempotency keys
# ---------------------------------------------------------------------------


def test_idempotency_key_returns_the_first_result(user):
    record_, existing = idempotency.claim(user, "scope", "key-1")
    assert existing is None
    idempotency.complete(record_, 42)

    _, existing = idempotency.claim(user, "scope", "key-1")
    assert existing == "42"


def test_idempotency_key_in_progress_is_a_conflict(user):
    idempotency.claim(user, "scope", "key-1")

    with pytest.raises(idempotency.RequestInProgress):
        idempotency.claim(user, "scope", "key-1")


def test_abandoned_idempotency_key_can_be_retried(user):
    record_, _ = idempotency.claim(user, "scope", "key-1")
    idempotency.abandon(record_)

    _, existing = idempotency.claim(user, "scope", "key-1")
    assert existing is None


def test_idempotency_keys_are_per_user(user, other_user):
    record_, _ = idempotency.claim(user, "scope", "key-1")
    idempotency.complete(record_, 1)

    _, existing = idempotency.claim(other_user, "scope", "key-1")
    assert existing is None


# ---------------------------------------------------------------------------
# Concurrency: separate database connections racing for the same allowance
# ---------------------------------------------------------------------------


def race(workers, action):
    """Run `action(index)` in parallel threads; return (successes, usage errors)."""
    barrier = threading.Barrier(workers)
    results = []
    lock = threading.Lock()

    def target(index):
        try:
            barrier.wait()
            action(index)
            outcome = "ok"
        except errors.UsageError as exc:
            outcome = exc.default_code
        finally:
            connection.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results.count("ok"), [r for r in results if r != "ok"]


def concurrent(test):
    """Real commits so each thread's connection sees the others' rows."""
    test = pytest.mark.usefixtures("seeded_transactional_db")(test)
    return pytest.mark.django_db(transaction=True)(test)


@concurrent
def test_concurrent_token_reservations_cannot_exceed_the_allowance(user, subscribe, budget):
    subscribe(user, ai_token_quota=10_000)

    ok, refused = race(8, lambda _: reserve_tokens(user, 3_000))

    assert ok == 3
    assert set(refused) == {"token_allowance_exhausted"}
    assert sum(UsageReservation.objects.values_list("quantity", flat=True)) == 9_000


@concurrent
def test_concurrent_interview_starts_cannot_exceed_the_allowance(user, subscribe, budget):
    subscribe(user, interview_quota=2)

    ok, refused = race(6, lambda _: usage.reserve_interview(user))

    assert ok == 2
    assert set(refused) == {"interview_allowance_exhausted"}


@concurrent
def test_concurrent_voice_charges_cannot_exceed_the_allowance(user, subscribe, budget):
    subscribe(user, voice_seconds_quota=100)

    ok, _ = race(6, lambda _: usage.charge_voice(user, seconds=30))

    assert ok == 3
    assert sum(UsageEvent.objects.values_list("quantity", flat=True)) == 90


@concurrent
def test_concurrent_users_cannot_exceed_the_global_spending_cap(make_user, subscribe, budget):
    budget("1.00")
    users = [make_user(email=f"racer{i}@example.com") for i in range(6)]
    for racer in users:
        subscribe(racer)

    ok, refused = race(6, lambda i: reserve_tokens(users[i], 10, cost="0.30"))

    assert ok == 3
    assert set(refused) == {"platform_budget_exhausted"}
