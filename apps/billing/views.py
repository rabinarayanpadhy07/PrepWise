from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Plan, Subscription
from apps.billing.serializers import (
    PlanSerializer,
    SubscriptionSerializer,
    SubscriptionStatusSerializer,
)
from apps.payments.gateway import is_configured as payments_configured
from apps.usage.services import current_subscription, subscription_summary


@extend_schema(tags=["billing"], summary="List active subscription plans")
class PlanListView(ListAPIView):
    """Public list of plans that can currently be purchased. The pricing page reads this."""

    serializer_class = PlanSerializer
    permission_classes = [AllowAny]
    authentication_classes = []

    def get_queryset(self):
        return (
            Plan.objects.filter(is_active=True)
            .prefetch_related("versions")
            .order_by("display_order", "id")
        )


@extend_schema(tags=["billing"])
class SubscriptionStatusView(APIView):
    """The user's current subscription with usage, queued renewals and past periods."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=SubscriptionStatusSerializer)
    def get(self, request, *args, **kwargs):
        user = request.user
        now = timezone.now()
        subscriptions = Subscription.objects.filter(user=user).select_related(
            "plan", "plan_version", "order"
        )
        current = current_subscription(user, now=now)
        upcoming = subscriptions.upcoming(now).order_by("starts_at")
        history = subscriptions.exclude(pk=current.pk if current else None).exclude(
            pk__in=upcoming.values("pk")
        )[:12]

        if current is not None:
            state = "active"
        elif subscriptions.filter(status=Subscription.Status.REVOKED).exists() and not (
            subscriptions.filter(status=Subscription.Status.ACTIVE).exists()
        ):
            state = "revoked"
        elif subscriptions.exists():
            state = "expired"
        else:
            state = "none"

        current_data = None
        if current is not None:
            current_data = {
                **SubscriptionSerializer(current).data,
                "usage": subscription_summary(current, now)["usage"],
            }
        return Response(
            {
                "status": state,
                "current": current_data,
                "upcoming": SubscriptionSerializer(upcoming, many=True).data,
                "history": SubscriptionSerializer(history, many=True).data,
                "payments_enabled": payments_configured(),
            }
        )
