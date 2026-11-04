"""
Encryption at rest for the platform's API keys (entered in the admin panel):
AES-256-GCM.

Encryption keys live outside the database, in the AI_KEY_ENCRYPTION_KEYS secret:

    AI_KEY_ENCRYPTION_KEYS=2026b:<base64 32-byte key>,2026a:<base64 32-byte key>

The first entry encrypts; every entry can decrypt. Each stored value records
which key encrypted it:

    v1.<key id>.<base64url nonce>.<base64url ciphertext+tag>

To rotate: add a new key at the front, deploy, run
`manage.py rotate_encryption_keys`, then remove the old key once the command
reports nothing left to re-encrypt.
"""

import binascii
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

from apps.ai_services.errors import KeyEncryptionUnavailable, StoredKeyUnreadable
from apps.ai_services.keyring import KeyConfigError, b64decode, b64encode, parse_keys

logger = logging.getLogger("prepwise.ai")

FORMAT_VERSION = "v1"
NONCE_BYTES = 12
# Bound in as associated data, so a ciphertext can't be reused for another purpose.
ASSOCIATED_DATA = f"prepwise:platform-api-key:{FORMAT_VERSION}".encode()


def generate_key():
    return b64encode(AESGCM.generate_key(bit_length=256))


def load_keyring():
    try:
        return parse_keys(settings.AI_SETTINGS.get("KEY_ENCRYPTION_KEYS", ""))
    except KeyConfigError as exc:
        logger.error("API key encryption is unavailable: %s", exc)
        raise KeyEncryptionUnavailable() from exc


def encrypt_api_key(plaintext):
    ring = load_keyring()
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(ring.keys[ring.active_id]).encrypt(
        nonce, plaintext.encode("utf-8"), ASSOCIATED_DATA
    )
    return ".".join([FORMAT_VERSION, ring.active_id, b64encode(nonce), b64encode(ciphertext)])


def key_id_of(token):
    parts = (token or "").split(".")
    return parts[1] if len(parts) == 4 and parts[0] == FORMAT_VERSION else None


def decrypt_api_key(token):
    ring = load_keyring()
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != FORMAT_VERSION:
        raise StoredKeyUnreadable()
    _, key_id, nonce, ciphertext = parts
    key = ring.keys.get(key_id)
    if key is None:
        logger.error("A stored API key was encrypted with a key id that is no longer configured")
        raise StoredKeyUnreadable()
    try:
        plaintext = AESGCM(key).decrypt(b64decode(nonce), b64decode(ciphertext), ASSOCIATED_DATA)
    except (InvalidTag, binascii.Error, ValueError) as exc:
        logger.error("A stored API key failed authentication and can't be decrypted")
        raise StoredKeyUnreadable() from exc
    return plaintext.decode("utf-8")


def needs_rotation(token):
    try:
        return key_id_of(token) != load_keyring().active_id
    except KeyEncryptionUnavailable:
        return False
