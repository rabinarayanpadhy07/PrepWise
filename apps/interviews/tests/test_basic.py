"""
The basic (rule-based, non-AI) engines: skill matching and role detection,
interview questions and answer feedback, resume analysis and tailoring.
Their output must always pass the same validation as the AI's output.
"""

from types import SimpleNamespace

import pytest

from apps.ai_services.basic_text import find_skills, importance, role_family
from apps.interviews.ai import EvaluationOutputSerializer, questions_serializer
from apps.interviews.basic import OPENING, basic_evaluation, basic_questions
from apps.interviews.rubric import COMPETENCY_CODES
from apps.resumes.analysis import AnalysisOutputSerializer
from apps.resumes.basic import basic_analysis, basic_tailoring
from apps.resumes.builder import TailorOutputSerializer, editable_fields, validate_changes

BACKEND_JOB = SimpleNamespace(
    title="Senior Backend Engineer",
    company="Acme",
    description=(
        "We need strong Python and Django experience.\n"
        "Must have Kubernetes in production.\n"
        "Experience with PostgreSQL required.\n"
        "Nice to have: GraphQL."
    ),
)
BACKEND_RESUME = SimpleNamespace(
    extracted_text=(
        "Jane Doe\n"
        "Built REST APIs in Python and Django serving 2M users\n"
        "Skills: Python, Django, PostgreSQL, Git"
    )
)


# --- Skills and roles ---------------------------------------------------------


def test_skills_match_whole_words_and_aliases():
    assert find_skills("JavaScript and Golang, plus K8s") == ["JavaScript", "Go", "Kubernetes"]
    assert "Java" not in find_skills("JavaScript developer")
    assert "Go" not in find_skills("a good team to go with")
    assert "C" not in find_skills("C-level stakeholders and R&D")


def test_importance_reads_the_job_description_wording():
    text = BACKEND_JOB.description
    assert importance("Kubernetes", text) == "high"
    assert importance("GraphQL", text) == "low"
    assert importance("Redis", "We use Redis for caching.") == "medium"


@pytest.mark.parametrize(
    "title, family",
    [
        ("Senior Backend Engineer", "engineering"),
        ("Data Analyst", "data"),
        ("Data Engineer", "data"),
        ("Financial Analyst", "finance"),
        ("Marketing Analyst", "marketing"),
        ("Product Designer", "design"),
        ("Product Manager", "product"),
        ("Account Executive", "sales"),
        ("HR Business Partner", "people"),
        ("Customer Success Manager", "support"),
        ("Barista", "general"),
    ],
)
def test_role_family_from_the_title(title, family):
    assert role_family(title) == family


def test_role_family_falls_back_to_the_job_description():
    text = "As a backend developer you will write software with the frontend engineer."
    assert role_family("Team member", text) == "engineering"


# --- Interview questions --------------------------------------------------------


@pytest.mark.parametrize("count", [3, 5, 10])
def test_basic_questions_validate_like_ai_output(count):
    data = basic_questions(
        role_title="Backend Engineer", job_profile=BACKEND_JOB, resume=BACKEND_RESUME, count=count
    )

    assert questions_serializer(count)(data=data).is_valid()
    assert len(data["questions"]) == count
    assert data["questions"][0]["text"] == OPENING[0].replace("{role}", "Backend Engineer")
    assert {q["competency"] for q in data["questions"]} <= set(COMPETENCY_CODES)
    assert len({q["text"] for q in data["questions"]}) == count  # no repeats


def test_basic_questions_use_skills_from_the_job_description():
    data = basic_questions(
        role_title="Backend Engineer", job_profile=BACKEND_JOB, resume=BACKEND_RESUME, count=10
    )

    text = " ".join(q["text"] for q in data["questions"])
    assert any(skill in text for skill in ("Python", "Django", "Kubernetes", "PostgreSQL"))


def test_basic_questions_work_without_a_job_or_resume():
    data = basic_questions(role_title="", job_profile=None, resume=None, count=5)

    assert questions_serializer(5)(data=data).is_valid()


# --- Answer feedback ----------------------------------------------------------

