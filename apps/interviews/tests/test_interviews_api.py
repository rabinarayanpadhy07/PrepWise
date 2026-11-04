import uuid

import pytest

from apps.ai_services.errors import AIProviderUnavailable, AIRateLimited
from apps.interviews.models import AnswerEvaluation, CompetencyScore, InterviewSession
from apps.interviews.rubric import COMPETENCY_CODES
from apps.resumes.models import JobProfile, Resume
from apps.usage.models import UsageEvent

pytestmark = pytest.mark.django_db

INTERVIEWS = "/api/v1/interviews/"
JOB_DESCRIPTION = (
    "Product Manager for a payments team: own the roadmap, work with engineering and "
    "design, and define success metrics for checkout conversion."
)


def questions(count=5):
    return {
        "questions": [
            {
                "text": f"Tell me about challenge number {n}.",
                "competency": COMPETENCY_CODES[n % len(COMPETENCY_CODES)],
                "guidance": f"SECRET-GUIDANCE-{n}: a structured example with a result.",
            }
            for n in range(1, count + 1)
        ]
    }


def evaluation(score=4, codes=COMPETENCY_CODES):
    return {
        "overall_score": score,
        "summary": "A clear answer with a concrete example.",
        "strengths": ["Specific example"],
        "improvements": [
            {
                "area": "Results",
                "explanation": "The outcome was vague.",
                "suggestion": "Quantify the impact.",
            }
        ],
        "competency_scores": [
            {"competency": code, "score": score, "rationale": "Reason."} for code in codes
        ],
    }


@pytest.fixture(autouse=True)
def _ai_configured(ai_config):
    """Fast mode served by OpenAI with one platform key (see conftest.ai_config)."""


@pytest.fixture
def platform_user(user, subscribe):
    subscribe(user)
    return user


def start(client, fake_ai, count=5, **payload):
    fake_ai.respond(questions(count))
    body = {"role_title": "Product Manager", "mode": "text", "question_count": count}
    body.update(payload)
    return client.post(INTERVIEWS, body, format="json")


def answer(client, session_id, position, text="My answer with a concrete example.", **extra):
    return client.post(
        f"{INTERVIEWS}{session_id}/questions/{position}/answer/",
        {"text": text, **extra},
        format="json",
    )


def evaluate(client, session_id, position):
    return client.post(f"{INTERVIEWS}{session_id}/questions/{position}/evaluate/")


# --------------------------------------------------------------------------
# Starting a session
# --------------------------------------------------------------------------


def test_start_creates_session_with_generated_questions(auth_client, platform_user, fake_ai):
    response = start(auth_client, fake_ai)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["current_position"] == 1
    assert [q["position"] for q in body["questions"]] == [1, 2, 3, 4, 5]
    assert body["questions"][0]["answer"] is None
    # Evaluation guidance is internal and never sent to the browser.
    assert "SECRET-GUIDANCE" not in response.content.decode()

    interview_usage = UsageEvent.objects.get(kind="interview")
    assert interview_usage.source == "platform" and interview_usage.quantity == 1


def test_start_from_job_profile_uses_its_title_and_description(auth_client, platform_user, fake_ai):
    profile = JobProfile.objects.create(
        owner=platform_user, title="Payments PM", description=JOB_DESCRIPTION
    )
    resume = Resume.objects.create(
        owner=platform_user,
        original_filename="cv.pdf",
        file_type="pdf",
        size_bytes=10,
        sha256="0" * 64,
        extracted_text="Led checkout redesign at a fintech startup.",
    )

    response = start(
        auth_client, fake_ai, role_title="", job_profile_id=profile.id, resume_id=resume.id
    )

    assert response.json()["role_title"] == "Payments PM"
    prompt = fake_ai.calls[0]["prompt"]
    assert "checkout conversion" in prompt
    assert "Led checkout redesign" in prompt


def test_start_requires_a_role(auth_client, platform_user, fake_ai):
    response = auth_client.post(INTERVIEWS, {"mode": "text"}, format="json")

    assert response.status_code == 400
    assert "role_title" in response.json()["error"]["details"]


