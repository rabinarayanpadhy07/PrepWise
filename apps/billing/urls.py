from django.urls import path

from apps.billing.views import PlanListView, SubscriptionStatusView

app_name = "billing"

urlpatterns = [
    path("plans/", PlanListView.as_view(), name="plan-list"),
    path("subscription/", SubscriptionStatusView.as_view(), name="subscription"),
]
