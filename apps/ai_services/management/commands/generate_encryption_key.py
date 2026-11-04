import re

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.ai_services.crypto import generate_key
from apps.ai_services.keyring import KEY_ID_PATTERN


class Command(BaseCommand):
    help = (
        "Print a new AI_KEY_ENCRYPTION_KEYS entry (<id>:<base64 256-bit key>) for encrypting "
        "the OpenAI API keys saved in the admin panel. Nothing is saved."
    )

    def add_arguments(self, parser):
        parser.add_argument("--id", dest="key_id", help="Key id (default: today's date).")

    def handle(self, *args, key_id=None, **options):
        key_id = key_id or timezone.now().strftime("%Y%m%d")
        if not KEY_ID_PATTERN.match(key_id) or re.search(r"[,:]", key_id):
            raise CommandError("The id may contain letters, digits, '-' and '_' (max 32).")
        self.stdout.write(f"{key_id}:{generate_key()}")
