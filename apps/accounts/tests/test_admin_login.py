"""
Admin panel sign-in (/auth/admin/login/ and /auth/admin/google/). Only
administrators get a session; nobody else gets tokens, a refresh cookie, a new
account, or a Google link.
"""

import pytest
from django.contrib.auth import get_user_model

from apps.accounts import google
from apps.accounts.tests.test_google import GOOGLE_KEY, id_token
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

User = get_user_model()
REFRESH_COOKIE_NAME = "prepwise_refresh"
ADMIN_LOGIN = "/api/v1/auth/admin/login/"
ADMIN_GOOGLE = "/api/v1/auth/admin/google/"


@pytest.fixture(autouse=True)
def google_keys(monkeypatch):
    monkeypatch.setattr(google, "_signing_key", lambda token: GOOGLE_KEY.public_key())


def admin_login(client, email, password=PASSWORD):
    return client.post(ADMIN_LOGIN, {"email": email, "password": password}, format="json")


def admin_google(client, token):
    return client.post(ADMIN_GOOGLE, {"credential": token}, format="json")


def test_admin_login_starts_a_session_for_an_administrator(api_client, admin_user):
    response = admin_login(api_client, "ADMIN@example.com")

    assert response.status_code == 200
    body = response.json()
    assert body["access"]
    assert body["user"]["role"] == "admin"
    assert response.cookies[REFRESH_COOKIE_NAME].value


def test_admin_login_refuses_a_regular_user_without_issuing_tokens(api_client, user):
    response = admin_login(api_client, user.email)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_required"
    assert "access" not in response.json()
    assert not response.cookies.get(REFRESH_COOKIE_NAME)


def test_admin_login_rejects_a_wrong_password(api_client, admin_user):
    response = admin_login(api_client, admin_user.email, password="wrong-password-123")

    assert response.status_code == 401
    assert not response.cookies.get(REFRESH_COOKIE_NAME)


def test_admin_login_rejects_a_deactivated_administrator(api_client, admin_user):
    admin_user.is_active = False
    admin_user.save(update_fields=["is_active"])

    assert admin_login(api_client, admin_user.email).status_code == 401


def test_admin_google_signs_in_a_linked_administrator(api_client, make_user):
    make_user(email="boss@gmail.com", role="admin", google_sub="google-sub-123")

    response = admin_google(api_client, id_token(email="boss@gmail.com"))

    assert response.status_code == 200
    assert response.json()["user"]["role"] == "admin"
    assert response.cookies[REFRESH_COOKIE_NAME].value


def test_admin_google_links_an_administrator_by_verified_email(api_client, make_user):
    admin = make_user(email="person@gmail.com", role="admin")

    response = admin_google(api_client, id_token())

    assert response.status_code == 200
    admin.refresh_from_db()
    assert admin.google_sub == "google-sub-123"


def test_admin_google_never_creates_an_account(api_client):
    response = admin_google(api_client, id_token())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_required"
    assert not User.objects.exists()
    assert not response.cookies.get(REFRESH_COOKIE_NAME)


def test_admin_google_refuses_a_regular_user_and_does_not_link_them(api_client, user):
    user.email = "person@gmail.com"
    user.save(update_fields=["email"])

    response = admin_google(api_client, id_token())

    assert response.status_code == 403
    user.refresh_from_db()
    assert user.google_sub is None


def test_admin_google_refuses_a_deactivated_administrator(api_client, make_user):
    make_user(email="person@gmail.com", role="admin", is_active=False)

    response = admin_google(api_client, id_token())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "account_inactive"


def test_admin_google_rejects_a_forged_token(api_client, make_user):
    from apps.accounts.tests.test_google import FORGER_KEY

    make_user(email="person@gmail.com", role="admin", google_sub="google-sub-123")

    response = admin_google(api_client, id_token(key=FORGER_KEY))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "google_token_invalid"


def test_admin_google_is_unavailable_without_a_client_id(api_client, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""

    response = admin_google(api_client, id_token())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "google_signin_unavailable"
