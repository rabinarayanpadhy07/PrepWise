"""
Logging helpers: request-ID context and secret redaction.

RedactSecretsFilter runs on every handler and scrubs:
  * values of sensitive keys (password=..., "api_key": "...", Authorization: ...),
  * bearer tokens and well-known API key formats,
  * credentials embedded in URLs (scheme://user:password@host),
  * the literal values of secrets configured in settings.
"""

import contextvars
import json
import logging
import re
from datetime import UTC, datetime

REDACTED = "[REDACTED]"

request_id_var = contextvars.ContextVar("request_id", default="-")

SENSITIVE_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "cookie",
    "sessionid",
    "csrftoken",
    "signing_key",
    "private_key",
    "key_secret",
    "encryption_key",
)

_KEY_PATTERN = "|".join(re.escape(key) for key in SENSITIVE_KEYS)

_PATTERNS = [
    # "key": "value"  /  'key': 'value'
    re.compile(
        rf"""(?P<prefix>["'][\w-]*(?:{_KEY_PATTERN})[\w-]*["']\s*:\s*)(?P<quote>["'])(?P<value>.*?)(?P=quote)""",
        re.IGNORECASE,
    ),
    # key=value  /  key: value  (headers, query strings, kwargs)
    re.compile(
        rf"""(?P<prefix>\b[\w-]*(?:{_KEY_PATTERN})[\w-]*\s*[=:]\s*)(?P<value>(?:Bearer\s+)?[^\s,;&'"}}\]]+)""",
        re.IGNORECASE,
    ),
    # Bearer tokens anywhere
    re.compile(r"(?P<prefix>\bBearer\s+)(?P<value>[A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),
    # Credentials in URLs: scheme://user:password@host
    re.compile(
        r"(?P<prefix>[a-z][a-z0-9+.-]*://[^\s:/@]+:)(?P<value>[^\s@/]+)(?=@)", re.IGNORECASE
    ),
]

# Common secret key shapes: OpenAI "sk-..." style keys, Google "AIza...",
# and payment gateway live/test keys (rzp_/sk_/pk_/rk_).
_TOKEN_SHAPES = re.compile(
    r"\b(?:"
    r"sk-(?:ant-)?[A-Za-z0-9_-]{16,}"
    r"|AIza[0-9A-Za-z_-]{30,}"
    r"|(?:rzp|sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"
    r")\b"
)

# Secrets shorter than this are not matched literally, to avoid redacting common words.
_MIN_LITERAL_SECRET_LENGTH = 8


def _encryption_key_values(raw):
    """The key material from "id:key,id:key" (ids are not secret)."""
    return [entry.partition(":")[2].strip() for entry in raw.split(",") if entry.strip()]


def _configured_secret_values():
    """Literal secret values from settings; empty if settings are not ready."""
    try:
        from django.conf import settings

        values = [
            getattr(settings, "SECRET_KEY", ""),
            settings.SIMPLE_JWT.get("SIGNING_KEY", ""),
            settings.DATABASES["default"].get("PASSWORD", ""),
            *_encryption_key_values(settings.AI_SETTINGS.get("KEY_ENCRYPTION_KEYS", "")),
            settings.PAYMENT_SETTINGS.get("KEY_SECRET", ""),
            settings.PAYMENT_SETTINGS.get("WEBHOOK_SECRET", ""),
            getattr(settings, "EMAIL_HOST_PASSWORD", ""),
        ]
    except Exception:  # noqa: BLE001 - logging must never fail because of settings
        return []
    return sorted(
        {
            value
            for value in values
            if isinstance(value, str) and len(value) >= _MIN_LITERAL_SECRET_LENGTH
        },
        key=len,
        reverse=True,
    )


def redact(text):
    """Return `text` with secrets replaced by [REDACTED]."""
    if not text:
        return text
    for secret in _configured_secret_values():
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: _replace_value(m), text)
    return _TOKEN_SHAPES.sub(REDACTED, text)


def _replace_value(match):
    groups = match.groupdict()
    quote = groups.get("quote") or ""
    return f"{groups['prefix']}{quote}{REDACTED}{quote}"


class RedactSecretsFilter(logging.Filter):
    """Redacts the rendered message and any traceback before a handler emits it."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not drop the record
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None

        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line, for log aggregation in production."""

    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        elif record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)
