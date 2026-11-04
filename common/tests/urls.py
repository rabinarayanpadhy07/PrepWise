"""Test-only views used to exercise the API error format. Not part of the real URLconf."""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.urls import include, re_path
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from config.urls import handler400, handler403, handler404, handler500  # noqa: F401
from config.urls import urlpatterns as project_urlpatterns


class SignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    age = serializers.IntegerField(min_value=18)

    def validate(self, attrs):
        if attrs["email"].endswith("@blocked.example"):
            raise serializers.ValidationError("This email domain is not allowed.")
        return attrs


class ValidationView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data)


class DjangoValidationView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, *args, **kwargs):
        raise DjangoValidationError({"title": ["Title is too long."]})


class CrashView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, *args, **kwargs):
        raise RuntimeError("database password=hunter2-should-not-leak")


class ProtectedView(APIView):
    # Uses the default IsAuthenticated permission and JWT authentication.
    def get(self, request, *args, **kwargs):
        return Response({"ok": True})


class OncePerMinuteThrottle(SimpleRateThrottle):
    rate = "1/minute"

    def get_cache_key(self, request, view):
        return "test-throttle"


class ThrottledView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [OncePerMinuteThrottle]

    def get(self, request, *args, **kwargs):
        return Response({"ok": True})


test_patterns = [
    re_path(r"^validation/$", ValidationView.as_view()),
    re_path(r"^django-validation/$", DjangoValidationView.as_view()),
    re_path(r"^crash/$", CrashView.as_view()),
    re_path(r"^protected/$", ProtectedView.as_view()),
    re_path(r"^throttled/$", ThrottledView.as_view()),
]

urlpatterns = [
    re_path(r"^api/(?P<version>v1)/test/", include(test_patterns)),
    *project_urlpatterns,
]
