"""URL routes for API version 1 (mounted at /api/v1/)."""

from django.conf import settings
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from common.views import HealthView, ReadinessView

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("health/ready/", ReadinessView.as_view(), name="readiness"),
    path("auth/", include("apps.accounts.urls")),
    path("admin/", include("apps.administration.urls")),
    path("billing/", include("apps.billing.urls")),
    path("ai/", include("apps.ai_services.urls")),
    path("payments/", include("apps.payments.urls")),
    path("", include("apps.dashboard.urls")),
    path("", include("apps.resumes.urls")),
    path("", include("apps.interviews.urls")),
]

if settings.API_DOCS_ENABLED:
    urlpatterns += [
        path("schema/", SpectacularAPIView.as_view(), name="schema"),
        path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
        path("redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    ]
