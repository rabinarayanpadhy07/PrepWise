from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from conftest import PASSWORD

pytestmark = pytest.mark.django_db

User = get_user_model()

AUTH = "/api/v1/auth"
COOKIE = "prepwise_refresh"
NEW_PASSWORD = "Another-Strong-Pass-42"


def register(client, **overrides):
    payload = {"email": "new@example.com", "full_name": "New User", "password": PASSWORD}
    payload.update(overrides)
    return client.post(f"{AUTH}/register/", payload, format="json")


def login(client, email="user@example.com", password=PASSWORD):
    return client.post(f"{AUTH}/login/", {"email": email, "password": password}, format="json")


def bearer(client, access):
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_register_creates_user_and_starts_session(api_client):
    response = register(api_client)

    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == "new@example.com"
    assert body["user"]["role"] == "user"
    assert body["access"]
    assert "refresh" not in body  # the refresh token is only ever in the cookie

    cookie = response.cookies[COOKIE]
    assert cookie["httponly"] is True
    assert cookie["path"] == "/api/v1/auth/"
    assert cookie["samesite"] == "Lax"
    assert cookie["secure"] is True


def test_register_normalises_email(api_client):
    response = register(api_client, email="  Mixed.Case@Example.COM ")

    assert response.status_code == 201
    assert response.json()["user"]["email"] == "mixed.case@example.com"


def test_register_rejects_duplicate_email_case_insensitively(api_client, user):
    response = register(api_client, email="USER@example.com")

    assert response.status_code == 400
    assert response.json()["error"]["details"]["email"] == [
        "An account with this email address already exists."
    ]


@pytest.mark.parametrize(
    "password, fragment",
    [("short", "too short"), ("12345678901", "entirely numeric"), ("password123", "too common")],
)
def test_register_enforces_password_rules(api_client, password, fragment):
    response = register(api_client, password=password)

    assert response.status_code == 400
    messages = " ".join(response.json()["error"]["details"]["password"])
    assert fragment in messages


def test_register_validates_required_fields(api_client):
    response = api_client.post(f"{AUTH}/register/", {"email": "not-an-email"}, format="json")

    details = response.json()["error"]["details"]
    assert response.status_code == 400
    assert set(details) >= {"email", "full_name", "password"}


def test_register_cannot_grant_admin_privileges(api_client):
    response = register(api_client, role="admin", is_staff=True, is_superuser=True)

    assert response.status_code == 201
    user = User.objects.get(email="new@example.com")
    assert user.role == "user"
    assert not user.is_staff and not user.is_superuser


# --------------------------------------------------------------------------
# Login, current user, logout
# --------------------------------------------------------------------------


def test_login_returns_access_token_and_sets_refresh_cookie(api_client, user):
    response = login(api_client)

    assert response.status_code == 200
    assert response.json()["user"]["id"] == user.id
    assert response.cookies[COOKIE].value

    me = bearer(APIClient(), response.json()["access"]).get(f"{AUTH}/me/")
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.com"


@pytest.mark.parametrize(
    "email, password",
    [("user@example.com", "wrong-password"), ("nobody@example.com", PASSWORD)],
)
def test_login_rejects_invalid_credentials_with_one_message(api_client, user, email, password):
    response = login(api_client, email=email, password=password)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert response.json()["error"]["message"] == "Incorrect email or password."
    assert COOKIE not in response.cookies


