"""
Configuration tests.

Production settings are imported in a subprocess so each case starts from a
clean environment (settings modules are evaluated once per process).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

BACKEND_DIR = Path(__file__).resolve().parents[2]

VALID_PRODUCTION_ENV = {
    "DJANGO_SECRET_KEY": "p" * 20 + "-production-test-key-0123456789-abcdefghij",
    "DJANGO_ALLOWED_HOSTS": "api.prepwise.example",
    "CORS_ALLOWED_ORIGINS": "https://app.prepwise.example",
    "CSRF_TRUSTED_ORIGINS": "https://app.prepwise.example",
    "DB_NAME": "prepwise",
    "DB_USER": "prepwise",
    "DB_PASSWORD": "a-database-password",
    "DB_HOST": "db.internal",
    "AI_KEY_ENCRYPTION_KEYS": "k1:" + "A" * 43,
}

PRINT_SETTINGS = """
import json, django
from django.conf import settings
django.setup()
print(json.dumps({
    "DEBUG": settings.DEBUG,
    "ENGINE": settings.DATABASES["default"]["ENGINE"],
    "SSLMODE": settings.DATABASES["default"]["OPTIONS"]["sslmode"],
    "ALLOWED_HOSTS": settings.ALLOWED_HOSTS,
    "CORS_ALLOW_ALL_ORIGINS": settings.CORS_ALLOW_ALL_ORIGINS,
    "CORS_ALLOWED_ORIGINS": settings.CORS_ALLOWED_ORIGINS,
    "SECURE_SSL_REDIRECT": settings.SECURE_SSL_REDIRECT,
    "SESSION_COOKIE_SECURE": settings.SESSION_COOKIE_SECURE,
    "CSRF_COOKIE_SECURE": settings.CSRF_COOKIE_SECURE,
    "SECURE_HSTS_SECONDS": settings.SECURE_HSTS_SECONDS,
    "API_DOCS_ENABLED": settings.API_DOCS_ENABLED,
    "RENDERERS": settings.REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"],
    "WHITENOISE": "whitenoise.middleware.WhiteNoiseMiddleware" in settings.MIDDLEWARE,
    "AUTH_COOKIE": settings.AUTH_COOKIE,
    "CSRF_COOKIE_HTTPONLY": settings.CSRF_COOKIE_HTTPONLY,
    "CORS_ALLOW_CREDENTIALS": settings.CORS_ALLOW_CREDENTIALS,
}))
"""


def run_settings(module, env_overrides=None, remove=()):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            (
                "DJANGO_",
                "DB_",
                "CORS_",
                "CSRF_",
                "API_",
                "AUTH_",
                "FRONTEND_",
                "AI_",
                "PAYMENT_",
            )
        )
    }
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": module,
            # Never let a developer's backend/.env influence these tests.
            "DJANGO_READ_ENV_FILE": "false",
        }
    )
    env.update(env_overrides or {})
    for key in remove:
        env.pop(key, None)
    return subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", PRINT_SETTINGS],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_valid_production_configuration_is_secure():
    result = run_settings("config.settings.production", VALID_PRODUCTION_ENV)

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["DEBUG"] is False
    assert config["ENGINE"] == "django.db.backends.postgresql"
    assert config["SSLMODE"] == "require"
    assert config["ALLOWED_HOSTS"] == ["api.prepwise.example"]
    assert config["CORS_ALLOW_ALL_ORIGINS"] is False
    assert config["CORS_ALLOWED_ORIGINS"] == ["https://app.prepwise.example"]
    assert config["SECURE_SSL_REDIRECT"] is True
    assert config["SESSION_COOKIE_SECURE"] is True
    assert config["CSRF_COOKIE_SECURE"] is True
    assert config["SECURE_HSTS_SECONDS"] > 0
    assert config["API_DOCS_ENABLED"] is False
    assert config["RENDERERS"] == ["rest_framework.renderers.JSONRenderer"]
    assert config["WHITENOISE"] is True
    assert config["AUTH_COOKIE"]["SECURE"] is True
    assert config["AUTH_COOKIE"]["SAMESITE"] == "Lax"
    assert config["CSRF_COOKIE_HTTPONLY"] is True
    assert config["CORS_ALLOW_CREDENTIALS"] is True


@pytest.mark.parametrize(
    ("overrides", "remove", "message"),
    [
        ({}, ["DJANGO_SECRET_KEY"], "DJANGO_SECRET_KEY is required"),
        ({"DJANGO_SECRET_KEY": "too-short"}, [], "at least 50 characters"),
        (
            {"DJANGO_SECRET_KEY": "django-insecure-" + "x" * 60},
            [],
            "at least 50 characters",
        ),
        ({"DJANGO_DEBUG": "true"}, [], "DJANGO_DEBUG must be false"),
        ({}, ["DJANGO_ALLOWED_HOSTS"], "DJANGO_ALLOWED_HOSTS is required"),
        ({"DJANGO_ALLOWED_HOSTS": "*"}, [], "must not contain '*'"),
        ({"CORS_ALLOWED_ORIGINS": "http://app.prepwise.example"}, [], "https://"),
        ({}, ["DB_PASSWORD"], "DB_PASSWORD is required"),
        ({"AUTH_COOKIE_SECURE": "false"}, [], "AUTH_COOKIE_SECURE must be true"),
        ({"AUTH_COOKIE_SAMESITE": "Sometimes"}, [], "AUTH_COOKIE_SAMESITE must be"),
        ({}, ["AI_KEY_ENCRYPTION_KEYS"], "AI_KEY_ENCRYPTION_KEYS is required"),
        ({"AI_KEY_ENCRYPTION_KEYS": "not-a-key"}, [], "AI_KEY_ENCRYPTION_KEYS is invalid"),
        # A 16-byte key is AES-128, not the required AES-256.
        (
            {"AI_KEY_ENCRYPTION_KEYS": "k1:MDEyMzQ1Njc4OWFiY2RlZg"},
            [],
            "must decode to 32 bytes",
        ),
        ({"PAYMENT_PROVIDER": "paypal"}, [], "PAYMENT_PROVIDER must be 'razorpay'"),
        ({"PAYMENT_PROVIDER": "razorpay"}, [], "PAYMENT_WEBHOOK_SECRET are required"),
    ],
)
def test_production_refuses_unsafe_configuration(overrides, remove, message):
    result = run_settings(
        "config.settings.production", {**VALID_PRODUCTION_ENV, **overrides}, remove=remove
    )

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert message in result.stderr
    # The error must not echo the configured secrets.
    assert VALID_PRODUCTION_ENV["DB_PASSWORD"] not in result.stderr


def test_development_settings_have_safe_local_defaults():
    result = run_settings("config.settings.development")

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["DEBUG"] is True
    assert config["ENGINE"] == "django.db.backends.postgresql"
    assert config["ALLOWED_HOSTS"] == ["localhost", "127.0.0.1"]
    assert config["CORS_ALLOW_ALL_ORIGINS"] is False
    assert config["CORS_ALLOWED_ORIGINS"] == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]


def test_comma_separated_lists_are_trimmed():
    result = run_settings(
        "config.settings.development",
        {"DJANGO_ALLOWED_HOSTS": " a.example , ,b.example ", "CORS_ALLOWED_ORIGINS": ""},
    )

    config = json.loads(result.stdout)
    assert config["ALLOWED_HOSTS"] == ["a.example", "b.example"]


def test_active_settings_use_postgresql_and_explicit_cors():
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"
    assert settings.CORS_ALLOW_ALL_ORIGINS is False
    assert all("*" not in origin for origin in settings.CORS_ALLOWED_ORIGINS)


def test_api_versioning_is_configured():
    rest = settings.REST_FRAMEWORK
    assert rest["DEFAULT_VERSIONING_CLASS"] == "rest_framework.versioning.URLPathVersioning"
    assert rest["ALLOWED_VERSIONS"] == ["v1"]
    assert rest["EXCEPTION_HANDLER"] == "common.exceptions.api_exception_handler"
