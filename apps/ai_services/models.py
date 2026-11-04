"""
Administrator-configured AI: OpenAI only, paid for by the platform.

  AIModelConfig      a OpenAI model an administrator has enabled, with its
                     per-request token limits, prices, and optional cheaper fallback
  AIMode             Fast / Balanced / Advanced: which model serves each mode
  AIPlatformSettings singleton with global limits (spending cap, voice, reservations)
  PlatformAPIKey     the platform's OpenAI API keys, entered in the admin panel and
                     used in turn: a key at its rate limit cools down and the next
                     one is used (see apps.ai_services.service)
  UserAIPreference   the user's preferred AI mode
  ProviderCallLog    sanitized call outcomes for health monitoring

Only subscribers (paid, or granted access by an administrator) can use AI.
Model identifiers and prices are entered by administrators.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Provider(models.TextChoices):
    OPENAI = "openai", "OpenAI"


PROVIDER_VALUES = [choice.value for choice in Provider]
OPENAI = Provider.OPENAI.value


class AIModelConfig(models.Model):
    provider = models.CharField(max_length=20, choices=Provider.choices, default=OPENAI)
    model_id = models.CharField(
        max_length=120,
        help_text="The exact model identifier from OpenAI's documentation.",
    )
    display_name = models.CharField(max_length=100)
    is_enabled = models.BooleanField(default=True)
    max_input_tokens = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Largest estimated prompt size accepted per request.",
    )
    max_output_tokens = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Upper bound on response tokens per request; feature limits are clamped to it.",
    )
    input_price_per_million = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Price per 1,000,000 input tokens in the cost currency (0 for free keys).",
    )
    output_price_per_million = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Price per 1,000,000 output tokens in the cost currency (0 for free keys).",
    )
    fallback = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fallback_for",
        help_text="A model that costs no more, used when this one is unavailable or at its limit.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]
        verbose_name = "AI model"
        constraints = [
            models.UniqueConstraint(fields=["provider", "model_id"], name="ai_model_unique"),
            models.CheckConstraint(
                condition=Q(provider__in=PROVIDER_VALUES), name="ai_model_provider_valid"
            ),
            models.CheckConstraint(
                condition=~Q(fallback=models.F("id")), name="ai_model_fallback_not_self"
            ),
        ]

    def __str__(self):
        return f"{self.display_name} ({self.model_id})"

    def costs_no_more_than(self, other):
        return (
            self.input_price_per_million <= other.input_price_per_million
            and self.output_price_per_million <= other.output_price_per_million
        )

    def clean(self):
        if self.fallback_id:
            fallback = self.fallback
            if self.pk and fallback.pk == self.pk:
                raise ValidationError({"fallback": "A model can't fall back to itself."})
            if not fallback.costs_no_more_than(self):
                raise ValidationError(
                    {"fallback": "The fallback can't cost more than this model (input or output)."}
                )


class AIMode(models.Model):
    class Key(models.TextChoices):
        FAST = "fast", "Fast"
        BALANCED = "balanced", "Balanced"
        ADVANCED = "advanced", "Advanced"

    DEFAULT = Key.FAST

    key = models.CharField(max_length=20, choices=Key.choices, unique=True)
    description = models.CharField(max_length=200, blank=True)
    is_enabled = models.BooleanField(default=False)
    model = models.ForeignKey(
        AIModelConfig,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="The OpenAI model that serves this mode.",
    )
    allow_fallback = models.BooleanField(
        default=True,
        help_text="Use the model's configured fallback when the model is unavailable or limited.",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "AI mode"
        constraints = [
            models.CheckConstraint(
                condition=Q(key__in=["fast", "balanced", "advanced"]), name="ai_mode_key_valid"
            ),
        ]

    def __str__(self):
        return self.get_key_display()

    def clean(self):
        if self.is_enabled and self.model_id is None:
            raise ValidationError({"model": "Choose a model before enabling this mode."})


class AIPlatformSettings(models.Model):
    """Single row (pk=1) of global AI limits, editable in the admin panel."""

    cost_currency = models.CharField(
        max_length=3,
        default="USD",
        help_text="Currency of model prices and the spending cap (provider billing currency).",
    )
    monthly_spend_cap = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Estimated AI spend allowed per calendar month. Empty means no cap.",
    )
    voice_max_answer_seconds = models.PositiveIntegerField(
        default=180, validators=[MinValueValidator(10)]
    )
    reservation_ttl_seconds = models.PositiveIntegerField(
        default=600,
        validators=[MinValueValidator(60)],
        help_text=(
            "How long an unreconciled quota reservation holds allowance before it is "
            "treated as abandoned. Keep it above the AI request timeout."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI platform settings"
        verbose_name_plural = "AI platform settings"

    def __str__(self):
        return "AI platform settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls, *, lock=False):
        queryset = cls.objects.select_for_update() if lock else cls.objects
        instance = queryset.filter(pk=1).first()
        if instance is None:
            instance, _ = cls.objects.get_or_create(pk=1)
            if lock:
                instance = cls.objects.select_for_update().get(pk=1)
        return instance


class PlatformAPIKey(models.Model):
    """
    One of the platform's OpenAI API keys, entered by an administrator.

    Keys are tried in priority order. When OpenAI reports a key is at its rate
    limit for a model, that key "cools down" for that model (1 minute, doubling
    on repeated hits up to an hour) and the next key is used. A key OpenAI
    rejects is marked rejected and skipped until an administrator replaces or
    re-enables it. Encrypted with AES-256-GCM (AI_KEY_ENCRYPTION_KEYS); the
    plaintext is never returned by the API or logged.
    """

    # Associated-data identity for encryption.
    CRYPTO_OWNER = "platform"
    COOLDOWN_SECONDS = 60
    MAX_COOLDOWN_SECONDS = 3600

    class Status(models.TextChoices):
        ACTIVE = "active", "Working"
        REJECTED = "rejected", "Rejected by OpenAI"

    label = models.CharField(max_length=80, blank=True)
    encrypted_key = models.TextField()
    key_hint = models.CharField(max_length=4)
    is_enabled = models.BooleanField(default=True)
    priority = models.PositiveIntegerField(default=0, help_text="Lower numbers are tried first.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    # {model_id: {"until": ISO datetime, "hits": consecutive rate-limit hits}}
    cooldowns = models.JSONField(default=dict, blank=True)
    request_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=60, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["priority", "id"]
        verbose_name = "platform API key"
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["active", "rejected"]), name="platform_api_key_status_valid"
            ),
        ]

    def __str__(self):
        return f"{self.label or 'OpenAI key'} ****{self.key_hint}"

    def cooling_until(self, model_id, now=None):
        """When this key may be used for `model_id` again, or None if it's ready now."""
        entry = (self.cooldowns or {}).get(model_id)
        if not entry:
            return None
        try:
            until = datetime.fromisoformat(entry["until"])
        except (KeyError, TypeError, ValueError):
            return None
        return until if until > (now or timezone.now()) else None

    def next_cooldown(self, model_id, now=None):
        """The cooldown entry to store after another rate-limit hit for `model_id`."""
        now = now or timezone.now()
        hits = int(((self.cooldowns or {}).get(model_id) or {}).get("hits", 0)) + 1
        seconds = min(self.COOLDOWN_SECONDS * 2 ** (hits - 1), self.MAX_COOLDOWN_SECONDS)
        return {"until": (now + timedelta(seconds=seconds)).isoformat(), "hits": hits}


