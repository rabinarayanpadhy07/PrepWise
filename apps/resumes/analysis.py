"""Resume-versus-job analysis: AI via the shared AI service, with a basic keyword fallback."""

import logging

from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import APIException

from apps.ai_services.prompts import UNTRUSTED_CONTENT_RULE, object_schema, string_list, tagged
from apps.ai_services.service import run_json_task
from apps.resumes.basic import basic_analysis
from apps.resumes.models import ResumeAnalysis

logger = logging.getLogger("prepwise.resumes")

FEATURE = "resume_analysis"
MAX_TOKENS = 16_000

SYSTEM_PROMPT = f"""You are an experienced interview coach and technical recruiter.
You compare a candidate's resume with a job description and help the candidate prepare.

Rules:
- Base every statement on the resume and job description provided. Do not invent
  experience, employers, qualifications or achievements.
- A skill counts as matched only if the resume gives evidence for it; quote or
  paraphrase that evidence briefly.
- Missing skills are requirements or strong preferences in the job description that the
  resume does not show. Rate their importance from the job description's wording.
- Interview questions must be specific to this role and this candidate's background.
- Write in clear, professional English. Be direct and constructive.
- {UNTRUSTED_CONTENT_RULE}"""

SCHEMA = object_schema(
    {
        "summary": {"type": "string"},
        "matched_skills": string_list(
            object_schema({"skill": {"type": "string"}, "evidence": {"type": "string"}})
        ),
        "missing_skills": string_list(
            object_schema(
                {
                    "skill": {"type": "string"},
                    "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                    "suggestion": {"type": "string"},
                }
            )
        ),
        "recommendations": string_list(),
        "questions": string_list(
            object_schema(
                {
                    "question": {"type": "string"},
                    "focus": {"type": "string"},
                    "reason": {"type": "string"},
                }
            )
        ),
    }
)


class _MatchedSkill(serializers.Serializer):
    skill = serializers.CharField(max_length=120)
    evidence = serializers.CharField(max_length=600)


class _MissingSkill(serializers.Serializer):
    skill = serializers.CharField(max_length=120)
    importance = serializers.ChoiceField(choices=["high", "medium", "low"])
    suggestion = serializers.CharField(max_length=600)


class _Question(serializers.Serializer):
    question = serializers.CharField(max_length=600)
    focus = serializers.CharField(max_length=120)
    reason = serializers.CharField(max_length=600)


class AnalysisOutputSerializer(serializers.Serializer):
    """Validates the model's JSON before anything is saved."""

    summary = serializers.CharField(max_length=3_000)
    matched_skills = serializers.ListField(child=_MatchedSkill(), max_length=40)
    missing_skills = serializers.ListField(child=_MissingSkill(), max_length=40)
    recommendations = serializers.ListField(
        child=serializers.CharField(max_length=600), min_length=1, max_length=15
    )
    questions = serializers.ListField(child=_Question(), min_length=3, max_length=20)


def build_prompt(resume, job_profile):
    company = f" at {job_profile.company}" if job_profile.company else ""
    return "\n\n".join(
        [
            f"Target role: {job_profile.title}{company}",
            tagged("job_description", job_profile.description),
            tagged("resume", resume.extracted_text),
            "Produce: a short summary of how well the resume fits the role; matched skills "
            "with evidence; missing skills with importance and a practical suggestion; up to "
            "8 preparation recommendations; and 8 to 12 likely interview questions for this "
            "role, each with its focus area and why it is likely to be asked.",
        ]
    )


def run_analysis(analysis, options=None):
    """
    Run the analysis and store the outcome. Users without a plan, and subscribers
    whose AI request fails, get the basic keyword analysis (ai_source "basic").
    """
    try:
        result = run_json_task(
            analysis.owner,
            feature=FEATURE,
            system=SYSTEM_PROMPT,
            prompt=build_prompt(analysis.resume, analysis.job_profile),
            schema=SCHEMA,
            max_output_tokens=MAX_TOKENS,
            serializer_class=AnalysisOutputSerializer,
            options=options,
            basic=lambda: basic_analysis(analysis.resume, analysis.job_profile),
        )
    except APIException as exc:
        analysis.status = ResumeAnalysis.Status.FAILED
        analysis.error_code = getattr(exc, "default_code", "error")[:50]
        analysis.save(update_fields=["status", "error_code"])
        raise

    data = result.data
    analysis.status = ResumeAnalysis.Status.COMPLETED
    analysis.summary = data["summary"]
    analysis.matched_skills = data["matched_skills"]
    analysis.missing_skills = data["missing_skills"]
    analysis.recommendations = data["recommendations"]
    analysis.questions = data["questions"]
    analysis.error_code = ""
    analysis.ai_source = result.source
    analysis.ai_mode = result.ai_mode
    analysis.ai_provider = result.provider
    analysis.ai_model = result.model
    analysis.completed_at = timezone.now()
    analysis.save()
    logger.info(
        "Resume analysis %s completed (%s, %s tokens)",
        analysis.pk,
        result.source,
        result.total_tokens,
    )
    return analysis
