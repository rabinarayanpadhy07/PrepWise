import pytest
from django.conf import settings

from common.views import ReadinessView

pytestmark = pytest.mark.django_db


def _secret_values():
    values = [
        settings.SECRET_KEY,
        settings.DATABASES["default"]["NAME"],
        settings.DATABASES["default"]["USER"],
        settings.DATABASES["default"]["PASSWORD"],
        settings.DATABASES["default"]["HOST"],
    ]
    return [v for v in values if v and len(v) > 3]


def test_health_is_public_and_minimal(api_client):
    response = api_client.get("/api/v1/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_ready_when_database_and_migrations_are_ok(api_client):
    response = api_client.get("/api/v1/health/ready/")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "migrations": "ok"},
    }


def test_readiness_returns_503_without_leaking_error_details(api_client, monkeypatch):
    def failing_check():
        raise RuntimeError(f"could not connect: password={settings.SECRET_KEY}")

    monkeypatch.setitem(ReadinessView.checks, "database", failing_check)

    response = api_client.get("/api/v1/health/ready/")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "unavailable", "migrations": "skipped"},
    }
    assert "could not connect" not in response.content.decode()


@pytest.mark.parametrize("path", ["/api/v1/health/", "/api/v1/health/ready/"])
def test_health_responses_expose_no_configuration(api_client, path):
    body = api_client.get(path).content.decode()

    for value in _secret_values():
        assert value not in body
