"""
The OpenAI adapter. It turns one prompt into one JSON response and reports
OpenAI's token counts. The API key is passed in by the service layer; feature
code never sees it.

The adapter uses OpenAI's official SDK (Responses API, store=False) with an
explicit timeout and one SDK-level retry for transient failures. SDK exceptions
are translated into service errors whose messages never include provider
responses or prompts.
"""

import logging
import re
from dataclasses import dataclass

import openai

from apps.ai_services.errors import (
    AICredentialsRejected,
    AIModelUnavailable,
    AIProviderUnavailable,
    AIRateLimited,
    AIRequestRejected,
)
from apps.ai_services.models import Provider

OPENAI = Provider.OPENAI.value

logger = logging.getLogger("prepwise.ai")
_CODE = re.compile(r"^[a-z0-9_.\-]{1,60}$")


def _safe_code(value):
    """An OpenAI error code or type if it looks like an identifier, else "-"."""
    return value if isinstance(value, str) and _CODE.match(value) else "-"


PROVIDER_LABELS = {choice.value: choice.label for choice in Provider}

# Normalised reasons a response ended.
FINISH_COMPLETE = "complete"
FINISH_MAX_TOKENS = "max_tokens"
FINISH_REFUSAL = "refusal"
FINISH_OTHER = "other"

SDK_MAX_RETRIES = 1


@dataclass(frozen=True)
class ProviderResponse:
    """Raw outcome of one provider call, before the service layer interprets it."""

    text: str
    finish: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens


class AIProvider:
    name = ""

    def __init__(
        self, *, api_key, model="", timeout=120, http_client=None, max_retries=SDK_MAX_RETRIES
    ):
        self.model = model
        self.timeout = timeout

    def generate_json(self, *, system, prompt, schema, max_output_tokens):
        """Return a ProviderResponse whose text should be JSON matching `schema`."""
        raise NotImplementedError

    def verify_key(self):
        """Check that the API key is accepted. Needs no model ID. Raises AIServiceError."""
        raise NotImplementedError

    def check_model(self):
        """Check that the key can see `self.model`. Raises AIServiceError."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenAI (openai SDK, Responses API)
# ---------------------------------------------------------------------------


class OpenAIProvider(AIProvider):
    name = OPENAI

    def __init__(
        self, *, api_key, model="", timeout=120, http_client=None, max_retries=SDK_MAX_RETRIES
    ):
        super().__init__(api_key=api_key, model=model, timeout=timeout)
        options = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
        if http_client is not None:
            options["http_client"] = http_client
        self.client = openai.OpenAI(**options)

    def generate_json(self, *, system, prompt, schema, max_output_tokens):
        request = {
            "model": self.model,
            "instructions": system,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "prepwise_output",
                    "schema": schema,
                    "strict": True,
                }
            },
        }
        with _translate(_openai_error):
            response = self.client.responses.create(**request)

        usage = response.usage
        return ProviderResponse(
            text=response.output_text or "",
            finish=_openai_finish(response),
            input_tokens=(usage.input_tokens if usage else 0) or 0,
            output_tokens=(usage.output_tokens if usage else 0) or 0,
            provider=self.name,
            model=response.model or self.model,
        )

    def verify_key(self):
        with _translate(_openai_error):
            self.client.models.list()

    def check_model(self):
        with _translate(_openai_error):
            self.client.models.retrieve(self.model)


def _openai_finish(response):
    for item in response.output or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") == "refusal":
                return FINISH_REFUSAL
    if response.status == "completed":
        return FINISH_COMPLETE
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        if reason == "max_output_tokens":
            return FINISH_MAX_TOKENS
        if reason == "content_filter":
            return FINISH_REFUSAL
    return FINISH_OTHER


def _openai_error(exc):
    if isinstance(exc, openai.APIStatusError):
        # OpenAI's error code (e.g. "invalid_api_key", "insufficient_quota") is a fixed
        # identifier, safe to log. The message is not logged: it can echo the key.
        logger.warning(
            "OpenAI returned HTTP %s (code: %s, type: %s)",
            exc.status_code,
            _safe_code(getattr(exc, "code", None)),
            _safe_code(getattr(exc, "type", None)),
        )
    if isinstance(exc, openai.AuthenticationError):
        return AICredentialsRejected()
    if isinstance(exc, openai.NotFoundError):
        return AIModelUnavailable()
    if isinstance(exc, openai.RateLimitError):
        # Both "too many requests" and "insufficient_quota" (no credit left on the
        # key's account): the key rests and the next key is tried.
        return AIRateLimited()
    if isinstance(
        exc,
        (
            openai.BadRequestError,
            openai.PermissionDeniedError,
            openai.UnprocessableEntityError,
            openai.ConflictError,
        ),
    ):
        return AIRequestRejected()
    if isinstance(exc, (openai.APIConnectionError, openai.APIStatusError)):
        return AIProviderUnavailable()
    return None


# ---------------------------------------------------------------------------


class _translate:
    """Map SDK exceptions to service errors without leaking provider messages."""

    def __init__(self, mapper):
        self.mapper = mapper

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        translated = self.mapper(exc)
        if translated is None:
            return False
        raise translated from exc


ADAPTERS = {OPENAI: OpenAIProvider}


def build_provider(provider, *, api_key, model, timeout, http_client=None):
    try:
        adapter = ADAPTERS[provider]
    except KeyError:
        raise ValueError(f"Unsupported AI provider: {provider!r}") from None
    return adapter(api_key=api_key, model=model, timeout=timeout, http_client=http_client)
