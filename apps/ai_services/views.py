from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_services import service
from apps.ai_services.models import AIMode, AIPlatformSettings, UserAIPreference
from apps.ai_services.serializers import AccessStatusSerializer, PreferenceSerializer
from apps.usage.services import (
    current_subscription,
    subscription_summary,
    voice_recording_limit,
)


@extend_schema(tags=["ai"])
class PreferenceView(APIView):
    """The user's preferred AI mode."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=PreferenceSerializer)
    def get(self, request, *args, **kwargs):
        return Response(PreferenceSerializer(service.preference_for(request.user)).data)

    @extend_schema(request=PreferenceSerializer, responses=PreferenceSerializer)
    def patch(self, request, *args, **kwargs):
        preference, _ = UserAIPreference.objects.get_or_create(user=request.user)
        serializer = PreferenceSerializer(
            preference, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


@extend_schema(tags=["ai"])
class AccessStatusView(APIView):
    """Whether the user can use AI right now, the AI modes, and their plan's usage."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=AccessStatusSerializer)
    def get(self, request, *args, **kwargs):
        user = request.user
        platform = AIPlatformSettings.load()
        return Response(
            {
                "platform_status": service.platform_status(user),
                "preference": PreferenceSerializer(service.preference_for(user)).data,
                "default_mode": AIMode.DEFAULT,
                "modes": service.mode_options(),
                "subscription": subscription_summary(current_subscription(user)),
                "voice": {
                    "max_answer_seconds": platform.voice_max_answer_seconds,
                    "remaining_seconds": voice_recording_limit(user),
                },
            }
        )
