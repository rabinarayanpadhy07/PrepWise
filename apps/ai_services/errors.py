"""
Errors raised by the AI service layer. They are DRF APIExceptions, so the
standard error envelope ({"error": {"code", "message", ...}}) is returned.
Messages never include provider responses, prompts, or user content.
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class AIServiceError(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "The AI service is unavailable right now. Please try again shortly."
    default_code = "ai_unavailable"


class AINotConfigured(AIServiceError):
    default_detail = "AI features are temporarily unavailable. Please try again later."
    default_code = "ai_not_configured"


class KeyEncryptionUnavailable(AIServiceError):
    """AI_KEY_ENCRYPTION_KEYS is missing or invalid, so API keys can't be stored or read."""

    default_detail = "AI features are temporarily unavailable. Please try again later."
    default_code = "key_encryption_unavailable"


class StoredKeyUnreadable(AIServiceError):
    """A saved platform API key can't be decrypted with the configured encryption keys."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "A saved API key can no longer be read. Remove it and add it again."
    default_code = "stored_key_unreadable"


class AIProviderUnavailable(AIServiceError):
    default_detail = "The AI service didn't respond. Please try again in a moment."
    default_code = "ai_provider_unavailable"


class AIRateLimited(AIServiceError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "The AI service is busy right now. Please wait a minute and try again."
    default_code = "ai_rate_limited"


class AICredentialsRejected(AIServiceError):
    """The provider rejected an API key. Only administrators ever see this directly."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "The AI provider rejected this API key. Check the key and try again."
    default_code = "ai_key_rejected"


class AIRequestRejected(AIServiceError):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "The AI provider rejected the request."
    default_code = "ai_request_rejected"


class AIDeclined(AIServiceError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = "The AI model declined to respond to this request."
    default_code = "ai_declined"


class AIInvalidResponse(AIServiceError):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The AI returned a response we couldn't use. Please try again."
    default_code = "ai_invalid_response"


class AIModelUnavailable(AIServiceError):
    """The provider says the configured model doesn't exist or isn't available."""

    default_detail = "The selected AI model isn't available right now. Please try again later."
    default_code = "ai_model_unavailable"


class AIModeUnavailable(AIServiceError):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "That AI mode isn't available. Choose another mode."
    default_code = "ai_mode_unavailable"
