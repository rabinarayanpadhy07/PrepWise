"""
Settings shared by every environment.

Values come from environment variables (optionally loaded from backend/.env).
Environment-specific modules (development, production, test) import from here
and override only what differs.
"""

from datetime import timedelta
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

# Variables already set in the process environment take precedence over .env.
# Set DJANGO_READ_ENV_FILE=false to ignore the file entirely (e.g. in containers).
if env.bool("DJANGO_READ_ENV_FILE", default=True) and (BASE_DIR / ".env").is_file():
    environ.Env.read_env(BASE_DIR / ".env", overwrite=False)


def env_list(name, default=None):
    """Comma-separated list with whitespace and empty items removed."""
    raw = env.str(name, default="" if default is None else ",".join(default))
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")

# Render platform support: automatically allow the service's external hostname and .onrender.com domain
RENDER_EXTERNAL_HOSTNAME = env.str("RENDER_EXTERNAL_HOSTNAME", default="")
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)

if env.bool("RENDER", default=False) or RENDER_EXTERNAL_HOSTNAME:
    if ".onrender.com" not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(".onrender.com")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    # PrepWise
    "common",
    "apps.accounts",
    "apps.resumes",
    "apps.interviews",
    "apps.ai_services",
    "apps.usage",
    "apps.billing",
    "apps.payments",
    "apps.dashboard",
    "apps.administration",
]

MIDDLEWARE = [
    "common.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

AUTH_USER_MODEL = "accounts.User"

# --------------------------------------------------------------------------
# Database: PostgreSQL only. SQLite is not supported in any environment.
# --------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.str("DB_NAME", default="prepwise"),
        "USER": env.str("DB_USER", default="prepwise"),
        "PASSWORD": env.str("DB_PASSWORD", default=""),
        "HOST": env.str("DB_HOST", default="localhost"),
        "PORT": env.int("DB_PORT", default=5432),
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "sslmode": env.str("DB_SSLMODE", default="prefer"),
            "connect_timeout": env.int("DB_CONNECT_TIMEOUT", default=5),
            "application_name": "prepwise",
        },
        "TEST": {
            "NAME": env.str("DB_TEST_NAME", default="test_prepwise"),
        },
    }
}

# Allow connection string via DATABASE_URL or POSTGRES_URL (e.g. Neon, Supabase, Vercel Postgres)
if env.str("DATABASE_URL", default="") or env.str("POSTGRES_URL", default=""):
    _db_key = "DATABASE_URL" if env.str("DATABASE_URL", default="") else "POSTGRES_URL"
    DATABASES["default"].update(env.db_url(_db_key))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env.str("DJANGO_TIME_ZONE", default="Asia/Kolkata")
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------
# Static and uploaded files
# --------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = Path(env.str("DJANGO_MEDIA_ROOT", default=str(BASE_DIR / "media")))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Hard ceiling for non-file request bodies held in memory.
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("DJANGO_DATA_UPLOAD_MAX_MEMORY_SIZE", default=2_621_440)
# Uploads larger than this are streamed to a temporary file instead of memory.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2_621_440

# Resume uploads (PDF or DOCX). Checked by the resumes app before parsing.
RESUME_MAX_UPLOAD_BYTES = env.int("RESUME_MAX_UPLOAD_BYTES", default=5 * 1024 * 1024)

# There is no Django admin site. Administration happens in the PrepWise admin
# panel (frontend /admin, API /api/v1/admin/), which writes every change to the
# audit log.

# --------------------------------------------------------------------------
# CORS / CSRF: explicit allowlists only.
# --------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")
# Required for the HttpOnly refresh-token cookie when the frontend runs on another origin.
CORS_ALLOW_CREDENTIALS = env.bool("CORS_ALLOW_CREDENTIALS", default=True)
CORS_URLS_REGEX = r"^/api/.*$"
# Content-Disposition lets the app save invoices and reports under their own names.
CORS_EXPOSE_HEADERS = ["X-Request-ID", "Content-Disposition"]
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
if RENDER_EXTERNAL_HOSTNAME:
    _render_origin = f"https://{RENDER_EXTERNAL_HOSTNAME}"
    if _render_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_render_origin)
if env.bool("RENDER", default=False) or RENDER_EXTERNAL_HOSTNAME:
    if "https://*.onrender.com" not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append("https://*.onrender.com")

# --------------------------------------------------------------------------
# Authentication transport (see README "Authentication").
#   * Access token: short-lived JWT returned in the JSON body, kept in memory
#     by the SPA and sent as "Authorization: Bearer ...".
#   * Refresh token: HttpOnly cookie scoped to /api/v1/auth/, never readable
#     by JavaScript. Endpoints that read or set it require a CSRF token.
#   * CSRF token: fetched from /api/v1/auth/csrf/ and sent as X-CSRFToken.
# --------------------------------------------------------------------------
AUTH_COOKIE_SAMESITE_CHOICES = ("Lax", "Strict", "None")
AUTH_COOKIE = {
    "REFRESH_NAME": env.str("AUTH_REFRESH_COOKIE_NAME", default="prepwise_refresh"),
    "PATH": "/api/v1/auth/",
    "DOMAIN": env.str("AUTH_COOKIE_DOMAIN", default="") or None,
    "SECURE": env.bool("AUTH_COOKIE_SECURE", default=True),
    "SAMESITE": env.str("AUTH_COOKIE_SAMESITE", default="Lax"),
}
if AUTH_COOKIE["SAMESITE"] not in AUTH_COOKIE_SAMESITE_CHOICES:
    raise ImproperlyConfigured("AUTH_COOKIE_SAMESITE must be Lax, Strict, or None.")

