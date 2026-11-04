import logging

import pytest
from django.conf import settings

from common.logging import REDACTED, JSONFormatter, RedactSecretsFilter, redact


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        ("login failed password=hunter2", "hunter2"),
        ('{"api_key": "abc123xyz"}', "abc123xyz"),
        ("{'refresh_token': 'eyJhbGciOi.payload.sig'}", "eyJhbGciOi.payload.sig"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig", "eyJhbGciOiJIUzI1NiJ9"),
        ("calling provider with sk-ant-REDACTMEabcdefghijklmnop", "REDACTMEabcdefghijklmnop"),
        ("key AIzaSyA1234567890abcdefghijklmnopqrstu", "AIzaSyA1234567890"),
        ("payment key rzp_live_ABCDEFGH12345678", "rzp_live_ABCDEFGH12345678"),
        ("postgres://prepwise:s3cr3tpass@db.example.com:5432/prepwise", "s3cr3tpass"),
        ("PAYMENT_WEBHOOK_SECRET=whsec_value_here", "whsec_value_here"),
    ],
)
def test_redact_removes_secrets(raw, leaked):
    result = redact(raw)

    assert leaked not in result
    assert REDACTED in result


def test_redact_keeps_ordinary_text():
    message = "User 42 started an interview session for role Backend Engineer"
    assert redact(message) == message


def test_redact_removes_configured_secret_values():
    message = f"unexpected value {settings.SECRET_KEY} in payload"

    assert settings.SECRET_KEY not in redact(message)


def _record(msg, args=(), exc_info=None):
    return logging.LogRecord("prepwise.test", logging.ERROR, __file__, 1, msg, args, exc_info)


def test_filter_redacts_formatted_args():
    record = _record("request headers: %s", ({"Authorization": "Bearer abc.def.ghi"},))

    RedactSecretsFilter().filter(record)

    assert "abc.def.ghi" not in record.getMessage()


def test_filter_redacts_tracebacks():
    try:
        raise ValueError("token=very-secret-token-value")
    except ValueError:
        import sys

        record = _record("failure", exc_info=sys.exc_info())

    RedactSecretsFilter().filter(record)

    assert "very-secret-token-value" not in record.exc_text
    assert "ValueError" in record.exc_text


def test_json_formatter_output_is_redacted():
    record = _record("password=%s", ("hunter2",))
    record.request_id = "req-1"
    RedactSecretsFilter().filter(record)

    output = JSONFormatter().format(record)

    assert "hunter2" not in output
    assert '"request_id": "req-1"' in output


def test_every_configured_handler_redacts():
    for name, handler in settings.LOGGING["handlers"].items():
        assert "redact_secrets" in handler.get("filters", []), name
