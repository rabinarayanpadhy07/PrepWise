import pytest
from django.db import IntegrityError, transaction

from apps.billing.models import Plan

pytestmark = pytest.mark.django_db

PLANS_URL = "/api/v1/billing/plans/"


def test_initial_plan_is_seeded_by_migration():
    plan = Plan.objects.get(code="platform-30-day")

    assert plan.price_minor == 500_000  # INR 5,000
    assert plan.currency == "INR"
    assert plan.duration_days == 30
    assert plan.renewal_mode == Plan.RenewalMode.MANUAL
    assert plan.is_active


def test_plans_endpoint_is_public_and_returns_backend_pricing(api_client):
    response = api_client.get(PLANS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["results"] == [
        {
            "code": "platform-30-day",
            "name": "Platform AI · 30 days",
            "description": (
                "Use PrepWise's platform AI for resume analysis, mock interviews, "
                "and answer feedback without supplying your own API key."
            ),
            "currency": "INR",
            "price_minor": 500000,
            "duration_days": 30,
            "renewal_mode": "manual",
            "ai_token_quota": 3_000_000,
            "daily_ai_token_limit": None,
            "interview_quota": 60,
            "daily_interview_limit": None,
            "voice_seconds_quota": 18_000,
            "daily_voice_seconds_limit": None,
            "version": 1,
        }
    ]


def test_inactive_plans_are_hidden(api_client):
    Plan.objects.update(is_active=False)

    response = api_client.get(PLANS_URL)

    assert response.json()["results"] == []


def test_plans_endpoint_is_read_only(api_client):
    response = api_client.post(PLANS_URL, {"code": "x"}, format="json")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"price_minor": 0},
        {"duration_days": 0},
        {"duration_days": 400},
        {"currency": "USD"},
        {"renewal_mode": "automatic"},
    ],
)
def test_database_constraints_reject_invalid_plans(overrides):
    values = {
        "code": "invalid-plan",
        "name": "Invalid",
        "currency": "INR",
        "price_minor": 100,
        "duration_days": 30,
        "renewal_mode": "manual",
        "ai_token_quota": 1000,
        "interview_quota": 1,
        "voice_seconds_quota": 60,
        **overrides,
    }

    with pytest.raises(IntegrityError), transaction.atomic():
        Plan.objects.create(**values)


def test_plan_code_is_unique():
    with pytest.raises(IntegrityError), transaction.atomic():
        Plan.objects.create(
            code="platform-30-day",
            name="Duplicate",
            price_minor=100,
            duration_days=30,
            ai_token_quota=1,
            interview_quota=1,
            voice_seconds_quota=1,
        )
