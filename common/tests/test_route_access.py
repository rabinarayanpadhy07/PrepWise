"""
Every API route requires authentication unless it is on the public list below.

The routes are discovered from the URL configuration, so a new endpoint that
forgets its permission classes fails here rather than shipping as public.
"""

import re
import uuid

import pytest
from django.urls import URLPattern, URLResolver

from config import api_v1

# Route names that are intentionally reachable without signing in.
PUBLIC_ROUTES = {
    "health",
    "readiness",
    "schema",
    "swagger-ui",
    "redoc",
    "accounts:csrf",
    "accounts:register",
    "accounts:login",
    "accounts:refresh",
    "accounts:logout",
    "accounts:providers",
    "accounts:google",
    "accounts:admin-login",
    "accounts:admin-google",
    "billing:plan-list",
    "payments:webhook-razorpay",
}

SAMPLE_VALUES = {
    "int": "999999",
    "uuid": str(uuid.uuid4()),
    "slug": "sample",
    "str": "sample",
    "path": "sample",
}
PARAMETER = re.compile(r"<(?:(\w+):)?(\w+)>")


def _sample_path(route):
    return PARAMETER.sub(lambda match: SAMPLE_VALUES[match.group(1) or "str"], route)


def _walk(patterns, prefix="", namespace=""):
    for pattern in patterns:
        route = str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            child_namespace = pattern.namespace or namespace
            yield from _walk(pattern.url_patterns, prefix + route, child_namespace)
        elif isinstance(pattern, URLPattern):
            name = f"{namespace}:{pattern.name}" if namespace else pattern.name
            yield name, "/api/v1/" + _sample_path(prefix + route), pattern.callback


def _methods(callback):
    view = callback.view_class
    return [
        method
        for method in ("get", "post", "put", "patch", "delete")
        if hasattr(view, method) and method in view.http_method_names
    ]


ROUTES = list(_walk(api_v1.urlpatterns))
PROTECTED = [
    (name, url, method)
    for name, url, callback in ROUTES
    if name not in PUBLIC_ROUTES
    for method in _methods(callback)
]


def test_route_discovery_finds_the_api():
    names = {name for name, _, _ in ROUTES}
    assert len(ROUTES) > 60
    assert {"interviews:session-list", "administration:audit-log", "payments:order-verify"} <= names
    # The public list must not name routes that no longer exist.
    assert PUBLIC_ROUTES - {"schema", "swagger-ui", "redoc"} <= names


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "url", "method"), PROTECTED, ids=[f"{m} {n}" for n, _, m in PROTECTED]
)
def test_protected_routes_reject_anonymous_requests(api_client, name, url, method):
    response = getattr(api_client, method)(url, {}, format="json")

    assert response.status_code == 401, f"{method.upper()} {url} returned {response.status_code}"
    assert response.json()["error"]["code"] in {"not_authenticated", "authentication_failed"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "url", "method"), PROTECTED, ids=[f"{m} {n}" for n, _, m in PROTECTED]
)
def test_protected_routes_reject_forged_tokens(api_client, name, url, method):
    api_client.credentials(HTTP_AUTHORIZATION="Bearer not.a.real.token")
    response = getattr(api_client, method)(url, {}, format="json")

    assert response.status_code == 401, f"{method.upper()} {url} returned {response.status_code}"
