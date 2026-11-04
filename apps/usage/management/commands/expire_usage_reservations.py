from django.core.management.base import BaseCommand

from apps.usage.services import expire_stale_reservations


class Command(BaseCommand):
    help = (
        "Mark quota reservations past their expiry as expired. Limits already ignore "
        "them; this keeps the table tidy. Safe to run on a schedule."
    )

    def handle(self, *args, **options):
        count = expire_stale_reservations()
        self.stdout.write(f"Expired {count} reservation(s).")
