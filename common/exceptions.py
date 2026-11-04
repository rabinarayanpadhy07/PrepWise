"""
Consistent API error responses.

Every error returned by the API has the shape:

    {
      "error": {
        "code": "validation_error",
        "message": "Some fields are invalid.",
        "details": {"email": ["Enter a valid email address."]},
        "request_id": "3f2c..."
      }
    }

`details` is present only for validation errors (field -> list of messages;
errors not tied to a field are under "non_field_errors"). Some errors add
fields such as `limit` (usage limits) or `retry_after_seconds` (throttling).
"""

import logging

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger("prepwise.api")

GENERIC_MESSAGES = {
    status.HTTP_400_BAD_REQUEST: "The request could not be processed.",
    status.HTTP_401_UNAUTHORIZED: "Authentication credentials were not provided or are invalid.",
    status.HTTP_403_FORBIDDEN: "You do not have permission to perform this action.",
    status.HTTP_404_NOT_FOUND: "The requested resource was not found.",
    status.HTTP_405_METHOD_NOT_ALLOWED: "This method is not allowed for this resource.",
    status.HTTP_429_TOO_MANY_REQUESTS: "Too many requests. Please try again later.",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "An unexpected error occurred. Please try again later.",
}

# DRF default codes mapped to the stable codes clients rely on.
CODE_OVERRIDES = {
    "invalid": "validation_error",
    "not_authenticated": "not_authenticated",
    "authentication_failed": "authentication_failed",
    "permission_denied": "permission_denied",
    "not_found": "not_found",
    "method_not_allowed": "method_not_allowed",
    "not_acceptable": "not_acceptable",
    "unsupported_media_type": "unsupported_media_type",
    "throttled": "throttled",
    "parse_error": "parse_error",
}


def error_payload(code, message, request=None, details=None):
    body = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    body["request_id"] = getattr(request, "request_id", None)
    return {"error": body}


def _flatten_messages(detail):
    """Turn DRF ErrorDetail structures into plain strings and lists."""
    if isinstance(detail, dict):
        return {key: _flatten_messages(value) for key, value in detail.items()}
    if isinstance(detail, list):
        return [_flatten_messages(item) for item in detail]
    return str(detail)


def _normalise_validation_details(detail):
    if isinstance(detail, dict):
        return _flatten_messages(detail)
    # A bare list/string is a non-field error.
    messages = detail if isinstance(detail, list) else [detail]
    return {api_settings.NON_FIELD_ERRORS_KEY: _flatten_messages(messages)}


def api_exception_handler(exc, context):
    request = context.get("request")

    if isinstance(exc, DjangoValidationError):
        exc = exceptions.ValidationError(
            detail=exc.message_dict if hasattr(exc, "error_dict") else exc.messages
        )
    elif isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, PermissionDenied):
        exc = exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)

    if response is None:
        # Unhandled exception: log it (redacted) and show the diagnostic detail.
        logger.exception("Unhandled API exception", exc_info=exc)
        return Response(
            error_payload(
                "server_error",
                f"An unexpected error occurred: {type(exc).__name__}: {str(exc)}",
                request,
            ),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, exceptions.ValidationError):
        payload = error_payload(
            "validation_error",
            "Some fields are invalid.",
            request,
            details=_normalise_validation_details(exc.detail),
        )
    else:
        detail = getattr(exc, "detail", None)
        # A code passed when raising (e.g. AuthenticationFailed(msg, code="x")) wins
        # over the exception class's default.
        default_code = getattr(detail, "code", None) or getattr(exc, "default_code", "error")
        code = CODE_OVERRIDES.get(default_code, default_code)
        message = (
            str(detail)
            if isinstance(detail, str)
            else GENERIC_MESSAGES.get(response.status_code, "The request could not be completed.")
        )
        payload = error_payload(code, message, request)
        if isinstance(exc, exceptions.Throttled) and exc.wait is not None:
            payload["error"]["retry_after_seconds"] = int(exc.wait)
        # Exceptions may attach extra machine-readable fields (e.g. usage limit details).
        for key, value in (getattr(exc, "error_extra", None) or {}).items():
            payload["error"].setdefault(key, value)

    response.data = payload
    return response
