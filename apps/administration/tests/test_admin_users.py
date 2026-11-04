import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from conftest import PASSWORD

pytestmark = pytest.mark.django_db

User = get_user_model()
USERS = "/api/v1/admin/users/"


def test_admin_endpoints_require_authentication(api_client):
    response = api_client.get(USERS)

    assert response.status_code == 401


def test_regular_users_cannot_use_admin_endpoints(auth_client, other_user):
    assert auth_client.get(USERS).status_code == 403
    response = auth_client.patch(f"{USERS}{other_user.id}/", {"role": "admin"}, format="json")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_user_cannot_promote_themselves(auth_client, user):
    response = auth_client.patch(f"{USERS}{user.id}/", {"role": "admin"}, format="json")

    assert response.status_code == 403
    user.refresh_from_db()
    assert user.role == "user"


def test_admin_lists_and_searches_users(client_for, admin_user, user, other_user):
    client = client_for(admin_user)

    everyone = client.get(USERS)
    found = client.get(USERS, {"search": "other"})

    assert everyone.status_code == 200
    assert everyone.json()["count"] == 3
    assert [row["email"] for row in found.json()["results"]] == ["other@example.com"]
    assert "password" not in everyone.json()["results"][0]


def test_admin_can_promote_a_user(client_for, admin_user, user):
    response = client_for(admin_user).patch(f"{USERS}{user.id}/", {"role": "admin"}, format="json")

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.role == "admin"
    assert client_for(user).get(USERS).status_code == 200


def test_admin_cannot_change_their_own_role_or_status(client_for, admin_user):
    response = client_for(admin_user).patch(
        f"{USERS}{admin_user.id}/", {"is_active": False}, format="json"
    )

    assert response.status_code == 400
    admin_user.refresh_from_db()
    assert admin_user.is_active


def test_admin_cannot_edit_identity_fields(client_for, admin_user, user):
    client_for(admin_user).patch(
        f"{USERS}{user.id}/", {"email": "changed@example.com", "full_name": "X"}, format="json"
    )

    user.refresh_from_db()
    assert user.email == "user@example.com"
    assert user.full_name == "Test User"


def test_deactivating_a_user_ends_their_sessions(client_for, admin_user, user):
    session = APIClient()
    session.post("/api/v1/auth/login/", {"email": user.email, "password": PASSWORD}, format="json")
    access = str(AccessToken.for_user(user))

    response = client_for(admin_user).patch(
        f"{USERS}{user.id}/", {"is_active": False}, format="json"
    )

    assert response.status_code == 200
    assert session.post("/api/v1/auth/refresh/").status_code == 401
    me = APIClient()
    me.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert me.get("/api/v1/auth/me/").status_code == 401


def test_demoted_admin_loses_access_immediately(client_for, admin_user, make_user):
    second_admin = make_user(email="second@example.com", role="admin")
    second_client = client_for(second_admin)
    assert second_client.get(USERS).status_code == 200

    client_for(admin_user).patch(f"{USERS}{second_admin.id}/", {"role": "user"}, format="json")

    assert second_client.get(USERS).status_code == 403


def test_superuser_gets_admin_role():
    superuser = User.objects.create_superuser(email="root@example.com", password=PASSWORD)

    assert superuser.role == "admin"
    assert superuser.is_admin
