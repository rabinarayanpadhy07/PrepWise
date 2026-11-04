import pytest

pytestmark = [pytest.mark.django_db, pytest.mark.urls("common.tests.urls")]


def assert_error(response, status, code):
    assert response.status_code == status
    assert response["Content-Type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert body["error"]["request_id"] == response["X-Request-ID"]
    return body["error"]


def test_field_validation_errors_are_grouped_by_field(api_client):
    response = api_client.post(
        "/api/v1/test/validation/", {"email": "not-an-email", "age": 12}, format="json"
    )

    error = assert_error(response, 400, "validation_error")
    assert set(error["details"]) == {"email", "age"}
    assert all(isinstance(m, str) for m in error["details"]["email"])


def test_missing_fields_are_reported(api_client):
    response = api_client.post("/api/v1/test/validation/", {}, format="json")

    error = assert_error(response, 400, "validation_error")
    assert error["details"]["email"] == ["This field is required."]


def test_object_level_errors_use_non_field_errors(api_client):
    response = api_client.post(
        "/api/v1/test/validation/", {"email": "a@blocked.example", "age": 30}, format="json"
    )

    error = assert_error(response, 400, "validation_error")
    assert error["details"] == {"non_field_errors": ["This email domain is not allowed."]}


def test_django_validation_error_is_converted(api_client):
    response = api_client.get("/api/v1/test/django-validation/")

    error = assert_error(response, 400, "validation_error")
    assert error["details"] == {"title": ["Title is too long."]}


def test_malformed_json_returns_parse_error(api_client):
    response = api_client.post(
        "/api/v1/test/validation/", data="{not json", content_type="application/json"
    )

    assert_error(response, 400, "parse_error")


def test_unsupported_media_type(api_client):
    response = api_client.post(
        "/api/v1/test/validation/", data="email=a@b.com", content_type="text/plain"
    )

    assert_error(response, 415, "unsupported_media_type")


def test_unauthenticated_request(api_client):
    response = api_client.get("/api/v1/test/protected/")

    assert_error(response, 401, "not_authenticated")


def test_invalid_bearer_token(api_client):
    api_client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
    response = api_client.get("/api/v1/test/protected/")

    error = assert_error(response, 401, "token_not_valid")
    assert "not-a-real-token" not in str(error)


def test_method_not_allowed(api_client):
    response = api_client.delete("/api/v1/health/")

    assert_error(response, 405, "method_not_allowed")


def test_unknown_api_route_returns_json_404(api_client):
    response = api_client.get("/api/v1/does-not-exist/")

    assert_error(response, 404, "not_found")


def test_unsupported_api_version_returns_404(api_client):
    response = api_client.get("/api/v2/health/")

    assert_error(response, 404, "not_found")


def test_throttled_request_includes_retry_after(api_client):
    assert api_client.get("/api/v1/test/throttled/").status_code == 200

    response = api_client.get("/api/v1/test/throttled/")

    error = assert_error(response, 429, "throttled")
    assert error["retry_after_seconds"] >= 1


def test_unhandled_exception_hides_internal_details(api_client, caplog):
    with caplog.at_level("ERROR", logger="prepwise.api"):
        response = api_client.get("/api/v1/test/crash/")

    error = assert_error(response, 500, "server_error")
    assert "hunter2" not in response.content.decode()
    assert "RuntimeError" not in response.content.decode()
    assert error["message"] == "An unexpected error occurred. Please try again later."


def test_client_supplied_request_id_is_echoed_when_safe(api_client):
    response = api_client.get("/api/v1/health/", HTTP_X_REQUEST_ID="abc-123")
    assert response["X-Request-ID"] == "abc-123"


def test_unsafe_request_id_is_replaced(api_client):
    response = api_client.get("/api/v1/health/", HTTP_X_REQUEST_ID="bad id\nwith newline")
    assert response["X-Request-ID"] != "bad id\nwith newline"
    assert len(response["X-Request-ID"]) == 32
