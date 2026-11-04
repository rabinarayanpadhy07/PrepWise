from django.db.models import Avg, Count, Prefetch
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.generics import ListAPIView, RetrieveAPIView, get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.ai_services.serializers import AIOptionsSerializer
from apps.interviews import services
from apps.interviews.models import (
    AnswerEvaluation,
    CompetencyScore,
    InterviewQuestion,
    InterviewSession,
)
from apps.interviews.rubric import COMPETENCIES
from apps.interviews.serializers import (
    AnswerSerializer,
    AnswerSubmitSerializer,
    EvaluationSerializer,
    SessionCreateSerializer,
    SessionDetailSerializer,
    SessionListSerializer,
)


def owned_sessions(user):
    return InterviewSession.objects.filter(owner=user)


def detail_queryset(user):
    return (
        owned_sessions(user)
        .select_related("job_profile")
        .prefetch_related(
            Prefetch(
                "questions",
                queryset=InterviewQuestion.objects.select_related(
                    "answer__evaluation"
                ).prefetch_related("answer__evaluation__competency_scores"),
            )
        )
    )


def _detail_response(request, session, response_status=status.HTTP_200_OK):
    session = get_object_or_404(detail_queryset(request.user), pk=session.pk)
    return Response(SessionDetailSerializer(session).data, status=response_status)


class _AIThrottleOnPostMixin:
    throttle_scope = "ai"

    def get_throttles(self):
        if self.request.method == "POST":
            return [ScopedRateThrottle()]
        return super().get_throttles()


@extend_schema(
    tags=["interviews"],
    parameters=[OpenApiParameter("status", str, enum=["in_progress", "completed"])],
)
class InterviewListCreateView(_AIThrottleOnPostMixin, ListAPIView):
    serializer_class = SessionListSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Aggregation drops Meta.ordering, so order explicitly for stable pagination.
        queryset = (
            owned_sessions(self.request.user)
            .annotate(
                answered_count=Count("answers", distinct=True),
                average_score=Avg("evaluations__overall_score"),
            )
            .order_by("-created_at", "-id")
        )
        wanted = self.request.query_params.get("status")
        if wanted in InterviewSession.Status.values:
            queryset = queryset.filter(status=wanted)
        return queryset

    @extend_schema(request=SessionCreateSerializer, responses={201: SessionDetailSerializer})
    def post(self, request, *args, **kwargs):
        serializer = SessionCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        session, created = services.start_session(
            request.user,
            role_title=data["role_title"],
            job_profile=data.get("job_profile_id"),
            resume=data.get("resume_id"),
            mode=data["mode"],
            question_count=data["question_count"],
            options=SessionCreateSerializer.options_from(data),
            idempotency_key=data.get("idempotency_key"),
        )
        return _detail_response(
            request, session, status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )


@extend_schema(tags=["interviews"])
class InterviewDetailView(RetrieveAPIView):
    serializer_class = SessionDetailSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return detail_queryset(self.request.user)


def _owned_question(request, pk, position):
    session = get_object_or_404(owned_sessions(request.user).select_related("job_profile"), pk=pk)
    question = get_object_or_404(InterviewQuestion, session=session, position=position)
    return session, question


@extend_schema(tags=["interviews"])
class AnswerView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AnswerSubmitSerializer, responses={200: AnswerSerializer, 201: AnswerSerializer}
    )
    def post(self, request, pk, position, *args, **kwargs):
        session, question = _owned_question(request, pk, position)
        serializer = AnswerSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        answer, created = services.submit_answer(session, question, **serializer.validated_data)
        return Response(
            AnswerSerializer(answer).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["interviews"])
class EvaluateView(_AIThrottleOnPostMixin, APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AIOptionsSerializer,
        responses={200: EvaluationSerializer, 201: EvaluationSerializer},
    )
    def post(self, request, pk, position, *args, **kwargs):
        session, question = _owned_question(request, pk, position)
        options = AIOptionsSerializer(data=request.data or {})
        options.is_valid(raise_exception=True)
        evaluation, created = services.evaluate(
            session, question, AIOptionsSerializer.options_from(options.validated_data)
        )
        evaluation = AnswerEvaluation.objects.prefetch_related("competency_scores").get(
            pk=evaluation.pk
        )
        return Response(
            EvaluationSerializer(evaluation).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=["interviews"])
class CompleteView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses=SessionDetailSerializer)
    def post(self, request, pk, *args, **kwargs):
        session = get_object_or_404(owned_sessions(request.user), pk=pk)
        services.complete_session(session)
        return _detail_response(request, session)


class EvaluationHistorySerializer(EvaluationSerializer):
    session_id = serializers.IntegerField(source="session.id")
    role_title = serializers.CharField(source="session.role_title")
    question_position = serializers.IntegerField(source="answer.question.position")
    question_text = serializers.CharField(source="answer.question.text")
    input_mode = serializers.CharField(source="answer.input_mode")

    class Meta(EvaluationSerializer.Meta):
        fields = [
            *EvaluationSerializer.Meta.fields,
            "session_id",
            "role_title",
            "question_position",
            "question_text",
            "input_mode",
        ]
        read_only_fields = fields


@extend_schema(
    tags=["evaluations"],
    parameters=[OpenApiParameter("session", int, description="Only this interview's evaluations.")],
)
class EvaluationListView(ListAPIView):
    serializer_class = EvaluationHistorySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = (
            AnswerEvaluation.objects.filter(session__owner=self.request.user)
            .select_related("session", "answer__question")
            .prefetch_related("competency_scores")
        )
        session_id = self.request.query_params.get("session", "")
        if session_id.isdigit():
            queryset = queryset.filter(session_id=int(session_id))
        return queryset


@extend_schema(
    tags=["evaluations"],
    responses=inline_serializer(
        name="EvaluationSummary",
        fields={
            "evaluation_count": serializers.IntegerField(),
            "competencies": serializers.ListField(child=serializers.DictField()),
            "sessions": serializers.ListField(child=serializers.DictField()),
        },
    ),
)
class EvaluationSummaryView(APIView):
    """Averages per competency and per interview, for progress charts."""

    permission_classes = [IsAuthenticated]
    RECENT_SESSIONS = 20

    def get(self, request, *args, **kwargs):
        user = request.user
        averages = {
            row["competency"]: row
            for row in CompetencyScore.objects.filter(session__owner=user)
            .values("competency")
            .annotate(average=Avg("score"), count=Count("id"))
        }
        competencies = [
            {
                "competency": code,
                "label": spec["label"],
                "average": round(averages[code]["average"], 2) if code in averages else None,
                "count": averages[code]["count"] if code in averages else 0,
            }
            for code, spec in COMPETENCIES.items()
        ]

        sessions = list(
            owned_sessions(user)
            .annotate(
                average_score=Avg("evaluations__overall_score"),
                evaluated_count=Count("evaluations", distinct=True),
            )
            .filter(evaluated_count__gt=0)
            .order_by("-created_at")[: self.RECENT_SESSIONS]
            .values("id", "role_title", "mode", "created_at", "average_score", "evaluated_count")
        )
        for row in sessions:
            row["average_score"] = round(row["average_score"], 2)
        sessions.reverse()  # oldest first, for a left-to-right timeline

        return Response(
            {
                "evaluation_count": AnswerEvaluation.objects.filter(session__owner=user).count(),
                "competencies": competencies,
                "sessions": sessions,
            }
        )
