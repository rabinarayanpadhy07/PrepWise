from rest_framework import serializers

from apps.billing.models import Plan, Subscription


class PlanSerializer(serializers.ModelSerializer):
    version = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            "code",
            "name",
            "description",
            "currency",
            "price_minor",
            "duration_days",
            "renewal_mode",
            "ai_token_quota",
            "daily_ai_token_limit",
            "interview_quota",
            "daily_interview_limit",
            "voice_seconds_quota",
            "daily_voice_seconds_limit",
            "version",
        ]
        read_only_fields = fields

    def get_version(self, obj) -> int | None:
        version = obj.current_version
        return version.version if version else None


class SubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.SerializerMethodField()
    plan_code = serializers.CharField(source="plan.code")
    price_minor = serializers.IntegerField(source="plan_version.price_minor", allow_null=True)
    currency = serializers.CharField(source="plan_version.currency", allow_null=True)
    order_id = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            "id",
            "plan_code",
            "plan_name",
            "price_minor",
            "currency",
            "status",
            "source",
            "starts_at",
            "ends_at",
            "revoked_at",
            "revoke_reason",
            "ai_token_quota",
            "daily_ai_token_limit",
            "interview_quota",
            "daily_interview_limit",
            "voice_seconds_quota",
            "daily_voice_seconds_limit",
            "order_id",
        ]
        read_only_fields = fields

    def get_plan_name(self, obj) -> str:
        return obj.plan_version.name if obj.plan_version else obj.plan.name

    def get_order_id(self, obj) -> str | None:
        order = getattr(obj, "order", None)
        return str(order.pk) if order else None


class SubscriptionStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["active", "expired", "revoked", "none"])
    current = serializers.DictField(allow_null=True)
    upcoming = SubscriptionSerializer(many=True)
    history = SubscriptionSerializer(many=True)
    payments_enabled = serializers.BooleanField()