@pytest.mark.parametrize("count", [2, 11])
def test_question_count_is_bounded(auth_client, platform_user, fake_ai, count):
    response = auth_client.post(
        INTERVIEWS, {"role_title": "PM", "mode": "text", "question_count": count}, format="json"
    )

    assert response.status_code == 400


def test_cannot_use_another_users_job_profile(auth_client, platform_user, other_user, fake_ai):
    profile = JobProfile.objects.create(owner=other_user, title="X", description=JOB_DESCRIPTION)

    response = auth_client.post(
        INTERVIEWS, {"job_profile_id": profile.id, "mode": "text"}, format="json"
    )

    assert response.status_code == 400
    assert fake_ai.calls == []


def assert_basic_session(response, count=5):
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["ai_source"] == "basic"
    session = InterviewSession.objects.get(pk=body["id"])
    assert (session.ai_provider, session.ai_model) == ("", "")
    assert session.questions.count() == count
    return body


@pytest.mark.parametrize("mode", ["text", "voice"])
def test_start_without_a_plan_uses_basic_questions(auth_client, fake_ai, mode):
    response = auth_client.post(
        INTERVIEWS, {"role_title": "Backend Engineer", "mode": mode}, format="json"
    )

    assert_basic_session(response)
    assert fake_ai.calls == []
    assert not UsageEvent.objects.exists()


def test_free_users_have_no_interview_quota(auth_client, fake_ai):
    for _ in range(4):
        response = auth_client.post(INTERVIEWS, {"role_title": "PM", "mode": "text"}, format="json")
        assert_basic_session(response)
    assert InterviewSession.objects.count() == 4


def test_exhausted_interview_quota_falls_back_to_basic_without_calling_ai(
    auth_client, user, subscribe, fake_ai
):
    subscribe(user, interview_quota=0)

    response = auth_client.post(INTERVIEWS, {"role_title": "PM", "mode": "text"}, format="json")

    assert_basic_session(response)
    assert fake_ai.calls == []
    assert not UsageEvent.objects.filter(kind="interview").exists()


def test_daily_interview_limit_falls_back_to_basic(auth_client, user, subscribe, fake_ai):
    subscribe(user, daily_interview_limit=1)

    first = start(auth_client, fake_ai)
    assert first.status_code == 201 and first.json()["ai_source"] == "platform"
    response = auth_client.post(INTERVIEWS, {"role_title": "PM", "mode": "text"}, format="json")

    assert_basic_session(response)
    assert len(fake_ai.calls) == 1
    assert UsageEvent.objects.filter(kind="interview").count() == 1


def test_failed_question_generation_uses_basic_and_is_not_charged(
    auth_client, user, subscribe, fake_ai
):
    subscribe(user, interview_quota=1)
    fake_ai.fail(AIProviderUnavailable())
    fallback = auth_client.post(INTERVIEWS, {"role_title": "PM", "mode": "text"}, format="json")
    assert_basic_session(fallback)
    assert not UsageEvent.objects.filter(kind="interview").exists()

    # The plan's one interview is still available for an AI session.
    response = start(auth_client, fake_ai)
    assert response.status_code == 201 and response.json()["ai_source"] == "platform"
    assert UsageEvent.objects.filter(kind="interview").count() == 1


def test_repeated_start_with_the_same_key_creates_one_session(auth_client, platform_user, fake_ai):
    key = str(uuid.uuid4())
    first = start(auth_client, fake_ai, idempotency_key=key)
    second = auth_client.post(
        INTERVIEWS,
        {"role_title": "Product Manager", "mode": "text", "idempotency_key": key},
        format="json",
    )

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert len(fake_ai.calls) == 1
    assert InterviewSession.objects.count() == 1
    assert UsageEvent.objects.filter(kind="interview").count() == 1


@pytest.mark.parametrize("failure", [AIProviderUnavailable(), AIRateLimited()])
def test_provider_errors_fall_back_to_basic_questions(auth_client, platform_user, fake_ai, failure):
    fake_ai.fail(failure)

    response = auth_client.post(INTERVIEWS, {"role_title": "PM", "mode": "text"}, format="json")

    assert_basic_session(response)
    assert not UsageEvent.objects.filter(kind="interview").exists()


