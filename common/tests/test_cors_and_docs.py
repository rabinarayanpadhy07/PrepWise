import pytest

pytestmark = pytest.mark.django_db

ALLOWED_ORIGIN = "http://localhost:5173"


def test_cors_allows_listed_origin(api_client):
    response = api_client.options(
        "/api/v1/health/",
        HTTP_ORIGIN=ALLOWED_ORIGIN,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
    )

    assert response["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN


def test_cors_rejects_unlisted_origin(api_client):
    response = api_client.get("/api/v1/health/", HTTP_ORIGIN="https://evil.example.com")

    assert "Access-Control-Allow-Origin" not in response


def test_openapi_schema_is_served(api_client):
    response = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "PrepWise API"
    assert "/api/v1/health/" in schema["paths"]
    assert "/api/v1/billing/plans/" in schema["paths"]
