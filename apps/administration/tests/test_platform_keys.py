"""
The platform's OpenAI API keys, managed in the admin panel: tested with OpenAI
before saving, stored encrypted, never returned, audited, rotated automatically
when one hits its limit, and re-enabled or reordered by an administrator.
"""

import pytest

from apps.administration.models import AuditLogEntry
from apps.ai_services import service
from apps.ai_services.crypto import decrypt_api_key
from apps.ai_services.errors import AICredentialsRejected, AIRateLimited
from apps.ai_services.models import PlatformAPIKey, ProviderCallLog
from conftest import LARGE_MODEL, PLATFORM_KEY, SECOND_PLATFORM_KEY

pytestmark = pytest.mark.django_db

KEYS = "/api/v1/admin/ai/keys/"
NEW_KEY = "sk-proj-TestOnlyOpenAIKeyNotReal0000000000WXYZ"


@pytest.fixture
def admin_client(client_for, admin_user):
    return client_for(admin_user)


def add(client, api_key=NEW_KEY, **extra):
    return client.post(KEYS, {"api_key": api_key, **extra}, format="json")


def test_adding_a_key_tests_it_encrypts_it_and_never_returns_it(admin_client, fake_ai):
    response = add(admin_client, label="Free key 1")

    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["key_hint"], body["label"], body["status"]) == ("WXYZ", "Free key 1", "active")
    assert NEW_KEY not in response.content.decode()
    # OpenAI was asked about this exact key before it was stored.
    assert fake_ai.built[-1]["api_key"] == NEW_KEY
    stored = PlatformAPIKey.objects.get()
    assert NEW_KEY not in stored.encrypted_key
    assert decrypt_api_key(stored.encrypted_key) == NEW_KEY
    assert service.has_usable_keys()

    listing = admin_client.get(KEYS)
    assert [row["key_hint"] for row in listing.json()] == ["WXYZ"]
    assert NEW_KEY not in listing.content.decode()


def test_new_keys_go_to_the_end_of_the_rotation(admin_client, fake_ai):
    add(admin_client, PLATFORM_KEY)
    add(admin_client, SECOND_PLATFORM_KEY)

    rows = admin_client.get(KEYS).json()
    assert [row["key_hint"] for row in rows] == [PLATFORM_KEY[-4:], SECOND_PLATFORM_KEY[-4:]]
    assert rows[0]["priority"] < rows[1]["priority"]


def test_adding_a_key_is_audited_without_the_key(admin_client, fake_ai):
    add(admin_client, reason="Second free project")

    entry = AuditLogEntry.objects.get(action="platform_key.create")
    assert entry.changes == {"key_hint": [None, "WXYZ"], "label": [None, ""]}
    assert entry.reason == "Second free project"
    assert NEW_KEY not in str(entry.changes)


def test_a_key_openai_rejects_is_not_saved(admin_client, fake_ai):
    fake_ai.verify_error = AICredentialsRejected()

    response = add(admin_client)

    assert response.status_code == 400
    assert "rejected" in response.content.decode()
    assert not PlatformAPIKey.objects.exists()


@pytest.mark.parametrize(
    "api_key",
    [
        "sk-TestOnlyLegacyKeyNotReal00000000abcdEFGH",
        "sk-proj-TestOnly_Project-KeyNotReal0000000abcdEFGH",
        "sk-svcacct-TestOnlyServiceAccountKeyNotReal00abcdEFGH",
    ],
)
def test_openai_key_formats_are_accepted(admin_client, fake_ai, api_key):
    response = add(admin_client, api_key)

    assert response.status_code == 201, response.json()
    assert fake_ai.built[-1]["api_key"] == api_key
    assert response.json()["key_hint"] == "EFGH"


def test_a_gemini_key_is_refused_with_a_clear_message(admin_client, fake_ai):
    response = add(admin_client, "AIzaSyTestOnlyGeminiKeyNotReal0000000000WXYZ")

    assert response.status_code == 400
    assert "sk-" in response.content.decode()
    assert fake_ai.built == []


def test_a_malformed_key_is_refused_before_calling_openai(admin_client, fake_ai):
    response = add(admin_client, "not a key!")

    assert response.status_code == 400
    assert fake_ai.built == []


def test_the_same_key_cannot_be_added_twice(admin_client, fake_ai):
    add(admin_client)

    response = add(admin_client)

    assert response.status_code == 409
    assert PlatformAPIKey.objects.count() == 1


