"""
Parsing for AI_KEY_ENCRYPTION_KEYS ("id:base64key,id:base64key").

Kept free of Django and DRF imports so production settings can validate the
value at startup. Error messages never contain key material.
"""

import base64
import binascii
import re
from dataclasses import dataclass

KEY_BYTES = 32
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class KeyConfigError(ValueError):
    """AI_KEY_ENCRYPTION_KEYS is malformed."""


@dataclass(frozen=True)
class KeyRing:
    active_id: str
    keys: dict


def b64decode(value):
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))


def b64encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def parse_keys(raw):
    """Parse "id:key,id:key" into a KeyRing whose first key is active."""
    entries = [entry.strip() for entry in (raw or "").split(",") if entry.strip()]
    if not entries:
        raise KeyConfigError("No encryption keys are configured.")
    keys = {}
    for position, entry in enumerate(entries, start=1):
        key_id, sep, encoded = entry.partition(":")
        if not sep or not KEY_ID_PATTERN.match(key_id):
            raise KeyConfigError(f"Entry {position} must look like <id>:<base64 key>.")
        if key_id in keys:
            raise KeyConfigError(f"Key id {key_id!r} is listed twice.")
        try:
            key = b64decode(encoded.strip())
        except (binascii.Error, ValueError) as exc:
            raise KeyConfigError(f"Key {key_id!r} is not valid base64.") from exc
        if len(key) != KEY_BYTES:
            raise KeyConfigError(f"Key {key_id!r} must decode to {KEY_BYTES} bytes.")
        keys[key_id] = key
    return KeyRing(active_id=next(iter(keys)), keys=keys)
