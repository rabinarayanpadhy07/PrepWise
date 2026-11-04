import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

pytestmark = pytest.mark.django_db

User = get_user_model()


def test_custom_user_model_is_active():
    assert User._meta.label == "accounts.User"
    assert User.USERNAME_FIELD == "email"


def test_create_user_normalises_email_and_hashes_password():
    user = User.objects.create_user(email="  Jane.Doe@Example.COM ", password="a-strong-pass")

    assert user.email == "jane.doe@example.com"
    assert user.check_password("a-strong-pass")
    assert user.password != "a-strong-pass"
    assert not user.is_staff and not user.is_superuser


def test_create_user_requires_email():
    with pytest.raises(ValueError, match="email"):
        User.objects.create_user(email="", password="x")


def test_create_superuser_sets_flags():
    admin = User.objects.create_superuser(email="admin@example.com", password="a-strong-pass")

    assert admin.is_staff and admin.is_superuser


def test_email_is_unique_regardless_of_case():
    User.objects.create_user(email="dup@example.com", password="pass-one-123")
    other = User.objects.create_user(email="other@example.com", password="pass-two-123")

    # Bypass save() normalisation to prove the database constraint itself holds.
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=other.pk).update(email="DUP@example.com")
