from django.conf import settings
from rest_framework import serializers

from apps.payments.models import Invoice, Payment, PaymentOrder, Refund


class OrderCreateSerializer(serializers.Serializer):
    # Only the plan is accepted; the price always comes from the stored plan version.
    plan_code = serializers.SlugField(max_length=50)


class CheckoutVerifySerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField(max_length=64)
    razorpay_payment_id = serializers.CharField(max_length=64)
    razorpay_signature = serializers.CharField(max_length=256)


class _PlanSnapshotSerializer(serializers.Serializer):
    code = serializers.CharField(source="plan.code")
    name = serializers.CharField()
    version = serializers.IntegerField()
    duration_days = serializers.IntegerField()


class _SubscriptionBriefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField()
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    revoke_reason = serializers.CharField()


class _InvoiceBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = ["id", "number", "issued_at"]


class OrderSerializer(serializers.ModelSerializer):
    plan = _PlanSnapshotSerializer(source="plan_version", read_only=True)
    subscription = _SubscriptionBriefSerializer(read_only=True, allow_null=True)
    invoice = serializers.SerializerMethodField()
    checkout = serializers.SerializerMethodField()

    class Meta:
        model = PaymentOrder
        fields = [
            "id",
            "status",
            "amount_minor",
            "currency",
            "gateway",
            "plan",
            "subscription",
            "invoice",
            "checkout",
            "created_at",
            "paid_at",
        ]
        read_only_fields = fields

    def get_invoice(self, obj) -> dict | None:
        invoice = getattr(obj, "invoice", None)
        return _InvoiceBriefSerializer(invoice).data if invoice else None

    def get_checkout(self, obj) -> dict | None:
        """Everything the gateway's checkout widget needs. Public values only."""
        if obj.status != PaymentOrder.Status.CREATED or not obj.gateway_order_id:
            return None
        user = obj.user
        return {
            "key_id": settings.PAYMENT_SETTINGS["KEY_ID"],
            "gateway_order_id": obj.gateway_order_id,
            "amount": obj.amount_minor,
            "currency": obj.currency,
            "name": settings.INVOICE_SETTINGS["SELLER_NAME"],
            "description": f"{obj.plan_version.name} ({obj.plan_version.duration_days} days)",
            "prefill": {"name": user.full_name, "email": user.email},
        }


class RefundSerializer(serializers.ModelSerializer):
    class Meta:
        model = Refund
        fields = ["amount_minor", "currency", "status", "created_at", "processed_at"]


class PaymentSerializer(serializers.ModelSerializer):
    refunds = RefundSerializer(many=True, read_only=True)

    class Meta:
        model = Payment
        fields = [
            "gateway_payment_id",
            "status",
            "amount_minor",
            "currency",
            "method",
            "amount_refunded_minor",
            "dispute_status",
            "captured_at",
            "created_at",
            "refunds",
        ]


class OrderHistorySerializer(OrderSerializer):
    payments = PaymentSerializer(many=True, read_only=True)

    class Meta(OrderSerializer.Meta):
        fields = [*OrderSerializer.Meta.fields, "payments"]
        read_only_fields = fields
