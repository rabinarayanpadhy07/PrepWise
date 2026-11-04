from rest_framework import serializers

from apps.ai_services.models import AIPlatformSettings
from apps.ai_services.serializers import AIOptionsSerializer
from apps.ai_services.service import SOURCE_BASIC
from apps.interviews.models import (
    MAX_QUESTIONS,
    MIN_QUESTIONS,
    AnswerEvaluation,
    InterviewAnswer,
    InterviewQuestion,
    InterviewSession,
)
from apps.interviews.rubric import competency_label
from apps.interviews.services import current_position
from apps.resumes.models import JobProfile, Resume
from apps.resumes.serializers import JobProfileBriefSerializer
from apps.usage.services import voice_recording_limit

MAX_ANSWER_CHARS = 8_000


class CompetencyScoreSerializer(serializers.Serializer):
    competency = serializers.CharField()
    label = serializers.SerializerMethodField()
    score = serializers.IntegerField()
    rationale = serializers.CharField()

    def get_label(self, obj) -> str:
        return competency_label(obj.competency)


class EvaluationSerializer(serializers.ModelSerializer):
    competency_scores = CompetencyScoreSerializer(many=True, read_only=True)
    # Lets clients label feedback as AI-generated, or as basic (rule-based) feedback.
    ai_generated = serializers.SerializerMethodField()

    class Meta:
        model = AnswerEvaluation
        fields = [
            "id",
            "overall_score",
            "summary",
            "strengths",
            "improvements",
            "competency_scores",
            "rubric_version",
            "ai_generated",
            "ai_source",
            "ai_mode",
            "ai_provider",
            "ai_model",
            "created_at",
        ]
        read_only_fields = fields

    def get_ai_generated(self, obj) -> bool:
        return obj.ai_source != SOURCE_BASIC


class AnswerSerializer(serializers.ModelSerializer):
    class Meta:
        model = InterviewAnswer
        fields = ["text", "input_mode", "voice_seconds", "submitted_at"]
        read_only_fields = fields


class QuestionSerializer(serializers.ModelSerializer):
    competency_label = serializers.SerializerMethodField()
    answer = serializers.SerializerMethodField()
    evaluation = serializers.SerializerMethodField()

    class Meta:
        model = InterviewQuestion
        fields = ["position", "text", "competency", "competency_label", "answer", "evaluation"]
        read_only_fields = fields

    def get_competency_label(self, obj) -> str:
        return competency_label(obj.competency)

    def get_answer(self, obj) -> dict | None:
        answer = getattr(obj, "answer", None)
        return AnswerSerializer(answer).data if answer else None

    def get_evaluation(self, obj) -> dict | None:
        answer = getattr(obj, "answer", None)
        evaluation = getattr(answer, "evaluation", None) if answer else None
        return EvaluationSerializer(evaluation).data if evaluation else None


class SessionListSerializer(serializers.ModelSerializer):
    answered_count = serializers.IntegerField(read_only=True)
    average_score = serializers.FloatField(read_only=True, allow_null=True)

    class Meta:
        model = InterviewSession
        fields = [
            "id",
            "role_title",
            "mode",
            "status",
            "question_count",
            "answered_count",
            "average_score",
            "created_at",
            "completed_at",
        ]
        read_only_fields = fields


class SessionDetailSerializer(serializers.ModelSerializer):
    job_profile = JobProfileBriefSerializer(read_only=True)
    questions = QuestionSerializer(many=True, read_only=True)
    current_position = serializers.SerializerMethodField()
    max_voice_answer_seconds = serializers.SerializerMethodField()

    class Meta:
        model = InterviewSession
        fields = [
            "id",
            "role_title",
            "job_profile",
            "mode",
            "status",
            "question_count",
            "current_position",
            "max_voice_answer_seconds",
            "questions",
            "ai_source",
            "ai_mode",
            "ai_provider",
            "ai_model",
            "created_at",
            "completed_at",
        ]
        read_only_fields = fields

    def get_current_position(self, obj) -> int | None:
        if obj.status != InterviewSession.Status.IN_PROGRESS:
            return None
        return current_position(obj)

    def get_max_voice_answer_seconds(self, obj) -> int:
        """Seconds the owner may record for the next answer (cap and remaining allowance)."""
        if obj.mode != InterviewSession.Mode.VOICE:
            return 0
        return voice_recording_limit(obj.owner)


class SessionCreateSerializer(AIOptionsSerializer):
    job_profile_id = serializers.PrimaryKeyRelatedField(
        queryset=JobProfile.objects.none(),
        required=False,
        allow_null=True,
        error_messages={"does_not_exist": "Job description not found."},
    )
    resume_id = serializers.PrimaryKeyRelatedField(
        queryset=Resume.objects.none(),
        required=False,
        allow_null=True,
        error_messages={"does_not_exist": "Resume not found."},
    )
    role_title = serializers.CharField(max_length=150, required=False, allow_blank=True)
    mode = serializers.ChoiceField(choices=InterviewSession.Mode.choices)
    question_count = serializers.IntegerField(
        min_value=MIN_QUESTIONS, max_value=MAX_QUESTIONS, default=5
    )
    # Client-generated key: retrying the same start returns the same session.
    idempotency_key = serializers.UUIDField(required=False, allow_null=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")  # absent during schema generation
        if request is not None:
            user = request.user
            self.fields["job_profile_id"].queryset = JobProfile.objects.filter(owner=user)
            self.fields["resume_id"].queryset = Resume.objects.filter(owner=user)

    def validate(self, attrs):
        job_profile = attrs.get("job_profile_id")
        role_title = (attrs.get("role_title") or "").strip() or (
            job_profile.title if job_profile else ""
        )
        if not role_title:
            raise serializers.ValidationError(
                {"role_title": ["Enter a role or choose a saved job description."]}
            )
        attrs["role_title"] = role_title
        return attrs


class AnswerSubmitSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=MAX_ANSWER_CHARS, trim_whitespace=True)
    input_mode = serializers.ChoiceField(choices=InterviewAnswer.InputMode.choices, default="text")
    voice_seconds = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    idempotency_key = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs["input_mode"] == InterviewAnswer.InputMode.VOICE:
            seconds = attrs.get("voice_seconds")
            limit = AIPlatformSettings.load().voice_max_answer_seconds
            if not seconds:
                raise serializers.ValidationError(
                    {"voice_seconds": ["Recording length is required for voice answers."]}
                )
            if seconds > limit:
                raise serializers.ValidationError(
                    {"voice_seconds": [f"Voice answers can be at most {limit} seconds long."]}
                )
        return attrs
