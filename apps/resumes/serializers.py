from rest_framework import serializers

from apps.ai_services.errors import AIServiceError
from apps.ai_services.serializers import AIOptionsSerializer
from apps.resumes.builder import clean_content
from apps.resumes.models import BuiltResume, JobProfile, Resume, ResumeAnalysis
from apps.usage.errors import UsageError


def _error_messages():
    """Stable failure code -> user-facing message, for every AI and usage error."""
    messages = {}
    pending = [AIServiceError, UsageError]
    while pending:
        cls = pending.pop()
        messages[cls.default_code] = str(cls.default_detail)
        pending.extend(cls.__subclasses__())
    return messages


FAILURE_MESSAGES = _error_messages()


class ResumeSerializer(serializers.ModelSerializer):
    text_length = serializers.IntegerField(read_only=True)

    class Meta:
        model = Resume
        fields = [
            "id",
            "original_filename",
            "file_type",
            "size_bytes",
            "page_count",
            "text_length",
            "created_at",
        ]
        read_only_fields = fields


class ResumeDetailSerializer(ResumeSerializer):
    class Meta(ResumeSerializer.Meta):
        fields = [*ResumeSerializer.Meta.fields, "extracted_text"]
        read_only_fields = fields


class ResumeUploadSerializer(serializers.Serializer):
    file = serializers.FileField(allow_empty_file=False, max_length=255)


class JobProfileSerializer(serializers.ModelSerializer):
    title = serializers.CharField(max_length=150, trim_whitespace=True)
    company = serializers.CharField(max_length=150, required=False, allow_blank=True)
    description = serializers.CharField(
        min_length=50,
        max_length=20_000,
        error_messages={
            "min_length": "Paste the full job description (at least 50 characters).",
            "max_length": "The job description must be 20,000 characters or fewer.",
        },
    )

    class Meta:
        model = JobProfile
        fields = ["id", "title", "company", "description", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class _ResumeBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Resume
        fields = ["id", "original_filename", "file_type"]


class JobProfileBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobProfile
        fields = ["id", "title", "company"]


class ResumeAnalysisSerializer(serializers.ModelSerializer):
    resume = _ResumeBriefSerializer(read_only=True)
    job_profile = JobProfileBriefSerializer(read_only=True)
    error_message = serializers.SerializerMethodField()

    class Meta:
        model = ResumeAnalysis
        fields = [
            "id",
            "status",
            "resume",
            "job_profile",
            "summary",
            "matched_skills",
            "missing_skills",
            "recommendations",
            "questions",
            "error_code",
            "error_message",
            "ai_source",
            "ai_mode",
            "ai_provider",
            "ai_model",
            "created_at",
            "completed_at",
        ]
        read_only_fields = fields

    def get_error_message(self, obj) -> str:
        if obj.status != ResumeAnalysis.Status.FAILED:
            return ""
        return FAILURE_MESSAGES.get(obj.error_code, "The analysis couldn't be completed.")


class ResumeAnalysisCreateSerializer(AIOptionsSerializer):
    resume_id = serializers.PrimaryKeyRelatedField(
        queryset=Resume.objects.none(),
        error_messages={
            "does_not_exist": "Resume not found.",
            "incorrect_type": "Resume not found.",
        },
    )
    job_profile_id = serializers.PrimaryKeyRelatedField(
        queryset=JobProfile.objects.none(),
        error_messages={
            "does_not_exist": "Job description not found.",
            "incorrect_type": "Job description not found.",
        },
    )
    # Client-generated key: retrying the same request returns the same analysis.
    idempotency_key = serializers.UUIDField(required=False, allow_null=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only the requester's own records can be referenced; other IDs look nonexistent.
        request = self.context.get("request")  # absent during schema generation
        if request is not None:
            self.fields["resume_id"].queryset = Resume.objects.filter(owner=request.user)
            self.fields["job_profile_id"].queryset = JobProfile.objects.filter(owner=request.user)


# ---------------------------------------------------------------------------
# Resume maker
# ---------------------------------------------------------------------------


def _owned_job_profile_field(**kwargs):
    return serializers.PrimaryKeyRelatedField(
        queryset=JobProfile.objects.none(),
        error_messages={
            "does_not_exist": "Job description not found.",
            "incorrect_type": "Job description not found.",
        },
        **kwargs,
    )


class _OwnedRecordsMixin:
    """Limits record fields to the requester's own rows; other IDs look nonexistent."""

    owned_fields = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Schema generation has no request or an anonymous one: leave the querysets empty.
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            for name, model in self.owned_fields.items():
                self.fields[name].queryset = model.objects.filter(owner=request.user)


class BuiltResumeListSerializer(serializers.ModelSerializer):
    job_profile_title = serializers.CharField(
        source="job_profile.title", read_only=True, default=None
    )

    class Meta:
        model = BuiltResume
        fields = ["id", "title", "job_profile_title", "created_at", "updated_at"]
        read_only_fields = fields


class BuiltResumeSerializer(_OwnedRecordsMixin, serializers.ModelSerializer):
    """A resume-maker resume. `content` follows builder.ResumeContentSerializer."""

    owned_fields = {"job_profile_id": JobProfile}

    content = serializers.JSONField(required=False)
    job_profile_id = _owned_job_profile_field(source="job_profile", required=False, allow_null=True)
    job_profile_title = serializers.CharField(
        source="job_profile.title", read_only=True, default=None
    )

    class Meta:
        model = BuiltResume
        fields = [
            "id",
            "title",
            "content",
            "job_profile_id",
            "job_profile_title",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "job_profile_title", "created_at", "updated_at"]

    def validate_title(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Enter a title.")
        return value

    def validate_content(self, value):
        try:
            return clean_content(value)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError(exc.detail) from exc

    def create(self, validated_data):
        validated_data.setdefault("content", clean_content({}))
        return super().create(validated_data)


class BuiltResumeImportSerializer(_OwnedRecordsMixin, AIOptionsSerializer):
    owned_fields = {"resume_id": Resume}

    resume_id = serializers.PrimaryKeyRelatedField(
        queryset=Resume.objects.none(),
        error_messages={
            "does_not_exist": "Resume not found.",
            "incorrect_type": "Resume not found.",
        },
    )
    title = serializers.CharField(max_length=150, required=False, allow_blank=True)


class TailorRequestSerializer(_OwnedRecordsMixin, AIOptionsSerializer):
    owned_fields = {"job_profile_id": JobProfile}

    job_profile_id = _owned_job_profile_field()
    # Optional unsaved content from the editor; defaults to the saved content.
    content = serializers.JSONField(required=False)

    def validate_content(self, value):
        return clean_content(value)


class TailorChangeSerializer(serializers.Serializer):
    path = serializers.CharField()
    label = serializers.CharField()
    before = serializers.CharField()
    after = serializers.CharField()
    reason = serializers.CharField()


class TailorResponseSerializer(serializers.Serializer):
    job_profile = JobProfileSerializer()
    # "platform" (AI), "basic" (rule-based, not AI-generated) or "" (nothing to edit).
    source = serializers.CharField(allow_blank=True)
    changes = TailorChangeSerializer(many=True)
