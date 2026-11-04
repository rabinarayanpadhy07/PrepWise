"""
Refresh-token cookie and CSRF helpers for the authentication endpoints.

The refresh token lives only in an HttpOnly cookie scoped to /api/v1/auth/.
Every endpoint that reads or sets that cookie enforces Django's CSRF check
(X-CSRFToken header matching the csrftoken cookie, plus Origin checking).
"""

from django.conf import settings
from django.contrib.auth.models import update_last_login
from django.middleware.csrf import CsrfViewMiddleware
from rest_framework import exceptions, status
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken


class CSRFFailed(exceptions.PermissionDenied):
    default_detail = "Security check failed. Refresh the page and try again."
    default_code = "csrf_failed"


class SessionExpired(exceptions.AuthenticationFailed):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Your session has ended. Please log in again."
    default_code = "session_expired"


class _CSRFCheck(CsrfViewMiddleware):
    def _reject(self, request, reason):
        return reason


def enforce_csrf(request):
    """Run Django's CSRF validation for a DRF request (DRF views are csrf_exempt)."""
    django_request = getattr(request, "_request", request)
    check = _CSRFCheck(lambda _request: None)
    check.process_request(django_request)
    reason = check.process_view(django_request, None, (), {})
    if reason:
        raise CSRFFailed(detail=f"Security check failed: {reason}")


def issue_tokens(user):
    """Create a refresh/access pair for `user` and record the login time."""
    refresh = RefreshToken.for_user(user)
    if settings.SIMPLE_JWT.get("UPDATE_LAST_LOGIN"):
        update_last_login(None, user)
    return str(refresh), str(refresh.access_token)


def set_refresh_cookie(response, refresh_token):
    cookie = settings.AUTH_COOKIE
    response.set_cookie(
        cookie["REFRESH_NAME"],
        refresh_token,
        max_age=int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
        path=cookie["PATH"],
        domain=cookie["DOMAIN"],
        secure=cookie["SECURE"],
        httponly=True,
        samesite=cookie["SAMESITE"],
    )


def clear_refresh_cookie(response):
    cookie = settings.AUTH_COOKIE
    response.delete_cookie(
        cookie["REFRESH_NAME"],
        path=cookie["PATH"],
        domain=cookie["DOMAIN"],
        samesite=cookie["SAMESITE"],
    )


def read_refresh_cookie(request):
    return request.COOKIES.get(settings.AUTH_COOKIE["REFRESH_NAME"], "")


def revoke_all_refresh_tokens(user):
    """Blacklist every outstanding refresh token for `user` (log out everywhere)."""
    outstanding = OutstandingToken.objects.filter(user=user, blacklistedtoken__isnull=True)
    BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=token) for token in outstanding], ignore_conflicts=True
    )
