"""Dashboard summary, activity feed and PDF reports: real data, owner-only access."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.interviews.tests.test_interviews_api import answer, evaluate, evaluation, start
from apps.resumes.models import JobProfile, Resume, ResumeAnalysis
from apps.resumes.tests.test_api import ANALYSIS_RESULT, make_profile, make_resume

pytestmark = pytest.mark.django_db

SUMMARY = "/api/v1/dashboard/summary/"
ACTIVITY = "/api/v1/dashboard/activity/"


@pytest.fixture(autouse=True)
def _ai_configured(ai_config):
    """Fast mode served by OpenAI with one platform key (see conftest.ai_config)."""


def completed_analysis(owner, **overrides):
    values = {
        "owner": owner,
        "resume": make_resume(owner),
        "job_profile": make_profile(owner),
        "status": ResumeAnalysis.Status.COMPLETED,
        "summary": ANALYSIS_RESULT["summary"],
        "matched_skills": ANALYSIS_RESULT["matched_skills"],
        "missing_skills": ANALYSIS_RESULT["missing_skills"],
        "recommendations": ANALYSIS_RESULT["recommendations"],
        "questions": ANALYSIS_RESULT["questions"],
        "ai_source": "platform",
        "ai_provider": "openai",
        "ai_model": "test-openai-large",
        "completed_at": timezone.now(),
    }
    values.update(overrides)
    return ResumeAnalysis.objects.create(**values)


def evaluated_interview(client, fake_ai, score=4):
    session = start(client, fake_ai, count=3).json()
    answer(client, session["id"], 1)
    fake_ai.respond(evaluation(score))
    evaluate(client, session["id"], 1)
    return session


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summary_for_a_new_user_has_no_fabricated_data(auth_client, user):
    body = auth_client.get(SUMMARY).json()

    assert body["counts"] == {
        "resumes": 0,
        "analyses": 0,
        "analyses_completed": 0,
        "interviews": 0,
        "interviews_completed": 0,
        "answers": 0,
        "evaluations": 0,
        "average_score": None,
    }
    assert body["subscription"]["status"] == "none"
    assert body["platform_status"] == "subscription_required"
    assert body["platform_usage"] is None
    assert "byok_usage" not in body and "has_byok_key" not in body


def test_summary_counts_only_the_users_own_records(
    auth_client, client_for, user, other_user, subscribe, fake_ai
):
    subscribe(user)
    subscribe(other_user)
    completed_analysis(user)
    completed_analysis(other_user)
    evaluated_interview(auth_client, fake_ai, score=4)
    evaluated_interview(client_for(other_user), fake_ai, score=1)

    counts = auth_client.get(SUMMARY).json()["counts"]

    assert counts["analyses"] == 1
    assert counts["interviews"] == 1
    assert counts["answers"] == 1
    assert counts["evaluations"] == 1
    assert counts["average_score"] == 4


def test_summary_shows_platform_allowances_and_subscription_expiry(
    auth_client, user, subscribe, fake_ai
):
    subscription = subscribe(user, daily_ai_token_limit=50_000, daily_interview_limit=3)
    evaluated_interview(auth_client, fake_ai)

    body = auth_client.get(SUMMARY).json()

    assert body["subscription"]["status"] == "active"
    assert body["subscription"]["ends_at"] == subscription.ends_at.isoformat().replace(
        "+00:00", "Z"
    )
    tokens = body["platform_usage"]["ai_tokens"]
    # Two AI calls (questions + evaluation) at 1,200 + 800 tokens each.
    assert tokens["period"]["used"] == 4_000
    assert tokens["period"]["remaining"] == 3_000_000 - 4_000
    assert tokens["daily"]["limit"] == 50_000
    assert tokens["daily"]["remaining"] == 46_000
    interviews = body["platform_usage"]["interview"]
    assert (interviews["period"]["used"], interviews["daily"]["remaining"]) == (1, 2)


def test_expired_subscription_is_shown_as_expired(auth_client, user, subscribe):
    now = timezone.now()
    subscribe(user, starts_at=now - timedelta(days=31), ends_at=now - timedelta(days=1))

    body = auth_client.get(SUMMARY).json()

    assert body["subscription"]["status"] == "expired"
    assert body["platform_status"] == "subscription_expired"
    assert body["platform_usage"] is None


def test_dashboard_requires_authentication(api_client):
    assert api_client.get(SUMMARY).status_code == 401
    assert api_client.get(ACTIVITY).status_code == 401


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


def test_activity_lists_recent_events_newest_first(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    analysis = completed_analysis(user, completed_at=timezone.now() - timedelta(days=1))
    session = evaluated_interview(auth_client, fake_ai)

    results = auth_client.get(ACTIVITY).json()["results"]

    assert [item["type"] for item in results] == [
        "answer_evaluated",
        "interview_started",
        "analysis_completed",
    ]
    assert results[0]["target"] == {"kind": "interview", "id": session["id"]}
    assert results[0]["detail"] == "Question 1: 4 / 5"
    assert results[-1]["target"] == {"kind": "analysis", "id": analysis.pk}


def test_activity_is_paginated_and_private(auth_client, client_for, user, other_user):
    for _ in range(23):
        completed_analysis(user)
    completed_analysis(other_user)

    first = auth_client.get(ACTIVITY).json()
    second = auth_client.get(ACTIVITY, {"page": 2}).json()

    assert first["count"] == 23
    assert len(first["results"]) == 20
    assert first["next"] is not None
    assert len(second["results"]) == 3
    assert client_for(other_user).get(ACTIVITY).json()["count"] == 1


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_analysis_report_is_generated_from_stored_results(auth_client, user, fake_ai):
    analysis = completed_analysis(user)

    response = auth_client.get(f"/api/v1/reports/analyses/{analysis.pk}/pdf/")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "private, no-store"
    assert response.content.startswith(b"%PDF")
    assert fake_ai.calls == []  # nothing is re-run through AI


def test_interview_report_is_generated_from_stored_results(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    session = evaluated_interview(auth_client, fake_ai)
    calls = len(fake_ai.calls)

    response = auth_client.get(f"/api/v1/reports/interviews/{session['id']}/pdf/")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert len(fake_ai.calls) == calls


def test_reports_are_only_available_to_their_owner(
    auth_client, client_for, user, other_user, subscribe, fake_ai
):
    subscribe(user)
    analysis = completed_analysis(user)
    session = evaluated_interview(auth_client, fake_ai)
    intruder = client_for(other_user)

    assert intruder.get(f"/api/v1/reports/analyses/{analysis.pk}/pdf/").status_code == 404
    assert intruder.get(f"/api/v1/reports/interviews/{session['id']}/pdf/").status_code == 404
    assert auth_client.get("/api/v1/reports/analyses/999999/pdf/").status_code == 404


def test_report_requires_a_completed_analysis(auth_client, user):
    pending = ResumeAnalysis.objects.create(
        owner=user,
        resume=Resume.objects.create(
            owner=user,
            original_filename="cv.pdf",
            file_type="pdf",
            size_bytes=10,
            sha256="0" * 64,
            extracted_text="Text",
        ),
        job_profile=JobProfile.objects.create(
            owner=user, title="Role", description="A real job description " * 5
        ),
    )

    response = auth_client.get(f"/api/v1/reports/analyses/{pending.pk}/pdf/")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "report_unavailable"


def test_reports_render_non_latin_text_without_failing(auth_client, user):
    analysis = completed_analysis(user, summary="Résumé fits well — “strong” • 日本語 ₹5,000")

    response = auth_client.get(f"/api/v1/reports/analyses/{analysis.pk}/pdf/")

    assert response.status_code == 200


def test_reports_label_basic_results_as_not_ai_generated():
    from apps.dashboard import reports

    assert reports._notice(["basic"]) == reports.BASIC_NOTICE
    assert reports._notice(["basic", "basic"]) == reports.BASIC_NOTICE
    assert reports._notice(["platform", "platform"]) == reports.AI_NOTICE
    assert reports._notice(["basic", "platform"]) == reports.MIXED_NOTICE
    assert "not AI-generated" in reports._ai_line("basic", "", "")
    assert (
        reports._ai_line("platform", "openai", "gpt-test") == "Generated by PrepWise AI (gpt-test)."
    )


def test_basic_interview_report_renders(auth_client, user, fake_ai):
    session = auth_client.post(
        "/api/v1/interviews/",
        {"role_title": "PM", "mode": "text", "question_count": 3},
        format="json",
    ).json()
    answer(auth_client, session["id"], 1)
    evaluate(auth_client, session["id"], 1)

    response = auth_client.get(f"/api/v1/reports/interviews/{session['id']}/pdf/")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert fake_ai.calls == []
