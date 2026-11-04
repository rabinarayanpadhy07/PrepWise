import logging
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.ai_services.errors import AIProviderUnavailable
from apps.resumes.models import JobProfile, Resume, ResumeAnalysis
from apps.resumes.tests.files import make_blank_pdf, make_docx, make_pdf
from apps.usage.models import UsageEvent

pytestmark = pytest.mark.django_db

RESUMES = "/api/v1/resumes/"
PROFILES = "/api/v1/job-profiles/"
ANALYSES = "/api/v1/resume-analyses/"
JOB_DESCRIPTION = (
    "We are hiring a Senior Backend Engineer to design Django REST APIs, own PostgreSQL "
    "performance, and run services on Kubernetes. Experience with Kafka is a plus."
)

ANALYSIS_RESULT = {
    "summary": "Strong backend fit; Kubernetes and Kafka are not shown.",
    "matched_skills": [{"skill": "Django", "evidence": "6 years building Django REST APIs"}],
    "missing_skills": [
        {"skill": "Kubernetes", "importance": "high", "suggestion": "Describe any container work."}
    ],
    "recommendations": ["Prepare a story about the latency improvement."],
    "questions": [
        {"question": f"Question {n}?", "focus": "Backend", "reason": "Core to the role"}
        for n in range(8)
    ],
}


def upload(client, data, name="resume.pdf"):
    return client.post(RESUMES, {"file": SimpleUploadedFile(name, data)}, format="multipart")


def make_resume(owner):
    return Resume.objects.create(
        owner=owner,
        original_filename="resume.pdf",
        file_type="pdf",
        size_bytes=1000,
        sha256="0" * 64,
        extracted_text="Jane Doe. Python and Django engineer with PostgreSQL experience.",
    )


def make_profile(owner):
    return JobProfile.objects.create(
        owner=owner, title="Senior Backend Engineer", company="Acme", description=JOB_DESCRIPTION
    )


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------


def test_upload_pdf_extracts_and_stores_text(auth_client, user):
    response = upload(auth_client, make_pdf(), name="Jane Doe CV.pdf")

    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "Jane Doe CV.pdf"
    assert body["file_type"] == "pdf"
    assert body["page_count"] == 1
    assert "PostgreSQL" in body["extracted_text"]
    assert body["text_length"] == len(body["extracted_text"])
    assert Resume.objects.get().owner == user


def test_upload_docx(auth_client):
    response = upload(auth_client, make_docx(), name="resume.docx")

    assert response.status_code == 201
    assert response.json()["file_type"] == "docx"


def test_upload_strips_paths_from_filenames(auth_client):
    response = upload(auth_client, make_pdf(), name="..\\..\\etc\\resume.pdf")

    assert response.json()["original_filename"] == "resume.pdf"


@pytest.mark.parametrize(
    "data, name, fragment",
    [
        (make_blank_pdf(), "scan.pdf", "scanned"),
        (b"%PDF-1.4 broken", "resume.pdf", "couldn't read"),
        (b"hello", "resume.txt", "PDF or DOCX"),
    ],
)
def test_upload_reports_extraction_failures(auth_client, data, name, fragment):
    response = upload(auth_client, data, name=name)

    assert response.status_code == 400
    assert fragment in response.json()["error"]["details"]["file"][0]
    assert Resume.objects.count() == 0


def test_upload_requires_a_file(auth_client):
    response = auth_client.post(RESUMES, {}, format="multipart")

    assert response.status_code == 400
    assert "file" in response.json()["error"]["details"]


def test_upload_enforces_size_limit(auth_client, settings):
    settings.RESUME_MAX_UPLOAD_BYTES = 500

    response = upload(auth_client, make_pdf())

    assert response.status_code == 400
    assert "MB or smaller" in response.json()["error"]["details"]["file"][0]


def test_oversized_request_is_refused_before_parsing(auth_client, settings):
    settings.RESUME_MAX_UPLOAD_BYTES = 500

    response = upload(auth_client, make_pdf() + b" " * 200_000)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


def test_upload_rejects_json_body(auth_client):
    response = auth_client.post(RESUMES, {"file": "x"}, format="json")

    assert response.status_code == 415


def test_upload_does_not_log_resume_text(auth_client, caplog):
    with caplog.at_level(logging.DEBUG):
        upload(auth_client, make_pdf())

    assert "PostgreSQL" not in caplog.text


