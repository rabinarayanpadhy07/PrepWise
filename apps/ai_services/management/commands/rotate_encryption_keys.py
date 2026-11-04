from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.ai_services.crypto import decrypt_api_key, encrypt_api_key, key_id_of, load_keyring
from apps.ai_services.errors import KeyEncryptionUnavailable, StoredKeyUnreadable
from apps.ai_services.models import PlatformAPIKey


class Command(BaseCommand):
    help = (
        "Re-encrypt the OpenAI API keys saved in the admin panel with the active (first) key "
        "in AI_KEY_ENCRYPTION_KEYS. Run after adding a new encryption key; remove old keys "
        "only when nothing is left to re-encrypt."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would change without saving."
        )

    def handle(self, *args, dry_run=False, **options):
        try:
            ring = load_keyring()
        except KeyEncryptionUnavailable as exc:
            raise CommandError("AI_KEY_ENCRYPTION_KEYS is missing or invalid.") from exc

        rotated = current = unreadable = 0
        for pk in PlatformAPIKey.objects.values_list("pk", flat=True).iterator():
            with transaction.atomic():
                stored = PlatformAPIKey.objects.select_for_update().get(pk=pk)
                if key_id_of(stored.encrypted_key) == ring.active_id:
                    current += 1
                    continue
                try:
                    plaintext = decrypt_api_key(stored.encrypted_key)
                except StoredKeyUnreadable:
                    unreadable += 1
                    continue
                if not dry_run:
                    stored.encrypted_key = encrypt_api_key(plaintext)
                    stored.save(update_fields=["encrypted_key"])
                rotated += 1

        verb = "Would re-encrypt" if dry_run else "Re-encrypted"
        self.stdout.write(
            f"{verb} {rotated} key(s); {current} already use key {ring.active_id!r}; "
            f"{unreadable} can't be decrypted with the configured keys."
        )
        if unreadable:
            self.stdout.write(
                "Keys that can't be decrypted must be added again in the admin panel. "
                "Don't remove an old encryption key while it may still be needed."
            )
