from django.db.models import Prefetch, Q
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView, get_object_or_404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.billing.models import Plan
from apps.payments import services
from apps.payments.invoices import render_invoice_pdf
from apps.payments.models import Invoice, Payment, PaymentOrder
from apps.payments.serializers import (
    CheckoutVerifySerializer,
    OrderCreateSerializer,
    OrderHistorySerializer,
    OrderSerializer,
)


def _owned_orders(user):
    return PaymentOrder.objects.filter(user=user).select_related(
        "plan_version__plan", "subscription", "invoice", "user"
    )


class _PaymentsThrottle:
    throttle_scope = "payments"

    def get_throttles(self):
        return [ScopedRateThrottle()]


@extend_schema(tags=["payments"])
class OrderCreateView(_PaymentsThrottle, APIView):
    """Create a gateway order for a plan. The amount always comes from the stored plan."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=OrderCreateSerializer, responses={201: OrderSerializer})
    def post(self, request, *args, **kwargs):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = get_object_or_404(Plan, code=serializer.validated_data["plan_code"], is_active=True)
        order = services.create_order(request.user, plan)
        order = _owned_orders(request.user).get(pk=order.pk)
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["payments"])
class OrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OrderSerializer)
    def get(self, request, pk, *args, **kwargs):
        order = get_object_or_404(_owned_orders(request.user), pk=pk)
        return Response(OrderSerializer(order).data)


@extend_schema(tags=["payments"])
class OrderVerifyView(_PaymentsThrottle, APIView):
    """
    Called with the Checkout handler's response. The payment is confirmed with
    the gateway before anything is activated; `status` is "paid" only then.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(request=CheckoutVerifySerializer, responses=OrderSerializer)
    def post(self, request, pk, *args, **kwargs):
        order = get_object_or_404(_owned_orders(request.user), pk=pk)
        serializer = CheckoutVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.verify_checkout(
            order,
            gateway_order_id=data["razorpay_order_id"],
            gateway_payment_id=data["razorpay_payment_id"],
            signature=data["razorpay_signature"],
        )
        order = _owned_orders(request.user).get(pk=order.pk)
        return Response(OrderSerializer(order).data)


@extend_schema(tags=["payments"])
class RazorpayWebhookView(APIView):
    """Gateway webhooks. Authenticated by signature, not by user credentials."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    def post(self, request, *args, **kwargs):
        # The signature covers the exact raw body, so it is read before any parsing.
        event = services.handle_webhook(
            request.body,
            signature=request.headers.get("X-Razorpay-Signature"),
            event_id=request.headers.get("X-Razorpay-Event-Id"),
        )
        return Response({"status": event.status})


@extend_schema(tags=["payments"])
class PaymentHistoryView(ListAPIView):
    """The user's orders that were paid or had a payment attempt, newest first."""

    serializer_class = OrderHistorySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            _owned_orders(self.request.user)
            .filter(
                Q(status__in=[PaymentOrder.Status.PAID, PaymentOrder.Status.REVIEW])
                | Q(payments__isnull=False)
            )
            .distinct()
            .prefetch_related(
                Prefetch("payments", queryset=Payment.objects.prefetch_related("refunds"))
            )
            .order_by("-created_at")
        )


@extend_schema(tags=["payments"])
class InvoicePDFView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            (200, "application/pdf"): OpenApiResponse(
                OpenApiTypes.BINARY, description="Invoice PDF"
            )
        }
    )
    def get(self, request, pk, *args, **kwargs):
        # Other users' invoices look nonexistent.
        invoice = get_object_or_404(Invoice, pk=pk, user=request.user)
        response = HttpResponse(render_invoice_pdf(invoice), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{invoice.number}.pdf"'
        response["Cache-Control"] = "private, no-store"
        return response
