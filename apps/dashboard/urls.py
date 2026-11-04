from django.urls import path

from apps.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("dashboard/summary/", views.DashboardSummaryView.as_view(), name="summary"),
    path("dashboard/activity/", views.ActivityView.as_view(), name="activity"),
    path(
        "reports/analyses/<int:pk>/pdf/", views.AnalysisReportView.as_view(), name="analysis-report"
    ),
    path(
        "reports/interviews/<int:pk>/pdf/",
        views.InterviewReportView.as_view(),
        name="interview-report",
    ),
]
