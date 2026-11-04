"""
Machine-readable usage-limit errors. Each has a stable `code`; limit errors
also carry a `limit` object:

    {"error": {"code": "daily_token_limit_reached", "message": "...",
               "limit": {"kind": "ai_tokens", "scope": "daily", "limit": 50000,
                         "used": 49800, "requested": 9000, "resets_at": "..."}}}
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class UsageError(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "usage_limit_reached"
    default_detail = "You've reached a usage limit."

    def __init__(self, detail=None, *, limit=None):
        super().__init__(detail or self.default_detail, self.default_code)
        self.error_extra = {"limit": limit} if limit else {}


class SubscriptionRequired(UsageError):
    default_code = "subscription_required"
    default_detail = "AI features need an active PrepWise plan. Buy a plan to continue."


class SubscriptionExpired(SubscriptionRequired):
    default_code = "subscription_expired"
    default_detail = "Your plan has ended. Renew it to keep using the AI features."


class TokenAllowanceExhausted(UsageError):
    default_code = "token_allowance_exhausted"
    default_detail = "You've used the platform AI included in your current subscription period."


class DailyTokenLimitReached(UsageError):
    default_code = "daily_token_limit_reached"
    default_detail = "You've reached today's platform AI limit. It resets at midnight."


class InterviewAllowanceExhausted(UsageError):
    default_code = "interview_allowance_exhausted"
    default_detail = "You've used the mock interviews included in your current subscription period."


class DailyInterviewLimitReached(UsageError):
    default_code = "daily_interview_limit_reached"
    default_detail = "You've reached today's interview limit. It resets at midnight."


class VoiceAllowanceExhausted(UsageError):
    default_code = "voice_allowance_exhausted"
    default_detail = "You've used the voice answer time included in your subscription period."


class DailyVoiceLimitReached(UsageError):
    default_code = "daily_voice_limit_reached"
    default_detail = "You've reached today's voice answer limit. It resets at midnight."


class PlatformBudgetExhausted(UsageError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "platform_budget_exhausted"
    default_detail = (
        "Platform AI has reached its spending limit for this month. You can use your own "
        "API key in the meantime."
    )


class RequestTooLarge(UsageError):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "ai_input_too_large"
    default_detail = (
        "This request is too large for the selected AI model. Shorten the resume or job "
        "description, or choose another AI mode."
    )