# The CSRF token is delivered in a response body, so its cookie can be HttpOnly too.
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = AUTH_COOKIE["SECURE"]
CSRF_COOKIE_SAMESITE = AUTH_COOKIE["SAMESITE"]
CSRF_COOKIE_DOMAIN = AUTH_COOKIE["DOMAIN"]
SESSION_COOKIE_SECURE = AUTH_COOKIE["SECURE"]

# Sign in with Google: the OAuth client ID from Google Cloud Console. Empty disables it.
# It's public (the browser needs it too); there is no client secret in this flow.
GOOGLE_OAUTH_CLIENT_ID = env.str("GOOGLE_OAUTH_CLIENT_ID", default="")

# --------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=False)

REST_FRAMEWORK = {
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_PAGINATION_CLASS": "common.pagination.StandardPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env.str("API_THROTTLE_ANON", default="60/minute"),
        "user": env.str("API_THROTTLE_USER", default="240/minute"),
        # Scoped rates, applied with ScopedRateThrottle on specific views.
        "auth": env.str("API_THROTTLE_AUTH", default="10/minute"),
        "ai": env.str("API_THROTTLE_AI", default="20/minute"),
        "payments": env.str("API_THROTTLE_PAYMENTS", default="20/minute"),
    },
    "EXCEPTION_HANDLER": "common.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_TOKEN_MINUTES", default=15)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_TOKEN_DAYS", default=7)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    # Tokens carry a hash of the password hash, so changing or resetting the
    # password invalidates every access token issued before the change.
    "CHECK_REVOKE_TOKEN": True,
    "ALGORITHM": "HS256",
    # Falls back to SECRET_KEY when unset; set separately to rotate tokens independently.
    "SIGNING_KEY": env.str("JWT_SIGNING_KEY", default="") or SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "PrepWise API",
    "DESCRIPTION": "API for PrepWise, an AI interview preparation platform.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v[0-9]+",
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "AnalysisStatusEnum": "apps.resumes.models.ResumeAnalysis.Status",
        "InterviewStatusEnum": "apps.interviews.models.InterviewSession.Status",
        "ProviderEnum": "apps.ai_services.models.Provider",
        "PlatformKeyStatusEnum": "apps.ai_services.models.PlatformAPIKey.Status",
        "SubscriptionStatusEnum": "apps.billing.models.Subscription.Status",
        "OrderStatusEnum": "apps.payments.models.PaymentOrder.Status",
        "PaymentStatusEnum": "apps.payments.models.Payment.Status",
        "RefundStatusEnum": "apps.payments.models.Refund.Status",
        "AIModeKeyEnum": "apps.ai_services.models.AIMode.Key",
    },
}

# --------------------------------------------------------------------------
# Integrations. Secrets are read here and never exposed by any endpoint.
# --------------------------------------------------------------------------
AI_SETTINGS = {
    # OpenAI models, modes, prices, limits and the platform's OpenAI API keys are
    # all configured by administrators in the admin panel. Only the encryption
    # secret and transport settings live here.
    "REQUEST_TIMEOUT_SECONDS": env.int("AI_REQUEST_TIMEOUT_SECONDS", default=120),
    # AES-256-GCM keys that encrypt the API keys saved in the admin panel:
    # "id:base64key,id:base64key". The first encrypts; all decrypt (see
    # apps.ai_services.crypto for rotation).
    "KEY_ENCRYPTION_KEYS": env.str("AI_KEY_ENCRYPTION_KEYS", default=""),
}

PAYMENT_SETTINGS = {
    # Supported: "razorpay". Empty disables purchases (subscriptions can still be
    # granted by an administrator in the admin panel).
    "PROVIDER": env.str("PAYMENT_PROVIDER", default=""),
    "KEY_ID": env.str("PAYMENT_KEY_ID", default=""),
    "KEY_SECRET": env.str("PAYMENT_KEY_SECRET", default=""),
    "WEBHOOK_SECRET": env.str("PAYMENT_WEBHOOK_SECRET", default=""),
    "REQUEST_TIMEOUT_SECONDS": env.int("PAYMENT_REQUEST_TIMEOUT_SECONDS", default=20),
}

# Seller details printed on invoices. Tax is never calculated by PrepWise; set
# INVOICE_SELLER_TAX_ID only if you are registered, and confirm invoice
# requirements with your accountant.
INVOICE_SETTINGS = {
    "SELLER_NAME": env.str("INVOICE_SELLER_NAME", default="PrepWise"),
    "SELLER_ADDRESS": env.str("INVOICE_SELLER_ADDRESS", default=""),
    "SELLER_EMAIL": env.str("INVOICE_SELLER_EMAIL", default=""),
    "SELLER_TAX_ID": env.str("INVOICE_SELLER_TAX_ID", default=""),
    "NUMBER_PREFIX": env.str("INVOICE_NUMBER_PREFIX", default="PW"),
}

# Optional TrueType font for PDFs (invoices, reports) so any script renders.
# Without it, text outside Latin-1 is transliterated or shown as "?".
PDF_UNICODE_FONT_PATH = env.str("PDF_UNICODE_FONT_PATH", default="")

# --------------------------------------------------------------------------
# Logging. Every handler passes through the secret-redaction filter.
# --------------------------------------------------------------------------
LOG_LEVEL = env.str("DJANGO_LOG_LEVEL", default="INFO").upper()
LOG_FORMAT = env.str("DJANGO_LOG_FORMAT", default="text")  # "text" or "json"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_secrets": {"()": "common.logging.RedactSecretsFilter"},
        "request_id": {"()": "common.logging.RequestIDFilter"},
    },
    "formatters": {
        "text": {
            "format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
        },
        "json": {"()": "common.logging.JSONFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": LOG_FORMAT,
            "filters": ["request_id", "redact_secrets"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.db.backends": {"level": "WARNING"},
        "prepwise": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}