def test_inactive_user_cannot_log_in(api_client, make_user):
    make_user(email="inactive@example.com", is_active=False)

    response = login(api_client, email="inactive@example.com")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_me_requires_authentication(api_client):
    response = api_client.get(f"{AUTH}/me/")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_profile_update_only_changes_full_name(auth_client, user):
    response = auth_client.patch(
        f"{AUTH}/me/",
        {"full_name": "  Renamed User ", "role": "admin", "email": "hijack@example.com"},
        format="json",
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.full_name == "Renamed User"
    assert user.role == "user"
    assert user.email == "user@example.com"


def test_profile_update_rejects_blank_name(auth_client):
    response = auth_client.patch(f"{AUTH}/me/", {"full_name": "   "}, format="json")

    assert response.status_code == 400
    assert "full_name" in response.json()["error"]["details"]


def test_logout_revokes_refresh_token_and_clears_cookie(api_client, user):
    login(api_client)
    old_refresh = api_client.cookies[COOKIE].value

    response = api_client.post(f"{AUTH}/logout/")

    assert response.status_code == 204
    assert response.cookies[COOKIE].value == ""
    # The revoked token can't be used even if it was copied before logout.
    replay = APIClient()
    replay.cookies[COOKIE] = old_refresh
    assert replay.post(f"{AUTH}/refresh/").status_code == 401


def test_logout_without_session_still_succeeds(api_client):
    assert api_client.post(f"{AUTH}/logout/").status_code == 204


# --------------------------------------------------------------------------
# Refresh and token expiry
# --------------------------------------------------------------------------


def test_refresh_rotates_cookie_and_returns_new_access_token(api_client, user):
    login(api_client)
    first_refresh = api_client.cookies[COOKIE].value

    response = api_client.post(f"{AUTH}/refresh/")

    assert response.status_code == 200
    assert response.json()["access"]
    assert response.json()["user"]["email"] == user.email
    assert response.cookies[COOKIE].value != first_refresh


def test_rotated_refresh_token_cannot_be_reused(api_client, user):
    login(api_client)
    first_refresh = api_client.cookies[COOKIE].value
    api_client.post(f"{AUTH}/refresh/")

    replay = APIClient()
    replay.cookies[COOKIE] = first_refresh
    response = replay.post(f"{AUTH}/refresh/")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"
    assert response.cookies[COOKIE].value == ""  # stale cookie is cleared


def test_refresh_without_cookie_reports_session_expired(api_client):
    response = api_client.post(f"{AUTH}/refresh/")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


def test_expired_refresh_token_is_rejected(api_client, user):
    token = RefreshToken.for_user(user)
    token.set_exp(lifetime=-timedelta(seconds=1))
    api_client.cookies[COOKIE] = str(token)

    response = api_client.post(f"{AUTH}/refresh/")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


def test_refresh_rejected_for_deactivated_user(api_client, user):
    login(api_client)
    User.objects.filter(pk=user.pk).update(is_active=False)

    assert api_client.post(f"{AUTH}/refresh/").status_code == 401


def test_expired_access_token_is_rejected(api_client, user):
    token = AccessToken.for_user(user)
    token.set_exp(lifetime=-timedelta(seconds=1))

    response = bearer(api_client, str(token)).get(f"{AUTH}/me/")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_not_valid"


def test_tampered_access_token_is_rejected(api_client, user):
    token = str(AccessToken.for_user(user))
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")

    response = bearer(api_client, tampered).get(f"{AUTH}/me/")

    assert response.status_code == 401


def test_access_token_is_rejected_after_user_is_deactivated(api_client, user):
    access = str(AccessToken.for_user(user))
    User.objects.filter(pk=user.pk).update(is_active=False)

    assert bearer(api_client, access).get(f"{AUTH}/me/").status_code == 401


# --------------------------------------------------------------------------
# CSRF protection on cookie endpoints
# --------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["login/", "register/", "refresh/", "logout/"])
def test_cookie_endpoints_require_csrf_token(user, endpoint):
    client = APIClient(enforce_csrf_checks=True)

    response = client.post(f"{AUTH}/{endpoint}", {}, format="json")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_login_succeeds_with_csrf_token(user):
    client = APIClient(enforce_csrf_checks=True)
    csrf = client.get(f"{AUTH}/csrf/")
    assert csrf.status_code == 200
    assert csrf.cookies["csrftoken"]["httponly"] is True

    response = client.post(
        f"{AUTH}/login/",
        {"email": user.email, "password": PASSWORD},
        format="json",
        HTTP_X_CSRFTOKEN=csrf.json()["csrf_token"],
    )

    assert response.status_code == 200


def test_csrf_rejects_untrusted_origin(user):
    client = APIClient(enforce_csrf_checks=True)
    token = client.get(f"{AUTH}/csrf/").json()["csrf_token"]

    response = client.post(
        f"{AUTH}/login/",
        {"email": user.email, "password": PASSWORD},
        format="json",
        HTTP_X_CSRFTOKEN=token,
        HTTP_ORIGIN="https://evil.example",
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------
# Password change and reset
# --------------------------------------------------------------------------


def test_password_change_requires_current_password(auth_client):
    response = auth_client.post(
        f"{AUTH}/password/change/",
        {"current_password": "wrong", "new_password": NEW_PASSWORD},
        format="json",
    )

    assert response.status_code == 400
    assert "current_password" in response.json()["error"]["details"]


def test_password_change_signs_out_other_sessions(api_client, user):
    other_device = APIClient()
    login(other_device)
    old_access = login(api_client).json()["access"]

    response = bearer(api_client, old_access).post(
        f"{AUTH}/password/change/",
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        format="json",
    )

    assert response.status_code == 200
    # Old access tokens stop working immediately; the new one works.
    assert bearer(APIClient(), old_access).get(f"{AUTH}/me/").status_code == 401
    assert bearer(APIClient(), response.json()["access"]).get(f"{AUTH}/me/").status_code == 200
    # Other devices' refresh tokens are revoked.
    assert other_device.post(f"{AUTH}/refresh/").status_code == 401
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)


# --------------------------------------------------------------------------
# Throttling
# --------------------------------------------------------------------------


def test_login_is_rate_limited(api_client, user, monkeypatch):
    monkeypatch.setattr(
        ScopedRateThrottle,
        "THROTTLE_RATES",
        {**ScopedRateThrottle.THROTTLE_RATES, "auth": "2/minute"},
    )
    login(api_client, password="wrong")
    login(api_client, password="wrong")

    response = login(api_client, password="wrong")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "throttled"