QUESTION = SimpleNamespace(
    text="Tell me about a time you improved the performance of a service.",
    competency="evidence",
    guidance="A specific example, the candidate's own actions and a measurable result.",
)
SESSION = SimpleNamespace(role_title="Backend Engineer", job_profile=BACKEND_JOB)
STRONG = (
    "At my previous job our checkout API was slow during sales, and my task was to bring the "
    "p95 latency under 300 ms. First I profiled the service and found the root cause: repeated "
    "PostgreSQL queries. I decided to add an index and a Redis cache instead of scaling up, "
    "because it was cheaper and lower risk. Then I load-tested the change and rolled it out "
    "gradually. As a result latency dropped from 900 ms to 250 ms and we saved 30 percent on "
    "database costs."
)
WEAK = "I am good at performance and I always try my best to make things fast for the team."


def feedback(text):
    data = basic_evaluation(session=SESSION, question=QUESTION, answer=SimpleNamespace(text=text))
    assert EvaluationOutputSerializer(data=data).is_valid(), data
    return data


def test_basic_feedback_validates_like_ai_output_and_scores_every_competency():
    data = feedback(STRONG)

    assert sorted(item["competency"] for item in data["competency_scores"]) == sorted(
        COMPETENCY_CODES
    )
    assert data["improvements"]


def test_a_specific_structured_answer_scores_higher_than_a_vague_one():
    strong, weak = feedback(STRONG), feedback(WEAK)

    assert strong["overall_score"] > weak["overall_score"]
    by_code = {item["competency"]: item["score"] for item in strong["competency_scores"]}
    weak_by_code = {item["competency"]: item["score"] for item in weak["competency_scores"]}
    assert by_code["evidence"] > weak_by_code["evidence"]
    assert by_code["structure"] > weak_by_code["structure"]


def test_a_very_short_answer_gets_the_lowest_scores():
    data = feedback("Not sure.")

    assert data["overall_score"] == 1
    assert {item["score"] for item in data["competency_scores"]} == {1}


# --- Resume analysis and tailoring ----------------------------------------------


def test_basic_analysis_matches_keywords_and_validates_like_ai_output():
    data = basic_analysis(BACKEND_RESUME, BACKEND_JOB)

    assert AnalysisOutputSerializer(data=data).is_valid()
    assert [item["skill"] for item in data["matched_skills"]] == ["Python", "Django", "PostgreSQL"]
    assert data["matched_skills"][0]["evidence"] == (
        "Built REST APIs in Python and Django serving 2M users"
    )
    missing = [(item["skill"], item["importance"]) for item in data["missing_skills"]]
    assert missing == [("Kubernetes", "high"), ("GraphQL", "low")]  # most important first
    assert "3 of the 5 skills" in data["summary"]
    assert any("Kubernetes" in rec for rec in data["recommendations"])
    assert any("Acme" in rec for rec in data["recommendations"])


def test_basic_analysis_explains_when_no_known_skills_are_found():
    job = SimpleNamespace(title="Chef", company="", description="Cook food for our guests.")

    data = basic_analysis(SimpleNamespace(extracted_text=""), job)

    assert AnalysisOutputSerializer(data=data).is_valid()
    assert data["matched_skills"] == [] and data["missing_skills"] == []
    assert "can't measure the match" in data["summary"]


def test_basic_tailoring_only_reorders_and_never_adds_skills():
    content = {
        "summary": "Developer.",
        "skills": [
            {"category": "Languages", "items": "Git, Java, Python, Django"},
            {"category": "Other", "items": "Excel, Figma"},
        ],
        "experience": [{"company": "Acme", "bullets": ["Built things."]}],
        "projects": [{"name": "P", "tech": "React, PostgreSQL", "bullets": []}],
    }

    data = basic_tailoring(editable_fields(content), BACKEND_JOB)

    assert TailorOutputSerializer(data=data).is_valid()
    changes = validate_changes(content, data["changes"])
    assert [(c["path"], c["after"]) for c in changes] == [
        ("skills.0.items", "Python, Django, Git, Java"),
        ("projects.0.tech", "PostgreSQL, React"),
    ]
    for change in changes:
        assert sorted(change["after"].split(", ")) == sorted(change["before"].split(", "))
