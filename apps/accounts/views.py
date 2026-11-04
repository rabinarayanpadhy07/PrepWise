from django.conf import settings
from django.contrib.auth import get_user_model
from django.middleware.csrf import get_token
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import exceptions, serializers, status
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts import google
from apps.accounts.authentication import issued_before_revocation
from apps.accounts.security import (
    SessionExpired,
    clear_refresh_cookie,
    enforce_csrf,
    issue_tokens,
    read_refresh_cookie,
    revoke_all_refresh_tokens,
    set_refresh_cookie,
)
from apps.accounts.serializers import (
    GoogleSignInSerializer,
    LoginSerializer,
    PasswordChangeSerializer,
    RegisterSerializer,
    UserSerializer,
)
from common.exceptions import error_payload

User = get_user_model()

_session_response = inline_serializer(
    name="AuthSessionResponse",
    fields={"access": serializers.CharField(), "user": UserSerializer()},
)
_detail_response = inline_serializer(
    name="DetailResponse", fields={"detail": serializers.CharField()}
)


def _session_payload(user, access):
    return {"access": access, "user": UserSerializer(user).data}


def _start_session(user, response_status=status.HTTP_200_OK):
    refresh, access = issue_tokens(user)
    response = Response(_session_payload(user, access), status=response_status)
    set_refresh_cookie(response, refresh)
    return response


class PublicAuthView(APIView):
    """
    Base for unauthenticated auth endpoints. No Authorization header is read, but
    401 responses still carry WWW-Authenticate (DRF would otherwise turn them into 403).
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get_authenticate_header(self, request):
        return 'Bearer realm="api"'


@extend_schema(tags=["auth"])
class CsrfTokenView(PublicAuthView):
    """Issues the CSRF token the SPA sends as X-CSRFToken on cookie-based auth calls."""

    @extend_schema(
        responses=inline_serializer(
            name="CsrfTokenResponse", fields={"csrf_token": serializers.CharField()}
        )
    )
    def get(self, request, *args, **kwargs):
        return Response({"csrf_token": get_token(request)})


@extend_schema(tags=["auth"])
class RegisterView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    @extend_schema(request=RegisterSerializer, responses={201: _session_response})
    def post(self, request, *args, **kwargs):
        enforce_csrf(request)
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return _start_session(user, status.HTTP_201_CREATED)


@extend_schema(tags=["auth"])
class LoginView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    @extend_schema(
        request=LoginSerializer,
        responses={200: _session_response, 401: OpenApiResponse(description="Invalid credentials")},
    )
    def post(self, request, *args, **kwargs):
        enforce_csrf(request)
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return _start_session(serializer.validated_data["user"])


class AdministratorRequired(exceptions.PermissionDenied):
    default_detail = "This account doesn't have administrator access."
    default_code = "admin_required"


@extend_schema(tags=["auth"])
class AdminLoginView(PublicAuthView):
    """
    Admin panel sign-in. Same credentials check as login, but only an
    administrator gets a session: anyone else is refused before tokens are issued.
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    @extend_schema(
        request=LoginSerializer,
        responses={
            200: _session_response,
            401: OpenApiResponse(description="Invalid credentials"),
            403: OpenApiResponse(description="Not an administrator"),
        },
    )
    def post(self, request, *args, **kwargs):
        enforce_csrf(request)
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        if user.role != User.Role.ADMIN:
            raise AdministratorRequired()
        return _start_session(user)


