"""JWT authentication that also honours administrator session revocation."""

from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme
from rest_framework_simplejwt.authentication import JWTAuthentication as BaseJWTAuthentication

from apps.accounts.security import SessionExpired


def issued_before_revocation(user, issued_at):
    """True if a token issued at `issued_at` (Unix seconds) predates the user's revocation."""
    revoked = getattr(user, "sessions_revoked_at", None)
    if revoked is None or issued_at is None:
        return False
    # Compare in whole seconds, as JWT iat claims are; a token from the same
    # second as the revocation is treated as revoked.
    return int(issued_at) <= int(revoked.timestamp())


class JWTAuthentication(BaseJWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if issued_before_revocation(user, validated_token.get("iat")):
            raise SessionExpired()
        return user


class JWTAuthenticationScheme(SimpleJWTScheme):
    """Documents this subclass in the OpenAPI schema like SimpleJWT's own class."""

    target_class = "apps.accounts.authentication.JWTAuthentication"
