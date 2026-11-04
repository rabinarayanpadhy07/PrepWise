"""
Interview session state machine.

  start_session -> in_progress -> (submit_answer, evaluate)* -> complete_session

Answer submission and evaluation are separate steps so a provider failure
never loses an answer: the answer is saved first and evaluation can be retried.
Both steps are idempotent, so client retries are safe.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException

from apps.ai_services.errors import AIServiceError
from apps.ai_services.service import AIRequestOptions, resolve_route
from apps.interviews import ai
from apps.interviews.models import (
    AnswerEvaluation,
    CompetencyScore,
    InterviewAnswer,
    InterviewQuestion,
    InterviewSession,
)
from apps.interviews.rubric import RUBRIC_VERSION
from apps.usage import errors as usage_errors
from apps.usage import idempotency
from apps.usage.services import (
    charge_voice,
    commit_reservation,
    release_reservation,
    reserve_interview,
    voice_seconds_available,
)

# An evaluation claim older than this is treated as abandoned.
EVALUATION_CLAIM_TTL = timedelta(minutes=10)


class InterviewConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "interview_conflict"
    default_detail = "This action isn't possible in the interview's current state."


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def start_session(
    user, *, role_title, job_profile, resume, mode, question_count, options, idempotency_key=None
):
    """Generate questions and create a session. Returns (session, created)."""
    record, existing_id = idempotency.claim(user, "interview_start", idempotency_key)
    if existing_id:
        return InterviewSession.objects.get(pk=existing_id, owner=user), False
    try:
        session = _start_session(
            user,
            role_title=role_title,
            job_profile=job_profile,
            resume=resume,
            mode=mode,
            question_count=question_count,
            options=options,
        )
    except BaseException:
        idempotency.abandon(record)
        raise
    idempotency.complete(record, session.pk)
    return session, True


def _start_session(user, *, role_title, job_profile, resume, mode, question_count, options):
    # Subscribers get AI questions, held against their interview allowance.
    # Without a plan, or when AI or the allowance isn't available, the session
    # uses basic questions instead and nothing is charged.
    route = reservation = None
    try:
        route = resolve_route(user, options)
        # Hold one interview so concurrent starts can't exceed the allowance; it
        # is given back if the AI fails and basic questions are used instead.
        reservation = reserve_interview(user)
    except (AIServiceError, usage_errors.UsageError):
        route = None
    try:
        result, questions = ai.generate_questions(
            user,
            route=route,
            role_title=role_title,
            job_profile=job_profile,
            resume=resume,
            count=question_count,
            mode=mode,
        )
        with transaction.atomic():
            session = InterviewSession.objects.create(
                owner=user,
                job_profile=job_profile,
                resume=resume,
                role_title=role_title,
                mode=mode,
                question_count=question_count,
                ai_source=result.source,
                ai_mode=result.ai_mode,
                ai_provider=result.provider,
                ai_model=result.model,
            )
            InterviewQuestion.objects.bulk_create(
                InterviewQuestion(
                    session=session,
                    position=index,
                    text=item["text"],
                    competency=item["competency"],
                    guidance=item["guidance"],
                )
                for index, item in enumerate(questions, start=1)
            )
            if reservation is not None:
                if result.is_basic:
                    release_reservation(reservation)
                else:
                    commit_reservation(reservation)
    except BaseException:
        if reservation is not None:
            release_reservation(reservation)
        raise
    return session


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


def current_position(session):
    """Position of the first unanswered question, or None when all are answered."""
    answered = set(session.answers.values_list("question__position", flat=True))
    for position in range(1, session.question_count + 1):
        if position not in answered:
            return position
    return None


def submit_answer(session, question, *, text, input_mode, voice_seconds=None, idempotency_key=None):
    """Save an answer. Returns (answer, created). Resubmitting the same answer is a no-op."""
    with transaction.atomic():
        # Serialise answer submissions for this session.
        session = InterviewSession.objects.select_for_update().get(pk=session.pk)

        existing = InterviewAnswer.objects.filter(question=question).first()
        if existing is not None:
            same_request = (
                idempotency_key is not None and existing.idempotency_key == idempotency_key
            )
            if same_request or existing.text == text:
                return existing, False
            raise InterviewConflict(
                "This question has already been answered.", code="already_answered"
            )

        if session.status != InterviewSession.Status.IN_PROGRESS:
            raise InterviewConflict("This interview has finished.", code="interview_finished")
        if question.position != current_position(session):
            raise InterviewConflict(
                "Answer the current question before moving on.", code="out_of_order"
            )

        if input_mode == InterviewAnswer.InputMode.VOICE:
            if session.mode != InterviewSession.Mode.VOICE:
                raise InterviewConflict(
                    "Voice answers are only accepted in voice interviews.", code="voice_not_allowed"
                )
            _charge_voice(session, voice_seconds)
        else:
            voice_seconds = None

        answer = InterviewAnswer.objects.create(
            session=session,
            question=question,
            text=text,
            input_mode=input_mode,
            voice_seconds=voice_seconds,
            idempotency_key=idempotency_key,
        )
        session.save(update_fields=["updated_at"])
    return answer, True


def _charge_voice(session, seconds):
    """Voice time counts against a subscriber's plan while some is left. Without a
    plan, or once it's used up, voice answers are still accepted (transcription
    happens in the browser and costs nothing)."""
    available = voice_seconds_available(session.owner)
    if available <= 0:
        return
    try:
        charge_voice(session.owner, seconds=min(seconds, available))
    except usage_errors.UsageError:
        pass


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def session_ai_options(session, overrides=None):
    """Evaluations use the session's AI mode unless the request overrides it."""
    overrides = overrides or AIRequestOptions()
    return AIRequestOptions(ai_mode=overrides.ai_mode or session.ai_mode or None)


