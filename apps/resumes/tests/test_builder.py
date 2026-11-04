"""Resume maker: content storage, AI import, and AI tailoring with server-side checks."""

import pytest

from apps.resumes.builder import apply_changes, clean_content, validate_changes
from apps.resumes.models import BuiltResume, JobProfile, Resume

pytestmark = pytest.mark.django_db

LIST = "/api/v1/built-resumes/"
IMPORT = "/api/v1/built-resumes/import/"

CONTENT = {
    "name": "Asha Rao",
    "phone": "+91 90000 00000",
    "email": "asha@example.com",
    "links": [{"kind": "github", "url": "https://github.com/asha"}],
    "summary": "Full-stack developer building web apps with React and Node.js.",
    "education": [
        {"institution": "City College", "dates": "2022 -- 2026", "degree": "B.Tech", "score": "8.2"}
    ],
    "skills": [
        {"category": "Languages", "items": "JavaScript, Python"},
        {"category": "Frameworks", "items": "React.js, Node.js, Express.js"},
    ],
    "experience": [
        {
            "company": "Acme",
            "dates": "Mar 2026 -- Apr 2026",
            "role": "Intern",
            "location": "Remote",
            "bullets": ["Built REST APIs in Express.js.", "Wrote unit tests."],
        }
    ],
    "projects": [
        {
            "name": "Tracker",
            "url": "https://tracker.example.com",
            "tech": "React.js, Node.js",
            "bullets": ["Made a dashboard with charts."],
        }
    ],
    "certifications": [{"name": "AWS Cloud Essentials", "url": "", "issuer": "AWS"}],
}


def job(user, **extra):
    return JobProfile.objects.create(
        owner=user, title="Backend Developer", description="Node.js, REST APIs, testing.", **extra
    )


def built(user, content=CONTENT, **extra):
    return BuiltResume.objects.create(
        owner=user, title="My resume", content=clean_content(content), **extra
    )


# --- Content and CRUD ---------------------------------------------------------


def test_clean_content_fills_every_field_for_empty_input():
    content = clean_content({})
    assert content["name"] == "" and content["skills"] == [] and content["links"] == []


def test_creates_a_blank_resume_with_full_content_shape(auth_client, user):
    response = auth_client.post(LIST, {"title": "  Backend resume  "}, format="json")

    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["title"] == "Backend resume"
    assert body["content"]["experience"] == []
    assert BuiltResume.objects.get().owner == user


def test_saves_valid_content_and_rejects_invalid_content(auth_client, user):
    resume = built(user, content={})
    url = f"{LIST}{resume.pk}/"

    ok = auth_client.patch(url, {"content": CONTENT}, format="json")
    assert ok.status_code == 200
    assert ok.json()["content"]["skills"][1]["items"] == "React.js, Node.js, Express.js"

    bad = auth_client.patch(
        url, {"content": {"links": [{"kind": "github", "url": "not a url"}]}}, format="json"
    )
    assert bad.status_code == 400
    resume.refresh_from_db()
    assert resume.content["name"] == "Asha Rao"


def test_list_omits_content_and_is_scoped_to_the_owner(auth_client, user, other_user):
    built(user)
    built(other_user)

    results = auth_client.get(LIST).json()["results"]

    assert len(results) == 1
    assert "content" not in results[0]


def test_other_users_resumes_and_job_descriptions_are_not_found(auth_client, user, other_user):
    theirs = built(other_user)
    mine = built(user)

    assert auth_client.get(f"{LIST}{theirs.pk}/").status_code == 404
    response = auth_client.patch(
        f"{LIST}{mine.pk}/", {"job_profile_id": job(other_user).pk}, format="json"
    )
    assert response.status_code == 400


# --- AI import ---------------------------------------------------------------


def test_import_fills_fields_from_an_uploaded_resume(
    auth_client, user, subscribe, fake_ai, ai_config
):
    subscribe(user)
    source = Resume.objects.create(
        owner=user,
        original_filename="Asha_Rao.pdf",
        file_type="pdf",
        size_bytes=100,
        sha256="0" * 64,
        extracted_text="Asha Rao ... resume text",
    )
    imported = {
        **CONTENT,
        # Untidy model output the import must clean up rather than fail on.
        "links": [{"kind": "website", "url": "asha.dev"}, *CONTENT["links"]],
        "education": [
            *CONTENT["education"],
            {"institution": "", "dates": "", "degree": "", "score": ""},
        ],
    }
    fake_ai.respond(imported)

    response = auth_client.post(IMPORT, {"resume_id": source.pk}, format="json")

    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["title"] == "Asha_Rao"
    assert body["content"]["links"] == CONTENT["links"]
    assert len(body["content"]["education"]) == 1
    assert "<resume>" in fake_ai.calls[0]["prompt"]


def test_import_of_another_users_resume_never_calls_the_ai(auth_client, other_user, fake_ai):
    theirs = Resume.objects.create(
        owner=other_user,
        original_filename="x.pdf",
        file_type="pdf",
        size_bytes=1,
        sha256="0" * 64,
        extracted_text="x",
    )

    response = auth_client.post(IMPORT, {"resume_id": theirs.pk}, format="json")

    assert response.status_code == 400
    assert fake_ai.calls == []


