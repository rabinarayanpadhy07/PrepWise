from django.urls import path

from apps.resumes import views

app_name = "resumes"

urlpatterns = [
    path("resumes/", views.ResumeListCreateView.as_view(), name="resume-list"),
    path("resumes/<int:pk>/", views.ResumeDetailView.as_view(), name="resume-detail"),
    path("job-profiles/", views.JobProfileListCreateView.as_view(), name="job-profile-list"),
    path("job-profiles/<int:pk>/", views.JobProfileDetailView.as_view(), name="job-profile-detail"),
    path("resume-analyses/", views.ResumeAnalysisListCreateView.as_view(), name="analysis-list"),
    path(
        "resume-analyses/<int:pk>/",
        views.ResumeAnalysisDetailView.as_view(),
        name="analysis-detail",
    ),
    path(
        "resume-analyses/<int:pk>/retry/",
        views.ResumeAnalysisRetryView.as_view(),
        name="analysis-retry",
    ),
    # Resume maker
    path("built-resumes/", views.BuiltResumeListCreateView.as_view(), name="built-resume-list"),
    path(
        "built-resumes/import/",
        views.BuiltResumeImportView.as_view(),
        name="built-resume-import",
    ),
    path(
        "built-resumes/<int:pk>/",
        views.BuiltResumeDetailView.as_view(),
        name="built-resume-detail",
    ),
    path(
        "built-resumes/<int:pk>/tailor/",
        views.BuiltResumeTailorView.as_view(),
        name="built-resume-tailor",
    ),
]
