"""
Give an existing account administrator access to the PrepWise admin panel.

    python manage.py promote_admin person@example.com

Use it for the first administrator (for example an account created with
Google sign-in, which has no password for createsuperuser). After that,
administrators manage roles in the admin panel, where changes are audited.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.administration.models import AuditLogEntry

# Shown as the actor in the audit log: no signed-in administrator ran this.
SYSTEM_ACTOR = "system@localhost"


class Command(BaseCommand):
    help = "Give an existing account administrator access to the admin panel."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email address of the existing account.")

    def handle(self, *args, email, **options):
        user_model = get_user_model()
        user = user_model.objects.filter(email__iexact=email.strip()).first()
        if user is None:
            raise CommandError(f"No account uses {email!r}. Sign up first, then run this again.")
        if not user.is_active:
            raise CommandError(f"{user.email} is deactivated. Reactivate the account first.")
        if user.role == user_model.Role.ADMIN:
            self.stdout.write(f"{user.email} is already an administrator.")
            return
        user.role = user_model.Role.ADMIN
        user.save(update_fields=["role"])
        AuditLogEntry.objects.create(
            actor=None,
            actor_email=SYSTEM_ACTOR,
            action="user.update",
            target_type=user._meta.label_lower,
            target_id=str(user.pk),
            target_label=str(user)[:200],
            changes={"role": ["user", "admin"]},
            reason="promote_admin management command",
        )
        self.stdout.write(self.style.SUCCESS(f"{user.email} is now an administrator."))
