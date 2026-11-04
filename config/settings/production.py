"""
Production settings.

Startup fails with ImproperlyConfigured when a required value is missing or
unsafe, so a misconfigured deployment never serves traffic.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env

INSECURE_KEY_PREFIX = "django-insecure"
MIN_SECRET_KEY_LENGTH = 50


def _require(condition, message):
    if not condition:
        raise ImproperlyConfigured(message)


if env.bool("DJANGO_DEBUG", default=False):
    raise ImproperlyConfigured("DJANGO_DEBUG must be false in production.")
DEBUG = False

_require(SECRET_KEY, "DJANGO_SECRET_KEY is required in production.")  # noqa: F405
_require(
    len(SECRET_KEY) >= MIN_SECRET_KEY_LENGTH  # noqa: F405
    and not SECRET_KEY.startswith(INSECURE_KEY_PREFIX),  # noqa: F405
    f"DJANGO_SECRET_KEY must be a random value of at least {MIN_SECRET_KEY_LENGTH} characters.",
)
_require(ALLOWED_HOSTS, "DJANGO_ALLOWED_HOSTS is required in production.")  # noqa: F405
_require("*" not in ALLOWED_HOSTS, "DJANGO_ALLOWED_HOSTS must not contain '*'.")  # noqa: F405
_require(
    all(origin.startswith("https://") for origin in CORS_ALLOWED_ORIGINS),  # noqa: F405
    "CORS_ALLOWED_ORIGINS must list explicit https:// origins in production.",
)
_require(
    DATABASES["default"]["PASSWORD"],  # noqa: F405
    "DB_PASSWORD is required in production.",
)
_require(AUTH_COOKIE["SECURE"], "AUTH_COOKIE_SECURE must be true in production.")  # noqa: F405


# The OpenAI keys saved in the admin panel are encrypted with these keys.
_require(
    AI_SETTINGS["KEY_ENCRYPTION_KEYS"],  # noqa: F405
    "AI_KEY_ENCRYPTION_KEYS is required in production (python manage.py generate_encryption_key).",
)
from apps.ai_services.keyring import KeyConfigError, parse_keys  # noqa: E402

try:
    parse_keys(AI_SETTINGS["KEY_ENCRYPTION_KEYS"])  # noqa: F405
except KeyConfigError as exc:
    raise ImproperlyConfigured(f"AI_KEY_ENCRYPTION_KEYS is invalid: {exc}") from None

# Payments are optional, but a configured gateway needs all of its secrets.
if PAYMENT_SETTINGS["PROVIDER"]:  # noqa: F405
    _require(
        PAYMENT_SETTINGS["PROVIDER"] == "razorpay",  # noqa: F405
        "PAYMENT_PROVIDER must be 'razorpay' (or empty to disable purchases).",
    )
    _require(
        all(
            PAYMENT_SETTINGS[name]  # noqa: F405
            for name in ("KEY_ID", "KEY_SECRET", "WEBHOOK_SECRET")
        ),
        "PAYMENT_KEY_ID, PAYMENT_KEY_SECRET and PAYMENT_WEBHOOK_SECRET are required "
        "when PAYMENT_PROVIDER is set.",
    )

DATABASES["default"]["OPTIONS"]["sslmode"] = env.str("DB_SSLMODE", default="require")  # noqa: F405

# HTTPS and cookies
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
# Health checks are often made over plain HTTP by the load balancer.
SECURE_REDIRECT_EXEMPT = [r"^api/v1/health/"]
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 30)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"

# Serve collected static files (API docs) from the app process.
MIDDLEWARE.insert(  # noqa: F405
    MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,  # noqa: F405
    "whitenoise.middleware.WhiteNoiseMiddleware",
)
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
}

LOGGING["handlers"]["console"]["formatter"] = env.str("DJANGO_LOG_FORMAT", default="json")  # noqa: F405
