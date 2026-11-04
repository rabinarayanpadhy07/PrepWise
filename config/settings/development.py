"""Local development settings. Never use these in production."""

from .base import *  # noqa: F403
from .base import env, env_list

DEBUG = env.bool("DJANGO_DEBUG", default=True)

# A throwaway key so the project runs without configuration. Production refuses it.
SECRET_KEY = env.str(
    "DJANGO_SECRET_KEY",
    default="django-insecure-local-development-key-do-not-use-in-production",
)
SIMPLE_JWT["SIGNING_KEY"] = env.str("JWT_SIGNING_KEY", default="") or SECRET_KEY  # noqa: F405

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
# Vite dev server (5173) and `npm run preview` (4173).
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    default=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
)
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", default=CORS_ALLOWED_ORIGINS)

API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=True)

# Local development runs over plain HTTP, so auth cookies cannot be Secure.
AUTH_COOKIE["SECURE"] = env.bool("AUTH_COOKIE_SECURE", default=False)  # noqa: F405
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE = AUTH_COOKIE["SECURE"]  # noqa: F405

# Browsable API is convenient locally; production returns JSON only.
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [  # noqa: F405
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
]