def test_keys_can_be_disabled_reordered_and_relabelled(admin_client, platform_key):
    key = platform_key()

    response = admin_client.patch(
        f"{KEYS}{key.pk}/",
        {"is_enabled": False, "priority": 7, "label": " Backup "},
        format="json",
    )

    assert response.status_code == 200
    key.refresh_from_db()
    assert (key.is_enabled, key.priority, key.label) == (False, 7, "Backup")
    assert not service.has_usable_keys()
    [entry] = AuditLogEntry.objects.filter(action="platform_key.update")
    assert entry.changes["is_enabled"] == [True, False]


def test_a_rejected_key_can_be_reactivated(admin_client, platform_key):
    key = platform_key(status=PlatformAPIKey.Status.REJECTED)
    key.cooldowns = {LARGE_MODEL: key.next_cooldown(LARGE_MODEL)}
    key.save()

    body = admin_client.patch(f"{KEYS}{key.pk}/", {"reactivate": True}, format="json").json()

    assert (body["status"], body["cooling_down"]) == ("active", [])
    assert service.has_usable_keys()


def test_cooldowns_are_listed_and_can_be_cleared(admin_client, platform_key):
    key = platform_key()
    key.cooldowns = {LARGE_MODEL: key.next_cooldown(LARGE_MODEL)}
    key.save()

    [row] = admin_client.get(KEYS).json()
    assert [entry["model"] for entry in row["cooling_down"]] == [LARGE_MODEL]

    body = admin_client.patch(f"{KEYS}{key.pk}/", {"clear_cooldowns": True}, format="json").json()
    assert body["cooling_down"] == []


def test_removing_a_key(admin_client, platform_key):
    key = platform_key()

    response = admin_client.delete(f"{KEYS}{key.pk}/")

    assert response.status_code == 204
    assert not PlatformAPIKey.objects.exists()
    assert AuditLogEntry.objects.filter(action="platform_key.delete").exists()
    assert admin_client.delete(f"{KEYS}{key.pk}/").status_code == 404


def test_testing_a_saved_key_updates_its_status(admin_client, platform_key, fake_ai):
    key = platform_key()

    ok = admin_client.post(f"{KEYS}{key.pk}/test/")
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert fake_ai.built[-1]["api_key"] == PLATFORM_KEY
    assert PLATFORM_KEY not in ok.content.decode()

    fake_ai.verify_error = AIRateLimited()
    limited = admin_client.post(f"{KEYS}{key.pk}/test/").json()
    assert (limited["ok"], limited["status"], limited["last_error"]) == (
        False,
        "active",  # over the limit isn't broken
        "ai_rate_limited",
    )

    fake_ai.verify_error = AICredentialsRejected()
    rejected = admin_client.post(f"{KEYS}{key.pk}/test/").json()
    assert (rejected["ok"], rejected["status"]) == (False, "rejected")

    fake_ai.verify_error = None
    fixed = admin_client.post(f"{KEYS}{key.pk}/test/").json()
    assert (fixed["ok"], fixed["status"], fixed["last_error"]) == (True, "active", "")
    assert ProviderCallLog.objects.filter(feature="key_check", api_key=key).count() == 4
    assert AuditLogEntry.objects.filter(action="platform_key.test").count() == 4


def test_requests_rotate_to_the_next_key_when_one_is_limited(
    admin_client, user, subscribe, ai_config, platform_key, fake_ai
):
    """End to end: a key that hits OpenAI's limit shows as cooling down in the panel."""
    platform_key(SECOND_PLATFORM_KEY)
    subscribe(user)
    fake_ai.fail(AIRateLimited())
    fake_ai.respond({"answer": "ok"})

    from rest_framework import serializers

    class Output(serializers.Serializer):
        answer = serializers.CharField()

    service.run_json_task(
        user,
        feature="test",
        system="s",
        prompt="p",
        schema={"type": "object"},
        max_output_tokens=100,
        serializer_class=Output,
    )

    rows = admin_client.get(KEYS).json()
    assert [row["cooling_down"] != [] for row in rows] == [True, False]
    assert [row["request_count"] for row in rows] == [0, 1]


def test_only_administrators_can_manage_keys(client_for, user, platform_key, fake_ai):
    client = client_for(user)
    key = platform_key()

    assert client.get(KEYS).status_code == 403
    assert add(client).status_code == 403
    assert client.patch(f"{KEYS}{key.pk}/", {"is_enabled": False}, format="json").status_code == 403
    assert client.post(f"{KEYS}{key.pk}/test/").status_code == 403
    assert client.delete(f"{KEYS}{key.pk}/").status_code == 403
    assert fake_ai.built == []
