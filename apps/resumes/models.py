from django.conf import settings
from django.db import models
from django.db.models import Q


class Resume(models.Model):
    """
    An uploaded resume. Only the extracted text and metadata are stored; the
    original file is not kept after extraction.
    """

    class FileType(models.TextChoices):
        PDF = "pdf", "PDF"
        DOCX = "docx", "DOCX"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="resumes"
    )
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=10, choices=FileType.choices)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    page_count = models.PositiveSmallIntegerField(null=True, blank=True)
    extracted_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(size_bytes__gt=0), name="resume_size_positive"),
            models.CheckConstraint(
                condition=Q(file_type__in=["pdf", "docx"]), name="resume_file_type_valid"
            ),
        ]
        indexes = [models.Index(fields=["owner", "-created_at"], name="resume_owner_created_idx")]

    def __str__(self):
        return f"{self.original_filename} ({self.owner_id})"


class JobProfile(models.Model):
    """A target role: title, optional company, and the job description text."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="job_profiles"
    )
    title = models.CharField(max_length=150)
    company = models.CharField(max_length=150, blank=True)
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(title=""), name="job_profile_title_not_empty"),
        ]
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="job_profile_owner_created_idx")
        ]

    def __str__(self):
        return self.title


class ResumeAnalysis(models.Model):
    """AI comparison of one resume against one job profile."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="resume_analyses"
    )
    resume = models.ForeignKey(Resume, on_delete=models.CASCADE, related_name="analyses")
    job_profile = models.ForeignKey(JobProfile, on_delete=models.CASCADE, related_name="analyses")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Results (filled in when completed).
    summary = models.TextField(blank=True)
    matched_skills = models.JSONField(default=list, blank=True)
    missing_skills = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    questions = models.JSONField(default=list, blank=True)
    # Failure details (stable error code only; never provider output).
    error_code = models.CharField(max_length=50, blank=True)
    ai_source = models.CharField(max_length=20, blank=True)
    ai_mode = models.CharField(max_length=20, blank=True)
    ai_provider = models.CharField(max_length=30, blank=True)
    ai_model = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # When the current attempt started; a pending attempt older than the stale
    # window may be retried.
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "resume analyses"
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["pending", "completed", "failed"]),
                name="resume_analysis_status_valid",
            ),
            models.CheckConstraint(
                condition=~Q(status="completed") | Q(completed_at__isnull=False),
                name="resume_analysis_completed_has_time",
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="analysis_owner_created_idx"),
        ]

    def __str__(self):
        return f"Analysis {self.pk} ({self.status})"


class BuiltResume(models.Model):
    """
    A resume made in the resume maker. Only structured content is stored
    (see apps.resumes.builder.ResumeContentSerializer); the frontend fills the
    fixed LaTeX template with it, so the layout never changes.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="built_resumes"
    )
    title = models.CharField(max_length=150)
    content = models.JSONField(default=dict)
    # The job description this version was last tailored to, if any.
    job_profile = models.ForeignKey(
        JobProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="built_resumes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(title=""), name="built_resume_title_not_empty"),
        ]
        indexes = [
            models.Index(fields=["owner", "-updated_at"], name="built_resume_owner_upd_idx"),
        ]

    def __str__(self):
        return self.title