def test_too_few_generated_questions_falls_back_to_basic(auth_client, platform_user, fake_ai):
    fake_ai.respond(questions(2))

    response = auth_client.post(
        INTERVIEWS, {"role_title": "PM", "mode": "text", "question_count": 5}, format="json"
    )

    body = assert_basic_session(response, count=5)
    # The AI's partial output is never mixed into the basic questions.
    detail = auth_client.get(f"{INTERVIEWS}{body['id']}/").json()
    assert not any("SECRET-GUIDANCE" in q["text"] for q in detail["questions"])


# --------------------------------------------------------------------------
# Answers
# --------------------------------------------------------------------------


@pytest.fixture
def session_id(auth_client, platform_user, fake_ai):
    return start(auth_client, fake_ai, count=3).json()["id"]


def test_answer_advances_to_next_question(auth_client, session_id):
    response = answer(auth_client, session_id, 1)

    assert response.status_code == 201
    assert response.json()["input_mode"] == "text"
    assert auth_client.get(f"{INTERVIEWS}{session_id}/").json()["current_position"] == 2


def test_duplicate_submission_is_safe(auth_client, session_id):
    key = str(uuid.uuid4())
    first = answer(auth_client, session_id, 1, idempotency_key=key)
    retry = answer(auth_client, session_id, 1, idempotency_key=key)
    same_text = answer(auth_client, session_id, 1)

    assert first.status_code == 201
    assert retry.status_code == 200 and same_text.status_code == 200
    assert retry.json() == first.json()


def test_changing_a_submitted_answer_is_refused(auth_client, session_id):
    answer(auth_client, session_id, 1)

    response = answer(auth_client, session_id, 1, text="A completely different answer.")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_answered"


def test_questions_must_be_answered_in_order(auth_client, session_id):
    response = answer(auth_client, session_id, 2)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "out_of_order"


def test_blank_and_oversized_answers_are_rejected(auth_client, session_id):
    assert answer(auth_client, session_id, 1, text="   ").status_code == 400
    assert answer(auth_client, session_id, 1, text="x" * 8_001).status_code == 400


def test_unknown_question_returns_404(auth_client, session_id):
    assert answer(auth_client, session_id, 99).status_code == 404


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def test_evaluation_scores_answer_against_rubric(auth_client, session_id, fake_ai):
    answer(auth_client, session_id, 1)
    fake_ai.respond(evaluation(score=4))

    response = evaluate(auth_client, session_id, 1)

    assert response.status_code == 201
    body = response.json()
    assert body["ai_generated"] is True
    assert body["overall_score"] == 4
    assert {item["competency"] for item in body["competency_scores"]} == set(COMPETENCY_CODES)
    assert body["rubric_version"]
    prompt = fake_ai.calls[-1]["prompt"]
    assert "Rubric:" in prompt and "SECRET-GUIDANCE-1" in prompt
    assert "<answer>" in prompt and "concrete example" in prompt


def test_repeat_evaluation_returns_stored_result(auth_client, session_id, fake_ai):
    answer(auth_client, session_id, 1)
    fake_ai.respond(evaluation())
    evaluate(auth_client, session_id, 1)
    calls = len(fake_ai.calls)

    response = evaluate(auth_client, session_id, 1)

    assert response.status_code == 200
    assert len(fake_ai.calls) == calls
    assert AnswerEvaluation.objects.count() == 1


def test_cannot_evaluate_unanswered_question(auth_client, session_id):
    response = evaluate(auth_client, session_id, 1)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_answered"


def assert_basic_evaluation(response):
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["ai_generated"] is False
    assert 1 <= body["overall_score"] <= 5
    assert {item["competency"] for item in body["competency_scores"]} == set(COMPETENCY_CODES)
    evaluation = AnswerEvaluation.objects.get()
    assert (evaluation.ai_source, evaluation.ai_model) == ("basic", "")
    assert CompetencyScore.objects.filter(evaluation=evaluation).count() == len(COMPETENCY_CODES)
    return body


def test_failed_evaluation_keeps_answer_and_gives_basic_feedback(auth_client, session_id, fake_ai):
    answer(auth_client, session_id, 1)
    fake_ai.fail(AIProviderUnavailable())

    assert_basic_evaluation(evaluate(auth_client, session_id, 1))
    question = auth_client.get(f"{INTERVIEWS}{session_id}/").json()["questions"][0]
    assert question["answer"]["text"] and question["evaluation"]["ai_generated"] is False