def evaluate(session, question, options=None):
    """Evaluate the question's answer. Returns (evaluation, created)."""
    answer = InterviewAnswer.objects.filter(question=question).first()
    if answer is None:
        raise InterviewConflict(
            "Answer this question before requesting feedback.", code="not_answered"
        )

    existing = AnswerEvaluation.objects.filter(answer=answer).first()
    if existing is not None:
        return existing, False

    # Claim the answer so a concurrent request doesn't pay for a second evaluation.
    now = timezone.now()
    claimed = (
        InterviewAnswer.objects.filter(pk=answer.pk)
        .exclude(evaluation_started_at__gt=now - EVALUATION_CLAIM_TTL)
        .update(evaluation_started_at=now)
    )
    if not claimed:
        existing = AnswerEvaluation.objects.filter(answer=answer).first()
        if existing is not None:
            return existing, False
        raise InterviewConflict(
            "Feedback for this answer is already being prepared.", code="evaluation_in_progress"
        )

    try:
        result = ai.evaluate_answer(
            session.owner,
            session=session,
            question=question,
            answer=answer,
            options=session_ai_options(session, options),
        )
    except BaseException:
        InterviewAnswer.objects.filter(pk=answer.pk).update(evaluation_started_at=None)
        raise
    data = result.data

    try:
        with transaction.atomic():
            evaluation = AnswerEvaluation.objects.create(
                session=session,
                answer=answer,
                overall_score=data["overall_score"],
                summary=data["summary"],
                strengths=data["strengths"],
                improvements=data["improvements"],
                rubric_version=RUBRIC_VERSION,
                ai_source=result.source,
                ai_mode=result.ai_mode,
                ai_provider=result.provider,
                ai_model=result.model,
            )
            CompetencyScore.objects.bulk_create(
                CompetencyScore(
                    evaluation=evaluation,
                    session=session,
                    competency=item["competency"],
                    score=item["score"],
                    rationale=item["rationale"],
                )
                for item in data["competency_scores"]
            )
    except IntegrityError:
        # A concurrent request evaluated the same answer first; keep that one.
        return AnswerEvaluation.objects.get(answer=answer), False
    return evaluation, True


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def complete_session(session):
    """Finish the interview. Unanswered questions stay unanswered. Idempotent."""
    with transaction.atomic():
        session = InterviewSession.objects.select_for_update().get(pk=session.pk)
        if session.status != InterviewSession.Status.COMPLETED:
            session.status = InterviewSession.Status.COMPLETED
            session.completed_at = timezone.now()
            session.save(update_fields=["status", "completed_at", "updated_at"])
    return session
