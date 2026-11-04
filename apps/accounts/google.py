"""
Sign in with Google: verification of Google Identity Services ID tokens.

The browser gets a signed ID token (a JWT) from Google and posts it to the API.
It is trusted only after its RS256 signature is checked against Google's
published keys and its audience (our client ID), issuer, expiry and verified
email are confirmed. No Google client secret is involved in this flow.
"""

import logging

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

logger = logging.getLogger("prepwise.accounts")

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
# Tolerance for clock differences between Google and this server.
CLOCK_SKEW_SECONDS = 30

_jwk_client = None


class GoogleTokenInvalid(Exception):
    """The ID token is forged, expired, for another app, or lacks a verified email."""


class GoogleUnavailable(Exception):
    """Google's signing keys couldn't be fetched."""


class GoogleAccountMismatch(Exception):
    """The email belongs to an account already linked to a different Google account."""


class AccountInactive(Exception):
    """The matching account has been deactivated."""


class NotAdministrator(Exception):
    """Admin sign-in with a Google identity that has no administrator account."""


def is_enabled():
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID)


def _signing_key(token):
    global _jwk_client
    if _jwk_client is None:
        # Keys are cached in memory; Google rotates them about every few weeks.
        _jwk_client = jwt.PyJWKClient(GOOGLE_CERTS_URL, cache_keys=True, lifespan=3600, timeout=10)
    try:
        return _jwk_client.get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientConnectionError as exc:
        raise GoogleUnavailable() from exc


def verify_id_token(token):
    """Return the verified claims of a Google ID token, or raise GoogleTokenInvalid."""
    try:
        key = _signing_key(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.GOOGLE_OAUTH_CLIENT_ID,
            issuer=GOOGLE_ISSUERS,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except GoogleUnavailable:
        raise
    except jwt.PyJWTError as exc:
        logger.info("Rejected a Google ID token: %s", type(exc).__name__)
        raise GoogleTokenInvalid() from exc
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise GoogleTokenInvalid()
    return claims


def _existing_user(user_model, sub, email, require_admin=False):
    """
    The account for this Google identity, linking it by email if needed. None if new.

    With require_admin, a non-administrator raises NotAdministrator before
    anything is linked, so an admin sign-in attempt never changes a user account.
    """
    with transaction.atomic():
        user = user_model.objects.select_for_update().filter(google_sub=sub).first()
        linking = user is None
        if linking:
            user = user_model.objects.select_for_update().filter(email__iexact=email).first()
        if user is None:
            return None
        if not user.is_active:
            raise AccountInactive()
        if require_admin and user.role != user_model.Role.ADMIN:
            raise NotAdministrator()
        if linking:
            if user.google_sub:
                raise GoogleAccountMismatch()
            user.google_sub = sub
            user.save(update_fields=["google_sub"])
        return user


def user_for_claims(claims):
    """
    Return (user, created) for a verified Google identity.

    Matches the Google account ID first, then the verified email address (which
    links an existing password account to Google), and otherwise creates an
    account with no usable password. Deactivated accounts are refused before
    anything is written.
    """
    user_model = get_user_model()
    sub = claims["sub"]
    email = user_model.objects.normalize_email(claims["email"]).strip().lower()

    user = _existing_user(user_model, sub, email)
    if user is not None:
        return user, False
    try:
        with transaction.atomic():
            user = user_model.objects.create_user(
                email=email,
                password=None,  # Google-only account: no password to guess
                full_name=(claims.get("name") or "").strip()[:150],
                google_sub=sub,
            )
    except IntegrityError:
        # A concurrent first sign-in for the same identity created it first.
        user = _existing_user(user_model, sub, email)
        if user is None:
            raise
        return user, False
    return user, True


def admin_for_claims(claims):
    """
    The administrator account for a verified Google identity.

    Unlike user_for_claims this never creates an account: the identity must
    match an active administrator (by Google ID, or by verified email, which
    then links it). Anything else raises NotAdministrator or AccountInactive.
    """
    user_model = get_user_model()
    email = user_model.objects.normalize_email(claims["email"]).strip().lower()
    user = _existing_user(user_model, claims["sub"], email, require_admin=True)
    if user is None:
        raise NotAdministrator()
    return user
