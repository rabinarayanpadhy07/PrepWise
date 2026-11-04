"""AES-256-GCM encryption of the platform's OpenAI keys, key rotation, and failures."""

import logging
from io import StringIO

import pytest
from django.core.management import call_command

from apps.ai_services.crypto import (
    decrypt_api_key,
    encrypt_api_key,
    generate_key,
    key_id_of,
    needs_rotation,
)
from apps.ai_services.errors import KeyEncryptionUnavailable, StoredKeyUnreadable
from apps.ai_services.keyring import KeyConfigError, b64decode, parse_keys
from apps.ai_services.models import PlatformAPIKey

SECRET = "sk-test-secret-value-not-real-00000000"


def test_round_trip_uses_the_active_key_and_a_fresh_nonce():
    first = encrypt_api_key(SECRET)
    second = encrypt_api_key(SECRET)

    assert first != second  # random nonce per encryption
    assert SECRET not in first
    assert key_id_of(first) == "test2"
    assert decrypt_api_key(first) == SECRET


def test_tampered_ciphertext_is_rejected(caplog):
    token = encrypt_api_key(SECRET)
    version, key_id, nonce, data = token.split(".")
    tampered = ".".join([version, key_id, nonce, ("A" if data[0] != "A" else "B") + data[1:]])

    with pytest.raises(StoredKeyUnreadable):
        decrypt_api_key(tampered)
    assert SECRET not in caplog.text


@pytest.mark.parametrize("token", ["", "garbage", "v0.test2.a.b", "v1.unknown.a.b"])
def test_malformed_tokens_are_unreadable(token):
    with pytest.raises(StoredKeyUnreadable):
        decrypt_api_key(token)


def test_missing_configuration_is_reported(settings):
    settings.AI_SETTINGS = {**settings.AI_SETTINGS, "KEY_ENCRYPTION_KEYS": ""}

    with pytest.raises(KeyEncryptionUnavailable):
        encrypt_api_key(SECRET)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "No encryption keys"),
        ("no-separator", "must look like"),
        ("bad id!:" + "A" * 43, "must look like"),
        ("k1:" + "A" * 43 + ",k1:" + "B" * 43, "listed twice"),
        ("k1:QUJD", "32 bytes"),
    ],
)
def test_keyring_parsing_errors(raw, message):
    with pytest.raises(KeyConfigError, match=message) as info:
        parse_keys(raw)
    assert "A" * 43 not in str(info.value)


def test_generated_keys_are_256_bit():
    assert len(b64decode(generate_key())) == 32


@pytest.mark.django_db
def test_rotation_reencrypts_with_the_new_active_key(settings):
    old_ring = settings.AI_SETTINGS["KEY_ENCRYPTION_KEYS"]
    # Encrypt with the older key (make it active temporarily).
    test1_first = ",".join(reversed(old_ring.split(",")))
    settings.AI_SETTINGS = {**settings.AI_SETTINGS, "KEY_ENCRYPTION_KEYS": test1_first}
    stored = PlatformAPIKey.objects.create(
        encrypted_key=encrypt_api_key(SECRET), key_hint=SECRET[-4:]
    )
    assert key_id_of(stored.encrypted_key) == "test1"

    settings.AI_SETTINGS = {**settings.AI_SETTINGS, "KEY_ENCRYPTION_KEYS": old_ring}
    assert needs_rotation(stored.encrypted_key)
    dry = StringIO()
    call_command("rotate_encryption_keys", "--dry-run", stdout=dry)
    assert "Would re-encrypt 1 key(s)" in dry.getvalue()
    stored.refresh_from_db()
    assert key_id_of(stored.encrypted_key) == "test1"

    out = StringIO()
    call_command("rotate_encryption_keys", stdout=out)
    assert "Re-encrypted 1 key(s)" in out.getvalue()
    assert SECRET not in out.getvalue()

    stored.refresh_from_db()
    assert key_id_of(stored.encrypted_key) == "test2"
    assert not needs_rotation(stored.encrypted_key)
    assert decrypt_api_key(stored.encrypted_key) == SECRET


def test_generate_encryption_key_command_prints_a_usable_entry():
    out = StringIO()
    call_command("generate_encryption_key", "--id", "2026b", stdout=out)

    ring = parse_keys(out.getvalue().strip())
    assert ring.active_id == "2026b"


def test_errors_never_contain_the_secret(caplog, settings):
    caplog.set_level(logging.DEBUG)
    token = encrypt_api_key(SECRET)
    # Only the other test key is configured: the stored key can't be read.
    only_test1 = [k for k in settings.AI_SETTINGS["KEY_ENCRYPTION_KEYS"].split(",") if "test1" in k]
    settings.AI_SETTINGS = {**settings.AI_SETTINGS, "KEY_ENCRYPTION_KEYS": only_test1[0]}
    with pytest.raises(StoredKeyUnreadable) as info:
        decrypt_api_key(token)

    assert SECRET not in str(info.value.detail)
    assert SECRET not in caplog.text
