from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import Plan, Subscription
from apps.usage.services import current_subscription

pytestmark = pytest.mark.django_db


def test_subscription_copies_period_and_quotas_from_plan(user):
    plan = Plan.objects.get(code="platform-30-day")
    start = timezone.now()

    subscription = Subscription.objects.create(user=user, plan=plan, starts_at=start)

    assert subscription.ends_at == start + timedelta(days=30)
    assert subscription.ai_token_quota == plan.ai_token_quota
    assert subscription.interview_quota == plan.interview_quota
    assert subscription.voice_seconds_quota == plan.voice_seconds_quota


def test_plan_changes_do_not_alter_existing_subscriptions(user, subscribe):
    subscription = subscribe(user)
    Plan.objects.filter(code="platform-30-day").update(interview_quota=1)

    subscription.refresh_from_db()

    assert subscription.interview_quota == 60


def test_current_subscription_ignores_expired_future_and_cancelled(user, subscribe):
    now = timezone.now()
    subscribe(user, starts_at=now - timedelta(days=40), ends_at=now - timedelta(days=10))
    subscribe(user, starts_at=now + timedelta(days=1))
    subscribe(user, status=Subscription.Status.CANCELLED)

    assert current_subscription(user) is None

    active = subscribe(user)
    assert current_subscription(user) == active


def test_subscription_period_must_be_positive(user):
    plan = Plan.objects.get(code="platform-30-day")
    now = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        Subscription.objects.create(user=user, plan=plan, starts_at=now, ends_at=now)


def test_changing_plan_terms_creates_a_new_version_for_future_purchases(user, subscribe):
    plan = Plan.objects.get(code="platform-30-day")
    original = plan.current_version
    bought = subscribe(user)

    plan.price_minor = 650_000
    plan.duration_days = 45
    plan.daily_ai_token_limit = 100_000
    plan.save()

    new_version = plan.current_version
    assert new_version.version == original.version + 1
    assert (new_version.price_minor, new_version.duration_days) == (650_000, 45)
    # The earlier purchase keeps the terms it was bought under.
    bought.refresh_from_db()
    assert bought.plan_version == original
    assert original.price_minor == 500_000
    assert bought.daily_ai_token_limit is None
    assert bought.ends_at - bought.starts_at == timedelta(days=30)

    later = subscribe(user)
    assert later.plan_version == new_version
    assert later.daily_ai_token_limit == 100_000
    assert later.ends_at - later.starts_at == timedelta(days=45)


def test_saving_a_plan_without_term_changes_keeps_the_version():
    plan = Plan.objects.get(code="platform-30-day")
    version = plan.current_version

    plan.description = "Updated wording"
    plan.save()

    assert plan.current_version == version


def test_plan_versions_are_immutable():
    from django.core.exceptions import ValidationError

    version = Plan.objects.get(code="platform-30-day").current_version
    version.price_minor = 1

    with pytest.raises(ValidationError):
        version.save()
    with pytest.raises(ValidationError):
        version.delete()
