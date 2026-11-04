from django.urls import path

from apps.ai_services import views

app_name = "ai_services"

urlpatterns = [
    path("access/", views.AccessStatusView.as_view(), name="access"),
    path("preference/", views.PreferenceView.as_view(), name="preference"),
]
