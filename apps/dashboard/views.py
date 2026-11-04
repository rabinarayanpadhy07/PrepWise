"""
Dashboard data for the signed-in user. Every query is scoped to the requester,
so changing an ID in the URL can only ever reach the user's own records.
"""

from django.db.models import Avg, Prefetch
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_services.service import platform_status
from apps.billing.models import Subscription
from apps.dashboard.reports import render_analysis_report, render_interview_report
from apps.interviews.models import (
    AnswerEvaluation,
    InterviewAnswer,
    InterviewQuestion,
    InterviewSession,
)
from apps.payments.models import PaymentOrder, Refund
from apps.resumes.models import Resume, ResumeAnalysis
from apps.usage.services import current_subscription, subscription_summary
from common.pagination import StandardPagination

# The activity feed covers this many recent items of each kind.
ACTIVITY_LIMIT_PER_KIND = 50


class ReportUnavailable(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "report_unavailable"
    default_detail = "A report is available once the analysis has completed."


def _subscription_overview(user, now):
    current = current_subscription(user, now=now)
    upcoming = Subscription.objects.upcoming(now).filter(user=user).order_by("starts_at").first()
    last = Subscription.objects.filter(user=user).order_by("-ends_at").first()
    if current is not None:
        state = "active"
    elif last is None:
        state = "none"
    elif last.status == Subscription.Status.REVOKED:
        state = "revoked"
    else:
        state = "expired"
    shown = current or last
    return {
        "status": state,
        "plan_name": (shown.plan_version.name if shown.plan_version else shown.plan.name)
        if shown
        else None,
        "starts_at": shown.starts_at if shown else None,
        "ends_at": shown.ends_at if shown else None,
        "renewal_starts_at": upcoming.starts_at if upcoming else None,
        "renewal_ends_at": upcoming.ends_at if upcoming else None,
    }


@extend_schema(tags=["dashboard"])
class DashboardSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses=inline_serializer(
            "DashboardSummary",
            {
                "counts": serializers.DictField(),
                "subscription": serializers.DictField(),
                "platform_status": serializers.CharField(),
                "platform_usage": serializers.DictField(allow_null=True),
            },
        )
    )
    def get(self, request, *args, **kwargs):
        user = request.user
        now = timezone.now()
        analyses = ResumeAnalysis.objects.filter(owner=user)
        sessions = InterviewSession.objects.filter(owner=user)
        evaluations = AnswerEvaluation.objects.filter(session__owner=user)
        average = evaluations.aggregate(value=Avg("overall_score"))["value"]
        current = current_subscription(user, now=now)
        usage = subscription_summary(current, now)
        return Response(
            {
                "counts": {
                    "resumes": Resume.objects.filter(owner=user).count(),
                    "analyses": analyses.count(),
                    "analyses_completed": analyses.filter(
                        status=ResumeAnalysis.Status.COMPLETED
                    ).count(),
                    "interviews": sessions.count(),
                    "interviews_completed": sessions.filter(
                        status=InterviewSession.Status.COMPLETED
                    ).count(),
                    "answers": InterviewAnswer.objects.filter(session__owner=user).count(),
                    "evaluations": evaluations.count(),
                    "average_score": round(average, 2) if average is not None else None,
                },
                "subscription": _subscription_overview(user, now),
                "platform_status": platform_status(user, now),
                "platform_usage": usage["usage"] if usage else None,
            }
        )


