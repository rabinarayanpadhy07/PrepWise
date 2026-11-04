"""
The OpenAI adapter, exercised through the official SDK against a mocked HTTP
transport. No network calls are made; model IDs and keys here are placeholders.
"""

import json

import httpx
import pytest

from apps.ai_services.errors import (
    AICredentialsRejected,
    AIModelUnavailable,
    AIProviderUnavailable,
    AIRateLimited,
    AIRequestRejected,
)
from apps.ai_services.providers import (
    FINISH_COMPLETE,
    FINISH_MAX_TOKENS,
    FINISH_OTHER,
    FINISH_REFUSAL,
    OpenAIProvider,
    build_provider,
)

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}},
    "required": ["ok", "note"],
    "additionalProperties": False,
}
TEST_KEY = "sk-test-openai-key-not-real-0000000000"
SECRET_IN_ERROR = "provider echoed sk-secret-value"


def generate(provider):
    return provider.generate_json(
        system="system prompt", prompt="user prompt", schema=SCHEMA, max_output_tokens=2000
    )


def openai_response(
    text='{"ok": true, "note": "x"}', status="completed", reason=None, refusal=False
):
    part = (
        {"type": "refusal", "refusal": "I can't help with that."}
        if refusal
        else {"type": "output_text", "text": text, "annotations": []}
    )
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1_700_000_000,
        "model": "test-openai-model-2026",
        "status": status,
        "incomplete_details": {"reason": reason} if reason else None,
        "output": [
            {
                "type": "message",
                "id": "msg_test",
                "status": "completed",
                "role": "assistant",
                "content": [part],
            }
        ],
        "usage": {
            "input_tokens": 90,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 45,
            "output_tokens_details": {"reasoning_tokens": 15},
            "total_tokens": 135,
        },
    }


def openai_with(handler):
    return OpenAIProvider(
        api_key=TEST_KEY,
        model="test-openai-model",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_openai_sends_strict_json_schema_request_without_storing_it():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=openai_response())

    response = generate(openai_with(handler))

    body = seen["body"]
    assert seen["path"].endswith("/responses")
    assert seen["auth"] == f"Bearer {TEST_KEY}"
    assert body["model"] == "test-openai-model"
    assert body["instructions"] == "system prompt"
    assert body["input"] == "user prompt"
    assert body["max_output_tokens"] == 2000
    assert body["store"] is False
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "prepwise_output",
        "schema": SCHEMA,
        "strict": True,
    }

    assert response.text == '{"ok": true, "note": "x"}'
    assert response.finish == FINISH_COMPLETE
    assert response.input_tokens == 90
    assert response.output_tokens == 45  # includes reasoning tokens
    assert response.provider == "openai"
    assert response.model == "test-openai-model-2026"


@pytest.mark.parametrize(
    "kwargs, finish",
    [
        ({"status": "incomplete", "reason": "max_output_tokens"}, FINISH_MAX_TOKENS),
        ({"status": "incomplete", "reason": "content_filter"}, FINISH_REFUSAL),
        ({"refusal": True}, FINISH_REFUSAL),
        ({"status": "failed"}, FINISH_OTHER),
    ],
)
def test_openai_normalises_finish(kwargs, finish):
    def handler(request):
        return httpx.Response(200, json=openai_response(**kwargs))

    assert generate(openai_with(handler)).finish == finish


def _error(message="x", code=None):
    return {"error": {"message": message, "type": "error", "code": code, "param": None}}


@pytest.mark.parametrize(
    "status, body, error",
    [
        (400, _error(SECRET_IN_ERROR), AIRequestRejected),
        (401, _error("Incorrect API key provided", "invalid_api_key"), AICredentialsRejected),
        (403, _error(), AIRequestRejected),
        (404, _error("The model does not exist", "model_not_found"), AIModelUnavailable),
        (429, _error("Rate limit reached"), AIRateLimited),
        # No credit left on the key's account: the key rests and the next is tried.
        (429, _error("You exceeded your current quota", "insufficient_quota"), AIRateLimited),
        (500, _error(), AIProviderUnavailable),
        (503, _error(), AIProviderUnavailable),
    ],
)
def test_openai_maps_http_errors_without_leaking_messages(status, body, error):
    def handler(request):
        return httpx.Response(status, json=body)

    with pytest.raises(error) as excinfo:
        generate(openai_with(handler))
    assert "sk-secret" not in str(excinfo.value.detail)


def test_openai_network_failure_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(AIProviderUnavailable):
        generate(openai_with(handler))


def test_openai_verify_key_lists_models_and_check_model_retrieves_it():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": []})
        return httpx.Response(
            200,
            json={"id": "test-openai-model", "object": "model", "created": 0, "owned_by": "x"},
        )

    provider = openai_with(handler)
    provider.verify_key()
    provider.check_model()
    assert paths[0].endswith("/models")
    assert paths[1].endswith("/models/test-openai-model")


def test_openai_verify_key_reports_a_rejected_key():
    def handler(request):
        return httpx.Response(401, json=_error("Incorrect API key provided", "invalid_api_key"))

    with pytest.raises(AICredentialsRejected):
        openai_with(handler).verify_key()


@pytest.mark.parametrize("provider", ["unknown", "gemini", "anthropic"])
def test_build_provider_supports_openai_only(provider):
    with pytest.raises(ValueError):
        build_provider(provider, api_key="x", model="y", timeout=5)
    assert isinstance(build_provider("openai", api_key="x", model="y", timeout=5), OpenAIProvider)