class UserAIPreference(models.Model):
    """The user's preferred AI mode; a request may still choose another mode."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_preference"
    )
    ai_mode = models.CharField(max_length=20, choices=AIMode.Key.choices, default=AIMode.DEFAULT)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user_id} {self.ai_mode}"


class ProviderCallLog(models.Model):
    """
    One provider call attempt, for health monitoring. Holds only sanitized
    facts: no prompts, responses, user content, user identity, or keys.
    `outcome` is "success" or the service error code (for example
    "ai_rate_limited").
    """

    SUCCESS = "success"

    provider = models.CharField(max_length=20, choices=Provider.choices, default=OPENAI)
    model = models.CharField(max_length=120, blank=True)
    api_key = models.ForeignKey(
        PlatformAPIKey, on_delete=models.SET_NULL, null=True, blank=True, related_name="calls"
    )
    feature = models.CharField(max_length=50)
    outcome = models.CharField(max_length=60)
    latency_ms = models.PositiveIntegerField(default=0)
    used_fallback = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["provider", "created_at"], name="ai_call_provider_time_idx"),
            models.Index(fields=["outcome", "created_at"], name="ai_call_outcome_time_idx"),
        ]

    def __str__(self):
        return f"{self.provider} {self.model} {self.outcome}"

    @property
    def is_error(self):
        return self.outcome != self.SUCCESS
