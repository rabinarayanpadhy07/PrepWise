import re

from rest_framework import serializers

from apps.ai_services.models import AIMode, UserAIPreference
from apps.ai_services.service import AIRequestOptions

# A light format check to catch pasting the wrong thing (spaces, quotes, a URL,
# a key from another provider). The live test with OpenAI is the real
# validation. OpenAI keys start with "sk-" (e.g. "sk-proj-...", "sk-svcacct-...").
OPENAI_KEY_PATTERN = re.compile(r"^sk-[A-Za-z0-9_\-.]{20,400}$")
OPENAI_KEY_MESSAGE = "This doesn't look like an OpenAI API key. OpenAI keys start with sk-."


def validate_openai_key(value):
    value = (value or "").strip()
    if not OPENAI_KEY_PATTERN.match(value):
        raise serializers.ValidationError(OPENAI_KEY_MESSAGE)
    return value


class AIOptionsSerializer(serializers.Serializer):
    """Optional explicit AI choice accepted by every AI-backed request."""

    ai_mode = serializers.ChoiceField(choices=AIMode.Key.choices, required=False, allow_null=True)

    @staticmethod
    def options_from(data):
        return AIRequestOptions(ai_mode=data.get("ai_mode") or None)


class PreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserAIPreference
        fields = ["ai_mode"]

    def validate_ai_mode(self, value):
        if not AIMode.objects.filter(key=value, is_enabled=True).exists():
            raise serializers.ValidationError("That AI mode isn't available.")
        return value


class AccessStatusSerializer(serializers.Serializer):
    platform_status = serializers.ChoiceField(
        choices=[
            "available",
            "subscription_required",
            "subscription_expired",
            "not_configured",
            "budget_exhausted",
        ]
    )
    preference = PreferenceSerializer()
    default_mode = serializers.CharField()
    modes = serializers.ListField(child=serializers.DictField())
    subscription = serializers.DictField(allow_null=True)
    voice = serializers.DictField()