def _activity(user):
    limit = ACTIVITY_LIMIT_PER_KIND
    items = []
    for analysis in ResumeAnalysis.objects.filter(owner=user).select_related("job_profile")[:limit]:
        kind = {
            ResumeAnalysis.Status.COMPLETED: "analysis_completed",
            ResumeAnalysis.Status.FAILED: "analysis_failed",
        }.get(analysis.status, "analysis_started")
        items.append(
            {
                "type": kind,
                "at": analysis.completed_at or analysis.created_at,
                "title": analysis.job_profile.title,
                "detail": None,
                "target": {"kind": "analysis", "id": analysis.pk},
            }
        )
    for session in InterviewSession.objects.filter(owner=user)[:limit]:
        items.append(
            {
                "type": "interview_started",
                "at": session.created_at,
                "title": session.role_title,
                "detail": "Voice" if session.mode == InterviewSession.Mode.VOICE else "Text",
                "target": {"kind": "interview", "id": session.pk},
            }
        )
        if session.completed_at:
            items.append(
                {
                    "type": "interview_completed",
                    "at": session.completed_at,
                    "title": session.role_title,
                    "detail": None,
                    "target": {"kind": "interview", "id": session.pk},
                }
            )
    evaluations = AnswerEvaluation.objects.filter(session__owner=user).select_related(
        "session", "answer__question"
    )[:limit]
    for evaluation in evaluations:
        items.append(
            {
                "type": "answer_evaluated",
                "at": evaluation.created_at,
                "title": evaluation.session.role_title,
                "detail": f"Question {evaluation.answer.question.position}: "
                f"{evaluation.overall_score} / 5",
                "target": {"kind": "interview", "id": evaluation.session_id},
            }
        )
    paid = PaymentOrder.objects.filter(user=user, status=PaymentOrder.Status.PAID).select_related(
        "plan_version"
    )[:limit]
    for order in paid:
        items.append(
            {
                "type": "payment_received",
                "at": order.paid_at,
                "title": order.plan_version.name,
                "detail": f"{order.currency} {order.amount_minor / 100:,.2f}",
                "target": {"kind": "billing", "id": None},
            }
        )
    refunds = Refund.objects.filter(
        payment__order__user=user, status=Refund.Status.PROCESSED
    ).order_by("-processed_at")[:limit]
    for refund in refunds:
        items.append(
            {
                "type": "refund_processed",
                "at": refund.processed_at,
                "title": "Refund",
                "detail": f"{refund.currency} {refund.amount_minor / 100:,.2f}",
                "target": {"kind": "billing", "id": None},
            }
        )
    items.sort(key=lambda item: item["at"], reverse=True)
    return items


@extend_schema(tags=["dashboard"])
class ActivityView(APIView):
    """Recent activity across analyses, interviews, feedback and payments (paginated)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses=inline_serializer(
            "ActivityPage",
            {
                "count": serializers.IntegerField(),
                "next": serializers.URLField(allow_null=True),
                "previous": serializers.URLField(allow_null=True),
                "results": serializers.ListField(child=serializers.DictField()),
            },
        )
    )
    def get(self, request, *args, **kwargs):
        paginator = StandardPagination()
        page = paginator.paginate_queryset(_activity(request.user), request, view=self)
        return paginator.get_paginated_response(page)


def _pdf_response(content, filename):
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    return response


_PDF = {(200, "application/pdf"): OpenApiResponse(OpenApiTypes.BINARY, description="PDF report")}


@extend_schema(tags=["reports"])
class AnalysisReportView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=_PDF)
    def get(self, request, pk, *args, **kwargs):
        analysis = get_object_or_404(
            ResumeAnalysis.objects.select_related("resume", "job_profile"),
            pk=pk,
            owner=request.user,
        )
        if analysis.status != ResumeAnalysis.Status.COMPLETED:
            raise ReportUnavailable()
        return _pdf_response(
            render_analysis_report(analysis), f"prepwise-analysis-{analysis.pk}.pdf"
        )


@extend_schema(tags=["reports"])
class InterviewReportView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=_PDF)
    def get(self, request, pk, *args, **kwargs):
        session = get_object_or_404(
            InterviewSession.objects.prefetch_related(
                Prefetch(
                    "questions",
                    queryset=InterviewQuestion.objects.select_related(
                        "answer__evaluation"
                    ).prefetch_related("answer__evaluation__competency_scores"),
                )
            ),
            pk=pk,
            owner=request.user,
        )
        return _pdf_response(
            render_interview_report(session), f"prepwise-interview-{session.pk}.pdf"
        )
