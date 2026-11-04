import logging

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import GENERIC_MESSAGES, error_payload

logger = logging.getLogger("prepwise.health")

# --------------------------------------------------------------------------
# Health and readiness
# Responses deliberately contain only fixed status strings: no versions,
# hostnames, settings, or exception messages.
# --------------------------------------------------------------------------

_health_response = inline_serializer(
    name="HealthResponse", fields={"status": serializers.CharField()}
)
_readiness_response = inline_serializer(
    name="ReadinessResponse",
    fields={
        "status": serializers.CharField(),
        "checks": serializers.DictField(child=serializers.CharField()),
    },
)


class HealthView(APIView):
    """Liveness: the process is up and serving requests. Touches no dependencies."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    @extend_schema(tags=["health"], responses={200: _health_response})
    def get(self, request, *args, **kwargs):
        return Response({"status": "ok"})


def _check_database():
    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def _check_migrations():
    connection = connections[DEFAULT_DB_ALIAS]
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    if executor.migration_plan(targets):
        raise RuntimeError("Unapplied migrations")


class ReadinessView(APIView):
    """Readiness: the service can handle traffic (database reachable, schema current)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    checks = {
        "database": _check_database,
        "migrations": _check_migrations,
    }

    @extend_schema(
        tags=["health"],
        responses={
            200: _readiness_response,
            503: OpenApiResponse(_readiness_response, description="Not ready"),
        },
    )
    def get(self, request, *args, **kwargs):
        results = {}
        for name, check in self.checks.items():
            try:
                check()
            except Exception:  # noqa: BLE001 - any failure means "not ready"
                logger.exception("Readiness check failed: %s", name)
                results[name] = "unavailable"
                # Later checks depend on the database; stop at the first failure.
                break
            results[name] = "ok"

        ready = len(results) == len(self.checks) and all(v == "ok" for v in results.values())
        for name in self.checks:
            results.setdefault(name, "skipped")
        return Response(
            {"status": "ready" if ready else "unavailable", "checks": results},
            status=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


# --------------------------------------------------------------------------
# Django-level error handlers (requests that never reach a DRF view).
# They return the same JSON envelope as the API.
# --------------------------------------------------------------------------


def _json_error(request, code, http_status):
    return JsonResponse(
        error_payload(code, GENERIC_MESSAGES[http_status], request), status=http_status
    )


def bad_request(request, exception=None):
    return _json_error(request, "bad_request", 400)


def permission_denied(request, exception=None):
    return _json_error(request, "permission_denied", 403)


def page_not_found(request, exception=None):
    return _json_error(request, "not_found", 404)


def server_error(request):
    return _json_error(request, "server_error", 500)