def test_resume_list_omits_text(auth_client, user):
    make_resume(user)

    item = auth_client.get(RESUMES).json()["results"][0]

    assert "extracted_text" not in item
    assert item["text_length"] > 0


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------


def test_resume_endpoints_require_authentication(api_client):
    for url in (RESUMES, PROFILES, ANALYSES):
        assert api_client.get(url).status_code == 401


def test_users_cannot_access_other_users_records_by_id(auth_client, user, other_user):
    resume = make_resume(other_user)
    profile = make_profile(other_user)
    analysis = ResumeAnalysis.objects.create(owner=other_user, resume=resume, job_profile=profile)

    for url in (
        f"{RESUMES}{resume.id}/",
        f"{PROFILES}{profile.id}/",
        f"{ANALYSES}{analysis.id}/",
    ):
        assert auth_client.get(url).status_code == 404
        assert auth_client.delete(url).status_code == 404
    assert auth_client.patch(f"{PROFILES}{profile.id}/", {"title": "x"}).status_code == 404
    assert auth_client.post(f"{ANALYSES}{analysis.id}/retry/").status_code == 404

    assert auth_client.get(RESUMES).json()["count"] == 0
    assert Resume.objects.filter(pk=resume.pk).exists()


def test_cannot_analyse_another_users_resume(auth_client, user, other_user, fake_ai):
    response = auth_client.post(
        ANALYSES,
        {"resume_id": make_resume(other_user).id, "job_profile_id": make_profile(user).id},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["details"]["resume_id"] == ["Resume not found."]
    assert fake_ai.calls == []


# --------------------------------------------------------------------------
# Job profiles
# --------------------------------------------------------------------------


def test_job_profile_crud(auth_client):
    created = auth_client.post(
        PROFILES, {"title": "Data Analyst", "description": JOB_DESCRIPTION}, format="json"
    )
    assert created.status_code == 201
    profile_id = created.json()["id"]

    updated = auth_client.patch(f"{PROFILES}{profile_id}/", {"company": "Acme"}, format="json")
    assert updated.json()["company"] == "Acme"

    assert auth_client.delete(f"{PROFILES}{profile_id}/").status_code == 204


def test_job_profile_requires_a_real_description(auth_client):
    response = auth_client.post(PROFILES, {"title": "x", "description": "too short"}, format="json")

    assert response.status_code == 400
    assert "at least 50 characters" in response.json()["error"]["details"]["description"][0]


# --------------------------------------------------------------------------
# Analyses
# --------------------------------------------------------------------------


@pytest.fixture
def _ai_configured(ai_config):
    """Fast mode served by OpenAI with one platform key (see conftest.ai_config)."""


def create_analysis(client, user, **extra):
    return client.post(
        ANALYSES,
        {"resume_id": make_resume(user).id, "job_profile_id": make_profile(user).id, **extra},
        format="json",
    )


@pytest.mark.usefixtures("_ai_configured")
def test_analysis_runs_through_the_ai_service(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    fake_ai.respond(ANALYSIS_RESULT)

    response = create_analysis(auth_client, user)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["missing_skills"][0]["skill"] == "Kubernetes"
    assert len(body["questions"]) == 8
    assert body["ai_source"] == "platform"
    assert body["completed_at"]

    prompt = fake_ai.calls[0]["prompt"]
    assert "<resume>" in prompt and "Python and Django engineer" in prompt
    assert "<job_description>" in prompt and "Kubernetes" in prompt


def assert_basic_analysis(response):
    """The keyword analysis of make_resume() against JOB_DESCRIPTION."""
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["status"] == "completed"
    assert (body["ai_source"], body["ai_provider"], body["ai_model"]) == ("basic", "", "")
    assert {item["skill"] for item in body["matched_skills"]} == {"Django", "PostgreSQL"}
    missing = {item["skill"]: item["importance"] for item in body["missing_skills"]}
    assert missing["Kafka"] == "low"  # "is a plus"
    assert "Kubernetes" in missing
    assert body["recommendations"] and len(body["questions"]) >= 3
    assert "Keyword match" in body["summary"]
    return body


@pytest.mark.usefixtures("_ai_configured")
def test_analysis_without_a_plan_uses_the_basic_keyword_analysis(auth_client, user, fake_ai):
    response = create_analysis(auth_client, user)

    assert_basic_analysis(response)
    assert fake_ai.calls == []
    assert not UsageEvent.objects.exists()


@pytest.mark.usefixtures("_ai_configured")
def test_provider_failure_falls_back_to_the_basic_analysis(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    fake_ai.fail(AIProviderUnavailable())

    response = create_analysis(auth_client, user)

    assert_basic_analysis(response)
    assert response.json()["error_message"] == ""
    # The reservation was released: nothing is charged for the failed AI call.
    assert not UsageEvent.objects.exists()


@pytest.mark.usefixtures("_ai_configured")
def test_invalid_ai_output_is_not_saved_and_the_basic_analysis_is_used(
    auth_client, user, subscribe, fake_ai
):
    subscribe(user)
    fake_ai.respond({**ANALYSIS_RESULT, "questions": []})

    body = assert_basic_analysis(create_analysis(auth_client, user))

    assert not any(item["question"].startswith("Question ") for item in body["questions"])


@pytest.mark.usefixtures("_ai_configured")
def test_completed_analysis_cannot_be_retried(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    fake_ai.respond(ANALYSIS_RESULT)
    analysis_id = create_analysis(auth_client, user).json()["id"]

    response = auth_client.post(f"{ANALYSES}{analysis_id}/retry/")

    assert response.status_code == 409
    assert fake_ai.calls and len(fake_ai.calls) == 1


@pytest.mark.usefixtures("_ai_configured")
def test_interrupted_pending_analysis_can_be_retried(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    analysis = ResumeAnalysis.objects.create(
        owner=user, resume=make_resume(user), job_profile=make_profile(user)
    )
    assert auth_client.post(f"{ANALYSES}{analysis.id}/retry/").status_code == 409

    ResumeAnalysis.objects.filter(pk=analysis.pk).update(
        created_at=timezone.now() - timedelta(minutes=30)
    )
    fake_ai.respond(ANALYSIS_RESULT)

    assert auth_client.post(f"{ANALYSES}{analysis.id}/retry/").status_code == 200


@pytest.mark.usefixtures("_ai_configured")
def test_analysis_history_lists_own_analyses(auth_client, user, other_user, subscribe, fake_ai):
    subscribe(user)
    fake_ai.respond(ANALYSIS_RESULT)
    create_analysis(auth_client, user)
    ResumeAnalysis.objects.create(
        owner=other_user, resume=make_resume(other_user), job_profile=make_profile(other_user)
    )

    history = auth_client.get(ANALYSES).json()

    assert history["count"] == 1
    assert history["results"][0]["job_profile"]["title"] == "Senior Backend Engineer"


@pytest.mark.usefixtures("_ai_configured")
def test_analysis_does_not_log_private_content(auth_client, user, subscribe, fake_ai, caplog):
    subscribe(user)
    fake_ai.respond(ANALYSIS_RESULT)

    with caplog.at_level(logging.DEBUG):
        create_analysis(auth_client, user)

    assert "Python and Django engineer" not in caplog.text
    assert "Kubernetes" not in caplog.text


@pytest.mark.usefixtures("_ai_configured")
def test_analysis_usage_is_recorded_against_the_subscription(auth_client, user, subscribe, fake_ai):
    subscription = subscribe(user)
    fake_ai.respond(ANALYSIS_RESULT, input_tokens=3000, output_tokens=1500)

    create_analysis(auth_client, user)

    event = UsageEvent.objects.get()
    assert (event.source, event.subscription, event.feature) == (
        "platform",
        subscription,
        "resume_analysis",
    )
    assert (event.input_tokens, event.output_tokens) == (3000, 1500)


@pytest.mark.usefixtures("_ai_configured")
def test_retried_create_with_the_same_key_does_not_call_the_ai_again(
    auth_client, user, subscribe, fake_ai
):
    import uuid

    subscribe(user)
    resume, profile = make_resume(user), make_profile(user)
    payload = {
        "resume_id": resume.id,
        "job_profile_id": profile.id,
        "idempotency_key": str(uuid.uuid4()),
    }
    fake_ai.respond(ANALYSIS_RESULT)

    first = auth_client.post(ANALYSES, payload, format="json")
    second = auth_client.post(ANALYSES, payload, format="json")

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert len(fake_ai.calls) == 1
    assert ResumeAnalysis.objects.count() == 1


@pytest.mark.usefixtures("_ai_configured")
def test_exhausted_daily_tokens_fall_back_to_the_basic_analysis(
    auth_client, user, subscribe, fake_ai
):
    subscribe(user, daily_ai_token_limit=10)

    response = create_analysis(auth_client, user)

    assert_basic_analysis(response)
    assert fake_ai.calls == []
    assert ResumeAnalysis.objects.get().error_code == ""
