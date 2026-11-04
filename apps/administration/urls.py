from django.urls import path

from apps.administration import views

app_name = "administration"

urlpatterns = [
    path("overview/", views.OverviewView.as_view(), name="overview"),
    # Users
    path("users/", views.AdminUserListView.as_view(), name="user-list"),
    path("users/<int:pk>/", views.AdminUserDetailView.as_view(), name="user-detail"),
    path("users/<int:pk>/summary/", views.AdminUserSummaryView.as_view(), name="user-summary"),
    path(
        "users/<int:pk>/revoke-sessions/",
        views.AdminRevokeSessionsView.as_view(),
        name="user-revoke-sessions",
    ),
    # Subscriptions and plans
    path("subscriptions/", views.AdminSubscriptionListView.as_view(), name="subscription-list"),
    path(
        "subscriptions/<int:pk>/",
        views.AdminSubscriptionDetailView.as_view(),
        name="subscription-detail",
    ),
    path("plans/", views.AdminPlanListView.as_view(), name="plan-list"),
    path("plans/<slug:code>/", views.AdminPlanDetailView.as_view(), name="plan-detail"),
    # AI configuration
    path("ai/keys/", views.AdminPlatformKeyListView.as_view(), name="ai-key-list"),
    path("ai/keys/<int:pk>/", views.AdminPlatformKeyDetailView.as_view(), name="ai-key-detail"),
    path("ai/keys/<int:pk>/test/", views.AdminPlatformKeyTestView.as_view(), name="ai-key-test"),
    path("ai/models/", views.AdminAIModelListView.as_view(), name="ai-model-list"),
    path("ai/models/<int:pk>/", views.AdminAIModelDetailView.as_view(), name="ai-model-detail"),
    path("ai/modes/", views.AdminAIModeListView.as_view(), name="ai-mode-list"),
    path("ai/modes/<str:key>/", views.AdminAIModeDetailView.as_view(), name="ai-mode-detail"),
    path("ai/settings/", views.AdminAISettingsView.as_view(), name="ai-settings"),
    # Usage
    path("usage/summary/", views.AdminUsageSummaryView.as_view(), name="usage-summary"),
    path("usage/users/", views.AdminUserUsageView.as_view(), name="usage-users"),
    # Payments
    path("payments/orders/", views.AdminOrderListView.as_view(), name="order-list"),
    path("payments/orders/<uuid:pk>/", views.AdminOrderDetailView.as_view(), name="order-detail"),
    path(
        "payments/payments/<str:payment_id>/refund/",
        views.AdminRefundView.as_view(),
        name="payment-refund",
    ),
    path(
        "payments/invoices/<int:pk>/pdf/", views.AdminInvoicePDFView.as_view(), name="invoice-pdf"
    ),
    path("payments/webhooks/", views.AdminWebhookEventListView.as_view(), name="webhook-list"),
    # Monitoring and audit
    path("health/providers/", views.AdminProviderHealthView.as_view(), name="provider-health"),
    path("health/calls/", views.AdminProviderCallListView.as_view(), name="provider-calls"),
    path("audit-log/", views.AdminAuditLogView.as_view(), name="audit-log"),
]