class GoogleSignInUnavailable(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = (
        "Google sign-in isn't available right now. Log in with your email and password."
    )
    default_code = "google_signin_unavailable"


@extend_schema(tags=["auth"])
class AuthProvidersView(PublicAuthView):
    """Sign-in methods the frontend should offer."""

    @extend_schema(
        responses=inline_serializer(
            name="AuthProvidersResponse",
            fields={"google_client_id": serializers.CharField(allow_null=True)},
        )
    )
    def get(self, request, *args, **kwargs):
        return Response({"google_client_id": settings.GOOGLE_OAUTH_CLIENT_ID or None})


@extend_schema(tags=["auth"])
class GoogleSignInView(PublicAuthView):
    """
    Signs in (or signs up) with a Google ID token. The token's signature,
    audience, issuer, expiry and verified email are checked on the server.
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    @extend_schema(
        request=GoogleSignInSerializer,
        responses={
            200: _session_response,
            201: _session_response,
            401: OpenApiResponse(description="Invalid Google token or deactivated account"),
            409: OpenApiResponse(description="Email linked to a different Google account"),
        },
    )
    def post(self, request, *args, **kwargs):
        enforce_csrf(request)
        claims = _verified_google_claims(request)
        try:
            user, created = google.user_for_claims(claims)
        except google.AccountInactive as exc:
            raise _account_inactive() from exc
        except google.GoogleAccountMismatch:
            return _google_mismatch_response(request)
        return _start_session(user, status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema(tags=["auth"])
class AdminGoogleSignInView(PublicAuthView):
    """
    Admin panel sign-in with Google. Never creates an account: the Google
    identity must belong to an active administrator.
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    @extend_schema(
        request=GoogleSignInSerializer,
        responses={
            200: _session_response,
            401: OpenApiResponse(description="Invalid Google token or deactivated account"),
            403: OpenApiResponse(description="No administrator account for this Google identity"),
            409: OpenApiResponse(description="Email linked to a different Google account"),
        },
    )
    def post(self, request, *args, **kwargs):
        enforce_csrf(request)
        claims = _verified_google_claims(request)
        try:
            user = google.admin_for_claims(claims)
        except google.NotAdministrator as exc:
            raise AdministratorRequired() from exc
        except google.AccountInactive as exc:
            raise _account_inactive() from exc
        except google.GoogleAccountMismatch:
            return _google_mismatch_response(request)
        return _start_session(user)


def _verified_google_claims(request):
    """Validate the request body and return the Google ID token's verified claims."""
    if not google.is_enabled():
        raise GoogleSignInUnavailable()
    serializer = GoogleSignInSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        return google.verify_id_token(serializer.validated_data["credential"])
    except google.GoogleUnavailable as exc:
        raise GoogleSignInUnavailable() from exc
    except google.GoogleTokenInvalid as exc:
        raise exceptions.AuthenticationFailed(
            "Google sign-in failed. Please try again.", code="google_token_invalid"
        ) from exc


def _account_inactive():
    return exceptions.AuthenticationFailed(
        "This account has been deactivated.", code="account_inactive"
    )


def _google_mismatch_response(request):
    return Response(
        error_payload(
            "google_account_mismatch",
            "This email address is linked to a different Google account.",
            request,
        ),
        status=status.HTTP_409_CONFLICT,
    )


@extend_schema(tags=["auth"])
class RefreshView(PublicAuthView):
    """Exchanges the refresh cookie for a new access token and rotates the cookie."""

    @extend_schema(
        request=None,
        responses={200: _session_response, 401: OpenApiResponse(description="Session expired")},
    )
    def post(self, request, *args, **kwargs):
        enforce_csrf(request)
        raw_token = read_refresh_cookie(request)
        if not raw_token:
            raise SessionExpired()

        try:
            old_refresh = RefreshToken(raw_token)  # validates signature, expiry, blacklist
            user = User.objects.get(pk=old_refresh["user_id"], is_active=True)
            if issued_before_revocation(user, old_refresh.get("iat")):
                raise TokenError("Session revoked by an administrator.")
            old_refresh.blacklist()
        except (TokenError, KeyError, User.DoesNotExist):
            # Returned (not raised) so the stale cookie is cleared on the error response.
            response = Response(
                error_payload(
                    SessionExpired.default_code, str(SessionExpired.default_detail), request
                ),
                status=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": self.get_authenticate_header(request)},
            )
            clear_refresh_cookie(response)
            return response
        return _start_session(user)


@extend_schema(tags=["auth"])
class LogoutView(PublicAuthView):
    """Revokes the current refresh token and clears the cookie. Always succeeds."""

    @extend_schema(request=None, responses={204: None})
    def post(self, request, *args, **kwargs):
        enforce_csrf(request)
        raw_token = read_refresh_cookie(request)
        if raw_token:
            try:
                RefreshToken(raw_token).blacklist()
            except TokenError:
                pass  # already expired or revoked
        response = Response(status=status.HTTP_204_NO_CONTENT)
        clear_refresh_cookie(response)
        return response


@extend_schema(tags=["auth"])
class MeView(RetrieveUpdateAPIView):
    """The signed-in user's profile."""

    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "options"]

    def get_object(self):
        return self.request.user


@extend_schema(tags=["auth"])
class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    @extend_schema(request=PasswordChangeSerializer, responses={200: _session_response})
    def post(self, request, *args, **kwargs):
        enforce_csrf(request)  # a new refresh cookie is set below
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        # Sign out every other session; this one continues with fresh tokens.
        revoke_all_refresh_tokens(user)
        return _start_session(user)
