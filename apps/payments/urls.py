from django.urls import path

from apps.payments import views

app_name = "payments"

urlpatterns = [
    path("orders/", views.OrderCreateView.as_view(), name="order-create"),
    path("orders/<uuid:pk>/", views.OrderDetailView.as_view(), name="order-detail"),
    path("orders/<uuid:pk>/verify/", views.OrderVerifyView.as_view(), name="order-verify"),
    path("history/", views.PaymentHistoryView.as_view(), name="history"),
    path("invoices/<int:pk>/pdf/", views.InvoicePDFView.as_view(), name="invoice-pdf"),
    path("webhooks/razorpay/", views.RazorpayWebhookView.as_view(), name="webhook-razorpay"),
]
