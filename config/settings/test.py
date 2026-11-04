"""Settings for the automated test suite (PostgreSQL, like every other environment)."""

from .base import *  # noqa: F403
from .base import env

# The suite creates and drops its own database, so it can point at a different
# server from the app (for example a local PostgreSQL while the app uses a hosted
# one). Each DB_TEST_* value falls back to the matching DB_* value.
for _key, _setting in (
    ("HOST", "DB_TEST_HOST"),
    ("PORT", "DB_TEST_PORT"),
    ("USER", "DB_TEST_USER"),
    ("PASSWORD", "DB_TEST_PASSWORD"),
):
    if env.str(_setting, default=""):
        DATABASES["default"][_key] = env.str(_setting)  # noqa: F405
if env.str("DB_TEST_SSLMODE", default=""):
    DATABASES["default"]["OPTIONS"]["sslmode"] = env.str("DB_TEST_SSLMODE")  # noqa: F405
if env.str("DB_TEST_HOST", default=""):
    # NAME is only the maintenance connection; the suite uses TEST["NAME"].
    DATABASES["default"]["NAME"] = env.str("DB_TEST_MAINTENANCE_DB", default="postgres")  # noqa: F405

DEBUG = False
SECRET_KEY = "test-only-secret-key-not-used-anywhere-else-0123456789abcdef"
SIMPLE_JWT["SIGNING_KEY"] = SECRET_KEY  # noqa: F405
ALLOWED_HOSTS = ["testserver", "localhost"]
CORS_ALLOWED_ORIGINS = ["http://localhost:5173"]
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS
API_DOCS_ENABLED = True

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


# Tests never call a real AI provider or payment gateway: they install fakes
# (see conftest.py). These values are test-only and not real credentials.
AI_SETTINGS = {  # noqa: F405
    **AI_SETTINGS,  # noqa: F405
    # Two fixed, test-only AES-256 keys (32 bytes each), so rotation can be tested.
    "KEY_ENCRYPTION_KEYS": (
        "test2:dGVzdC1vbmx5LWFlcy1rZXktbnVtYmVyLXR3by0yISE,"
        "test1:dGVzdC1vbmx5LWFlcy1rZXktbnVtYmVyLW9uZS0xISE"
    ),
}

PAYMENT_SETTINGS = {  # noqa: F405
    **PAYMENT_SETTINGS,  # noqa: F405
    "PROVIDER": "razorpay",
    "KEY_ID": "rzp_test_notarealkey",
    "KEY_SECRET": "test-only-gateway-secret",
    "WEBHOOK_SECRET": "test-only-webhook-secret",
}
GOOGLE_OAUTH_CLIENT_ID = "test-client-id.apps.googleusercontent.com"

# Tests replace throttle rates explicitly where they need them.
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {  # noqa: F405
    "anon": "1000/minute",
    "user": "1000/minute",
    "auth": "1000/minute",
    "ai": "1000/minute",
    "payments": "1000/minute",
}

LOGGING["root"]["level"] = "WARNING"  # noqa: F405
LOGGING["loggers"]["django"]["level"] = "ERROR"  # noqa: F405
