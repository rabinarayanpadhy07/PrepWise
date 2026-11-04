from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class UserManager(DjangoUserManager):
    """Email-based manager: users sign in with their email address."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", User.Role.ADMIN)
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            raise ValueError("Superusers must have is_staff=True and is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    PrepWise user. Defined from the start so the user table can evolve
    without a painful swap of AUTH_USER_MODEL later.
    """

    class Role(models.TextChoices):
        USER = "user", "User"
        ADMIN = "admin", "Administrator"

    username = None
    first_name = None
    last_name = None

    # Emails are lowercased on save; the Lower() constraint below also guards
    # against case-variant duplicates written outside the ORM's save().
    email = models.EmailField("email address", max_length=254, unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    # Application role. Only an existing administrator (or the Django admin site)
    # can change it; no API lets users set their own role.
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.USER)
    # Access and refresh tokens issued at or before this time are rejected
    # (administrator "sign out everywhere"; see apps.accounts.authentication).
    sessions_revoked_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Google's stable account ID ("sub"), set on first Google sign-in.
    # Emails can change; this can't.
    google_sub = models.CharField(
        max_length=255, null=True, blank=True, unique=True, editable=False
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                name="accounts_user_email_ci_unique",
                violation_error_message="A user with this email address already exists.",
            ),
            models.CheckConstraint(
                condition=Q(role__in=["user", "admin"]), name="accounts_user_role_valid"
            ),
        ]
        indexes = [models.Index(fields=["role"], name="accounts_user_role_idx")]

    def __str__(self):
        return self.email

    @property
    def is_admin(self):
        return self.is_active and self.role == self.Role.ADMIN

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)

    def get_full_name(self):
        return self.full_name or self.email

    def get_short_name(self):
        return self.full_name.split(" ")[0] if self.full_name else self.email
