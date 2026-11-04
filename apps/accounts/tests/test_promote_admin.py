import pytest
from django.core.management import CommandError, call_command

from apps.administration.models import AuditLogEntry

pytestmark = pytest.mark.django_db


def test_promotes_an_existing_user_and_audits_it(user):
    call_command("promote_admin", user.email.upper())

    user.refresh_from_db()
    assert user.role == "admin"
    entry = AuditLogEntry.objects.get()
    assert entry.action == "user.update"
    assert entry.target_id == str(user.pk)
    assert entry.changes == {"role": ["user", "admin"]}


def test_refuses_an_unknown_email(db):
    with pytest.raises(CommandError, match="No account"):
        call_command("promote_admin", "nobody@example.com")


def test_refuses_a_deactivated_account(make_user):
    make_user(email="gone@example.com", is_active=False)
    with pytest.raises(CommandError, match="deactivated"):
        call_command("promote_admin", "gone@example.com")


def test_an_existing_administrator_is_left_unchanged(admin_user):
    call_command("promote_admin", admin_user.email)

    assert not AuditLogEntry.objects.exists()