# --- AI tailoring ------------------------------------------------------------


def test_tailor_returns_reviewed_changes_and_saves_nothing(
    auth_client, user, subscribe, fake_ai, ai_config
):
    subscribe(user)
    resume = built(user)
    target = job(user)
    fake_ai.respond(
        {
            "changes": [
                {
                    "path": "experience.0.bullets.0",
                    "after": "Designed and built REST APIs with Express.js.",
                    "reason": "The job asks for REST API experience.",
                },
                {
                    "path": "skills.1.items",
                    "after": "Node.js, Express.js, React.js",
                    "reason": "Backend first.",
                },
                # Dropped: adds a skill the resume never mentions.
                {"path": "skills.0.items", "after": "Python, Go", "reason": "Go is required."},
                # Dropped: not an editable path / unchanged / out of range.
                {"path": "name", "after": "Someone else", "reason": "x"},
                {"path": "experience.0.bullets.1", "after": "Wrote unit tests.", "reason": "x"},
                {"path": "projects.4.bullets.0", "after": "x", "reason": "x"},
            ]
        }
    )

    response = auth_client.post(
        f"{LIST}{resume.pk}/tailor/", {"job_profile_id": target.pk}, format="json"
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["job_profile"]["id"] == target.pk
    assert body["source"] == "platform"
    assert [c["path"] for c in body["changes"]] == ["skills.1.items", "experience.0.bullets.0"]
    first = body["changes"][0]
    assert first["before"] == "React.js, Node.js, Express.js"
    assert first["label"] == "Skills · Frameworks"
    resume.refresh_from_db()
    assert resume.content == clean_content(CONTENT)
    prompt = fake_ai.calls[0]["prompt"]
    assert "experience.0.bullets.0: Built REST APIs in Express.js." in prompt
    assert "<job_description>" in prompt


def test_tailor_uses_unsaved_editor_content_when_given(
    auth_client, user, subscribe, fake_ai, ai_config
):
    subscribe(user)
    resume = built(user, content={})
    fake_ai.respond({"changes": []})

    response = auth_client.post(
        f"{LIST}{resume.pk}/tailor/",
        {"job_profile_id": job(user).pk, "content": CONTENT},
        format="json",
    )

    assert response.status_code == 200, response.json()
    assert "summary: Full-stack developer" in fake_ai.calls[0]["prompt"]


def test_tailor_with_nothing_to_edit_skips_the_ai(auth_client, user, fake_ai):
    resume = built(user, content={"name": "Only a name"})

    response = auth_client.post(
        f"{LIST}{resume.pk}/tailor/", {"job_profile_id": job(user).pk}, format="json"
    )

    assert response.status_code == 200, response.json()
    assert response.json()["changes"] == []
    assert response.json()["source"] == ""
    assert fake_ai.calls == []


BASIC_TAILORING = [
    {
        "path": "skills.1.items",
        "label": "Skills · Frameworks",
        "before": "React.js, Node.js, Express.js",
        "after": "Node.js, React.js, Express.js",
        "reason": "Puts Node.js first because the job description asks for them.",
    },
    {
        "path": "projects.0.tech",
        "label": "Projects · Tracker · stack",
        "before": "React.js, Node.js",
        "after": "Node.js, React.js",
        "reason": "Puts Node.js first because the job description asks for them.",
    },
]


def test_tailor_without_a_plan_only_reorders_skill_lists(auth_client, user, fake_ai):
    resume = built(user)

    response = auth_client.post(
        f"{LIST}{resume.pk}/tailor/", {"job_profile_id": job(user).pk}, format="json"
    )

    assert response.status_code == 200, response.json()
    assert response.json()["source"] == "basic"
    assert response.json()["changes"] == BASIC_TAILORING
    assert fake_ai.calls == []
    resume.refresh_from_db()
    assert resume.content == clean_content(CONTENT)


def test_tailor_falls_back_to_basic_when_the_ai_fails(
    auth_client, user, subscribe, fake_ai, ai_config
):
    from apps.ai_services.errors import AIProviderUnavailable
    from apps.usage.models import UsageEvent

    subscribe(user)
    fake_ai.fail(AIProviderUnavailable())
    resume = built(user)

    response = auth_client.post(
        f"{LIST}{resume.pk}/tailor/", {"job_profile_id": job(user).pk}, format="json"
    )

    assert response.status_code == 200, response.json()
    assert response.json()["source"] == "basic"
    assert response.json()["changes"] == BASIC_TAILORING
    assert not UsageEvent.objects.exists()


def test_apply_changes_updates_only_the_named_fields():
    content = clean_content(CONTENT)
    changes = validate_changes(
        content,
        [
            {"path": "summary", "after": "Backend-focused developer.", "reason": ""},
            {"path": "projects.0.tech", "after": "Node.js, React.js", "reason": ""},
        ],
    )

    updated = apply_changes(content, changes)

    assert updated["summary"] == "Backend-focused developer."
    assert updated["projects"][0]["tech"] == "Node.js, React.js"
    assert updated["experience"] == content["experience"]
    assert content["summary"].startswith("Full-stack")  # original untouched
