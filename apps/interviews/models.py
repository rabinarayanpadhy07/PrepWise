from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.interviews.rubric import COMPETENCY_CHOICES, COMPETENCY_CODES

MIN_QUESTIONS = 3
MAX_QUESTIONS = 10


class InterviewSession(models.Model):
    class Mode(models.TextChoices):
        TEXT = "text", "Text"
        VOICE = "voice", "Voice"

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="interview_sessions"
    )
    job_profile = models.ForeignKey(
        "resumes.JobProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="interview_sessions",
    )
    resume = models.ForeignKey(
        "resumes.Resume",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="interview_sessions",
    )
    role_title = models.CharField(max_length=150)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.TEXT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_PROGRESS)
    question_count = models.PositiveSmallIntegerField()
    # How the questions were generated; evaluations default to the same choices.
    ai_source = models.CharField(max_length=20, blank=True)
    ai_mode = models.CharField(max_length=20, blank=True)
    ai_provider = models.CharField(max_length=30, blank=True)
    ai_model = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(mode__in=["text", "voice"]), name="interview_session_mode_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["in_progress", "completed"]),
                name="interview_session_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(question_count__gte=MIN_QUESTIONS)
                & Q(question_count__lte=MAX_QUESTIONS),
                name="interview_session_question_count_range",
            ),
            models.CheckConstraint(
                condition=~Q(status="completed") | Q(completed_at__isnull=False),
                name="interview_session_completed_has_time",
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="interview_owner_created_idx"),
            models.Index(fields=["owner", "status"], name="interview_owner_status_idx"),
        ]

    def __str__(self):
        return f"{self.role_title} ({self.mode}, {self.status})"


class InterviewQuestion(models.Model):
    session = models.ForeignKey(
        InterviewSession, on_delete=models.CASCADE, related_name="questions"
    )
    position = models.PositiveSmallIntegerField()
    text = models.TextField()
    competency = models.CharField(max_length=30, choices=COMPETENCY_CHOICES)
    # What a strong answer covers. Used by the evaluator; not shown before answering.
    guidance = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["session", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "position"], name="interview_question_unique_position"
            ),
            models.CheckConstraint(
                condition=Q(position__gte=1), name="interview_question_position_positive"
            ),
            models.CheckConstraint(
                condition=Q(competency__in=COMPETENCY_CODES),
                name="interview_question_competency_valid",
            ),
        ]

    def __str__(self):
        return f"Q{self.position} of session {self.session_id}"


class InterviewAnswer(models.Model):
    class InputMode(models.TextChoices):
        TEXT = "text", "Typed"
        VOICE = "voice", "Spoken (transcribed in the browser)"

    session = models.ForeignKey(InterviewSession, on_delete=models.CASCADE, related_name="answers")
    question = models.OneToOneField(
        InterviewQuestion, on_delete=models.CASCADE, related_name="answer"
    )
    text = models.TextField()
    input_mode = models.CharField(max_length=10, choices=InputMode.choices)
    voice_seconds = models.PositiveSmallIntegerField(null=True, blank=True)
    # Client-generated key that makes resubmitting the same answer safe.
    idempotency_key = models.UUIDField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    # Set while an evaluation request is running, so concurrent requests don't
    # pay for the same evaluation twice.
    evaluation_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["session", "question__position"]
        constraints = [
            models.CheckConstraint(
                condition=Q(input_mode__in=["text", "voice"]), name="interview_answer_mode_valid"
            ),
            models.CheckConstraint(condition=~Q(text=""), name="interview_answer_text_not_empty"),
            models.CheckConstraint(
                condition=Q(input_mode="text") | Q(voice_seconds__gt=0),
                name="interview_answer_voice_has_duration",
            ),
        ]
        indexes = [models.Index(fields=["session"], name="interview_answer_session_idx")]

    def __str__(self):
        return f"Answer to question {self.question_id}"


class AnswerEvaluation(models.Model):
    """AI-generated feedback on one answer, scored against the rubric."""

    session = models.ForeignKey(
        InterviewSession, on_delete=models.CASCADE, related_name="evaluations"
    )
    answer = models.OneToOneField(
        InterviewAnswer, on_delete=models.CASCADE, related_name="evaluation"
    )
    overall_score = models.PositiveSmallIntegerField()
    summary = models.TextField()
    strengths = models.JSONField(default=list)
    improvements = models.JSONField(default=list)
    rubric_version = models.CharField(max_length=30)
    ai_source = models.CharField(max_length=20)
    ai_mode = models.CharField(max_length=20, blank=True)
    ai_provider = models.CharField(max_length=30)
    ai_model = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(overall_score__gte=1) & Q(overall_score__lte=5),
                name="answer_evaluation_score_range",
            ),
        ]
        indexes = [
            models.Index(fields=["session"], name="answer_evaluation_session_idx"),
            models.Index(fields=["-created_at"], name="answer_evaluation_created_idx"),
        ]

    def __str__(self):
        return f"Evaluation of answer {self.answer_id}: {self.overall_score}/5"


class CompetencyScore(models.Model):
    evaluation = models.ForeignKey(
        AnswerEvaluation, on_delete=models.CASCADE, related_name="competency_scores"
    )
    session = models.ForeignKey(
        InterviewSession, on_delete=models.CASCADE, related_name="competency_scores"
    )
    competency = models.CharField(max_length=30, choices=COMPETENCY_CHOICES)
    score = models.PositiveSmallIntegerField()
    rationale = models.TextField()

    class Meta:
        ordering = ["evaluation", "competency"]
        constraints = [
            models.UniqueConstraint(
                fields=["evaluation", "competency"], name="competency_score_unique_per_evaluation"
            ),
            models.CheckConstraint(
                condition=Q(score__gte=1) & Q(score__lte=5), name="competency_score_range"
            ),
            models.CheckConstraint(
                condition=Q(competency__in=COMPETENCY_CODES), name="competency_score_code_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["session", "competency"], name="competency_score_session_idx")
        ]

    def __str__(self):
        return f"{self.competency}: {self.score}/5"
