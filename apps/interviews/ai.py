"""Interview AI tasks: question generation and answer evaluation, with basic fallbacks."""

from rest_framework import serializers

from apps.ai_services.prompts import UNTRUSTED_CONTENT_RULE, object_schema, string_list, tagged
from apps.ai_services.service import run_json_task
from apps.interviews import basic
from apps.interviews.rubric import COMPETENCY_CODES, rubric_text

QUESTIONS_FEATURE = "interview_questions"
EVALUATION_FEATURE = "answer_evaluation"
QUESTIONS_MAX_TOKENS = 16_000
EVALUATION_MAX_TOKENS = 12_000

# ---------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------

QUESTIONS_SYSTEM = f"""You are an experienced interviewer preparing a realistic mock interview.

Rules:
- Ask questions a real interviewer for this role would ask, ordered from warm-up to harder.
- Mix behavioural, situational and role-specific technical questions.
- When a resume is provided, refer to the candidate's actual experience where useful,
  but never assume experience the resume does not show.
- Ask one thing per question. Do not number the questions.
- For each question give the competency it mainly tests and short guidance describing
  what a strong answer would cover. The guidance is used later to evaluate the answer.
- {UNTRUSTED_CONTENT_RULE}"""

QUESTIONS_SCHEMA = object_schema(
    {
        "questions": string_list(
            object_schema(
                {
                    "text": {"type": "string"},
                    "competency": {"type": "string", "enum": COMPETENCY_CODES},
                    "guidance": {"type": "string"},
                }
            )
        )
    }
)


class _GeneratedQuestion(serializers.Serializer):
    text = serializers.CharField(max_length=1_000)
    competency = serializers.ChoiceField(choices=COMPETENCY_CODES)
    guidance = serializers.CharField(max_length=1_500)


def questions_serializer(count):
    class GeneratedQuestionsSerializer(serializers.Serializer):
        # Extra questions are ignored; fewer than requested is an invalid response.
        questions = serializers.ListField(
            child=_GeneratedQuestion(), min_length=count, max_length=30
        )

    return GeneratedQuestionsSerializer


def generate_questions(user, *, route, role_title, job_profile, resume, count, mode):
    """AI questions when `route` is set (falling back to basic ones), else basic questions."""
    parts = [f"Role: {role_title}"]
    if job_profile is not None:
        if job_profile.company:
            parts.append(f"Company: {job_profile.company}")
        parts.append(tagged("job_description", job_profile.description))
    if resume is not None:
        parts.append(tagged("resume", resume.extracted_text))
    spoken = (
        " The candidate will answer aloud, so each question should be answerable in one to "
        "three minutes of speech."
        if mode == "voice"
        else ""
    )
    parts.append(f"Write exactly {count} interview questions.{spoken}")

    result = run_json_task(
        user,
        feature=QUESTIONS_FEATURE,
        system=QUESTIONS_SYSTEM,
        prompt="\n\n".join(parts),
        schema=QUESTIONS_SCHEMA,
        max_output_tokens=QUESTIONS_MAX_TOKENS,
        serializer_class=questions_serializer(count),
        route=route,
        use_ai=route is not None,
        basic=lambda: basic.basic_questions(
            role_title=role_title, job_profile=job_profile, resume=resume, count=count
        ),
    )
    return result, result.data["questions"][:count]


# ---------------------------------------------------------------------------
# Answer evaluation
# ---------------------------------------------------------------------------

EVALUATION_SYSTEM = f"""You are an interview coach giving feedback on one mock interview answer.

Rules:
- Score the answer against the rubric. Scores are an approximate coaching aid based on
  this single answer, not a hiring decision.
- Judge only what the candidate actually said. Do not credit things they did not say,
  and quote or paraphrase briefly when you refer to their answer.
- If the answer was spoken, it is a speech-recognition transcript: ignore filler words,
  punctuation and likely transcription errors.
- Be constructive and specific. Every improvement should explain why it matters and
  how to do it better.
- {UNTRUSTED_CONTENT_RULE}"""

_SCORE = {"type": "integer", "enum": [1, 2, 3, 4, 5]}

EVALUATION_SCHEMA = object_schema(
    {
        "overall_score": _SCORE,
        "summary": {"type": "string"},
        "strengths": string_list(),
        "improvements": string_list(
            object_schema(
                {
                    "area": {"type": "string"},
                    "explanation": {"type": "string"},
                    "suggestion": {"type": "string"},
                }
            )
        ),
        "competency_scores": string_list(
            object_schema(
                {
                    "competency": {"type": "string", "enum": COMPETENCY_CODES},
                    "score": _SCORE,
                    "rationale": {"type": "string"},
                }
            )
        ),
    }
)


class _Improvement(serializers.Serializer):
    area = serializers.CharField(max_length=120)
    explanation = serializers.CharField(max_length=800)
    suggestion = serializers.CharField(max_length=800)


class _CompetencyScore(serializers.Serializer):
    competency = serializers.ChoiceField(choices=COMPETENCY_CODES)
    score = serializers.IntegerField(min_value=1, max_value=5)
    rationale = serializers.CharField(max_length=800)


class EvaluationOutputSerializer(serializers.Serializer):
    overall_score = serializers.IntegerField(min_value=1, max_value=5)
    summary = serializers.CharField(max_length=2_000)
    strengths = serializers.ListField(child=serializers.CharField(max_length=600), max_length=10)
    improvements = serializers.ListField(child=_Improvement(), min_length=1, max_length=10)
    competency_scores = serializers.ListField(child=_CompetencyScore())

    def validate_competency_scores(self, value):
        codes = [item["competency"] for item in value]
        if sorted(set(codes)) != sorted(COMPETENCY_CODES) or len(codes) != len(COMPETENCY_CODES):
            raise serializers.ValidationError(
                "Every rubric competency must be scored exactly once."
            )
        return value


def evaluate_answer(user, *, session, question, answer, options):
    answer_format = (
        "spoken, transcribed by speech recognition" if answer.input_mode == "voice" else "typed"
    )
    parts = [f"Role: {session.role_title}"]
    if session.job_profile is not None:
        parts.append(tagged("job_description", session.job_profile.description))
    parts += [
        "Rubric:\n" + rubric_text(session.role_title),
        f"Question (mainly tests {question.competency}): {question.text}",
        f"What a strong answer would cover: {question.guidance}",
        f"Answer format: {answer_format}",
        tagged("answer", answer.text),
        "Evaluate the answer.",
    ]
    return run_json_task(
        user,
        feature=EVALUATION_FEATURE,
        system=EVALUATION_SYSTEM,
        prompt="\n\n".join(parts),
        schema=EVALUATION_SCHEMA,
        max_output_tokens=EVALUATION_MAX_TOKENS,
        serializer_class=EvaluationOutputSerializer,
        options=options,
        basic=lambda: basic.basic_evaluation(session=session, question=question, answer=answer),
    )
