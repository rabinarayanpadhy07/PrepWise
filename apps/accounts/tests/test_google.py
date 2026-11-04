"""
Sign in with Google. Tokens are signed with a real RSA key and verified with
PyJWT; only the download of Google's public keys is replaced, so signature,
audience, issuer, expiry and email checks are all exercised.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.accounts import google

pytestmark = pytest.mark.django_db

User = get_user_model()
GOOGLE = "/api/v1/auth/google/"
CLIENT_ID = "test-client-id.apps.googleusercontent.com"
GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FORGER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def google_keys(monkeypatch):
    monkeypatch.setattr(google, "_signing_key", lambda token: GOOGLE_KEY.public_key())


def id_token(key=GOOGLE_KEY, **overrides):
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-sub-123",
        "email": "Person@Gmail.com",
        "email_verified": True,
        "name": "Google Person",
        "iat": now,
        "exp": now + 3600,
        **overrides,
    }
    claims = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def sign_in(client, token):
    return client.post(GOOGLE, {"credential": token}, format="json")


def test_providers_endpoint_publishes_only_the_client_id(api_client):
    response = api_client.get("/api/v1/auth/providers/")

    assert response.status_code == 200
    assert response.json() == {"google_client_id": CLIENT_ID}


def test_providers_endpoint_reports_google_disabled(api_client, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""

    assert api_client.get("/api/v1/auth/providers/").json() == {"google_client_id": None}


def test_first_sign_in_creates_an_account_without_a_password(api_client):
    response = sign_in(api_client, id_token())

    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == "person@gmail.com"
    assert body["user"]["full_name"] == "Google Person"
    assert body["user"]["role"] == "user"
    assert body["user"]["has_password"] is False
    assert response.cookies["prepwise_refresh"]["httponly"]
    user = User.objects.get(email="person@gmail.com")
    assert user.google_sub == "google-sub-123"
    assert not user.has_usable_password()
    me = APIClient()
    me.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    assert me.get("/api/v1/auth/me/").status_code == 200


def test_returning_user_is_matched_by_google_id_even_after_an_email_change(api_client):
    sign_in(api_client, id_token())

    response = sign_in(APIClient(), id_token(email="renamed@gmail.com"))

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "person@gmail.com"
    assert User.objects.count() == 1


def test_existing_password_account_is_linked_by_verified_email(api_client, make_user):
    existing = make_user(email="person@gmail.com")

    response = sign_in(api_client, id_token())

    assert response.status_code == 200
    assert response.json()["user"]["id"] == existing.id
    assert response.json()["user"]["has_password"] is True
    existing.refresh_from_db()
    assert existing.google_sub == "google-sub-123"
    assert existing.role == "user"


def test_email_linked_to_another_google_account_is_refused(api_client, make_user):
    make_user(email="person@gmail.com", google_sub="someone-else")

    response = sign_in(api_client, id_token())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "google_account_mismatch"


def test_deactivated_accounts_cannot_sign_in(api_client, make_user):
    user = make_user(email="person@gmail.com", is_active=False)

    response = sign_in(api_client, id_token())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "account_inactive"
    user.refresh_from_db()
    assert user.google_sub is None


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: id_token(key=FORGER_KEY), id="forged-signature"),
        pytest.param(
            lambda: id_token(aud="another-app.apps.googleusercontent.com"), id="other-app"
        ),
        pytest.param(lambda: id_token(iss="https://evil.example"), id="wrong-issuer"),
        pytest.param(lambda: id_token(exp=int(time.time()) - 120), id="expired"),
        pytest.param(lambda: id_token(email_verified=False), id="unverified-email"),
        pytest.param(lambda: id_token(email=None), id="no-email"),
        pytest.param(lambda: id_token(sub=None), id="no-subject"),
        pytest.param(lambda: "not-a-jwt", id="garbage"),
        pytest.param(
            lambda: jwt.encode(
                {"sub": "x", "aud": CLIENT_ID},
                "an-attacker-chosen-hmac-secret-key!",
                algorithm="HS256",
            ),
            id="hs256-confusion",
        ),
    ],
)
def test_invalid_tokens_are_rejected(api_client, token):
    response = sign_in(api_client, token())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "google_token_invalid"
    assert not User.objects.exists()


def test_disabled_without_a_client_id(api_client, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""

    response = sign_in(api_client, id_token())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "google_signin_unavailable"
    assert not User.objects.exists()


def test_google_unreachable_is_reported_as_unavailable(api_client, monkeypatch):
    def unreachable(token):
        raise google.GoogleUnavailable()

    monkeypatch.setattr(google, "_signing_key", unreachable)

    assert sign_in(api_client, id_token()).status_code == 503


def test_google_sign_in_requires_csrf():
    client = APIClient(enforce_csrf_checks=True)

    response = client.post(GOOGLE, {"credential": id_token()}, format="json")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
