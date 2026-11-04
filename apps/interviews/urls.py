from django.urls import path

from apps.interviews import views

app_name = "interviews"

urlpatterns = [
    path("interviews/", views.InterviewListCreateView.as_view(), name="session-list"),
    path("interviews/<int:pk>/", views.InterviewDetailView.as_view(), name="session-detail"),
    path(
        "interviews/<int:pk>/questions/<int:position>/answer/",
        views.AnswerView.as_view(),
        name="answer",
    ),
    path(
        "interviews/<int:pk>/questions/<int:position>/evaluate/",
        views.EvaluateView.as_view(),
        name="evaluate",
    ),
    path("interviews/<int:pk>/complete/", views.CompleteView.as_view(), name="complete"),
    path("evaluations/", views.EvaluationListView.as_view(), name="evaluation-list"),
    path("evaluations/summary/", views.EvaluationSummaryView.as_view(), name="evaluation-summary"),
]