def test_incomplete_rubric_scores_are_not_saved_and_basic_feedback_is_used(
    auth_client, session_id, fake_ai
):
    answer(auth_client, session_id, 1)
    fake_ai.respond(evaluation(codes=COMPETENCY_CODES[:3]))

    assert_basic_evaluation(evaluate(auth_client, session_id, 1))


def test_out_of_range_scores_are_not_saved_and_basic_feedback_is_used(
    auth_client, session_id, fake_ai
):
    answer(auth_client, session_id, 1)
    fake_ai.respond(evaluation(score=7))

    assert_basic_evaluation(evaluate(auth_client, session_id, 1))


def test_free_users_get_basic_feedback_without_calling_ai(auth_client, fake_ai):
    session_id = auth_client.post(
        INTERVIEWS, {"role_title": "PM", "mode": "text", "question_count": 3}, format="json"
    ).json()["id"]
    answer(auth_client, session_id, 1)

    assert_basic_evaluation(evaluate(auth_client, session_id, 1))
    assert fake_ai.calls == []
    assert not UsageEvent.objects.exists()


# --------------------------------------------------------------------------
# Completion, interruption and history
# --------------------------------------------------------------------------


def test_complete_finishes_session_and_blocks_new_answers(auth_client, session_id):
    answer(auth_client, session_id, 1)

    response = auth_client.post(f"{INTERVIEWS}{session_id}/complete/")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["current_position"] is None
    assert answer(auth_client, session_id, 2).json()["error"]["code"] == "interview_finished"
    assert auth_client.post(f"{INTERVIEWS}{session_id}/complete/").status_code == 200


def test_interrupted_session_resumes_at_current_question(auth_client, session_id):
    answer(auth_client, session_id, 1)
    answer(auth_client, session_id, 2)

    resumed = auth_client.get(f"{INTERVIEWS}{session_id}/").json()

    assert resumed["status"] == "in_progress"
    assert resumed["current_position"] == 3
    in_progress = auth_client.get(INTERVIEWS, {"status": "in_progress"}).json()["results"]
    assert in_progress[0]["answered_count"] == 2


def test_completed_sessions_can_still_be_evaluated(auth_client, session_id, fake_ai):
    answer(auth_client, session_id, 1)
    auth_client.post(f"{INTERVIEWS}{session_id}/complete/")
    fake_ai.respond(evaluation())

    assert evaluate(auth_client, session_id, 1).status_code == 201


def test_sessions_are_private(client_for, other_user, session_id, fake_ai):
    intruder = client_for(other_user)

    assert intruder.get(f"{INTERVIEWS}{session_id}/").status_code == 404
    assert answer(intruder, session_id, 1).status_code == 404
    assert evaluate(intruder, session_id, 1).status_code == 404
    assert intruder.post(f"{INTERVIEWS}{session_id}/complete/").status_code == 404
    assert intruder.get(INTERVIEWS).json()["count"] == 0
    assert intruder.get("/api/v1/evaluations/").json()["count"] == 0


def test_interview_endpoints_require_authentication(api_client):
    assert api_client.get(INTERVIEWS).status_code == 401
    assert api_client.get("/api/v1/evaluations/summary/").status_code == 401


def test_evaluation_history_and_summary(auth_client, session_id, fake_ai):
    for position, score in ((1, 2), (2, 4)):
        answer(auth_client, session_id, position)
        fake_ai.respond(evaluation(score=score))
        evaluate(auth_client, session_id, position)

    history = auth_client.get("/api/v1/evaluations/", {"session": session_id}).json()
    summary = auth_client.get("/api/v1/evaluations/summary/").json()

    assert history["count"] == 2
    assert history["results"][0]["ai_generated"] is True
    assert history["results"][0]["question_text"].startswith("Tell me about")
    assert summary["evaluation_count"] == 2
    communication = next(c for c in summary["competencies"] if c["competency"] == "communication")
    assert communication == {
        "competency": "communication",
        "label": "Communication",
        "average": 3.0,
        "count": 2,
    }
    assert summary["sessions"][0]["average_score"] == 3.0
