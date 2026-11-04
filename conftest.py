import importlib
import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.ai_services import providers, service

PASSWORD = "Correct-Horse-Battery-9"
# Placeholder model IDs and key. Real ones are entered by administrators, never shipped.
LARGE_MODEL = "test-openai-large"
SMALL_MODEL = "test-openai-small"
PLATFORM_KEY = "sk-test-platform-key-not-real-0000WXYZ"
SECOND_PLATFORM_KEY = "sk-test-second-platform-key-000ABCD"


def _restore_seed_data():
    """
    Recreate rows that data migrations seed (the initial plan, AI modes, AI
    platform settings) if they are missing. Transactional tests truncate every
    table, and with --reuse-db the next run would otherwise start without them.
    """
    from django.apps import apps as django_apps

    from apps.billing.models import Plan

    seed_plan = importlib.import_module("apps.billing.migrations.0002_seed_initial_plan")
    plan_quotas = importlib.import_module("apps.billing.migrations.0003_plan_quotas_subscription")
    seed_ai = importlib.import_module(
        "apps.ai_services.migrations.0003_seed_modes_and_platform_settings"
    )

    plan = seed_plan.INITIAL_PLAN
    if not Plan.objects.filter(code=plan["code"]).exists():
        Plan.objects.create(**plan, **plan_quotas.INITIAL_PLAN_QUOTAS)
    seed_ai.seed(django_apps, None)


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        _restore_seed_data()


@pytest.fixture
def seeded_transactional_db(transactional_db):
    """
    A database where transactions really commit (for tests that race separate
    connections), with the migration-seeded rows present. pytest-django runs
    these tests after all others and truncates tables after each one.
    """
    _restore_seed_data()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture(autouse=True)
def _clear_cache():
    # Throttle counters live in the cache; keep tests independent.
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def make_user(db):
    def _make(email="user@example.com", password=PASSWORD, **extra):
        extra.setdefault("full_name", "Test User")
        return get_user_model().objects.create_user(email=email, password=password, **extra)

    return _make


@pytest.fixture
def user(make_user):
    return make_user()


@pytest.fixture
def other_user(make_user):
    return make_user(email="other@example.com", full_name="Other User")


@pytest.fixture
def admin_user(make_user):
    return make_user(email="admin@example.com", full_name="Admin User", role="admin")


@pytest.fixture
def client_for():
    """APIClient authenticated with a real access token for the given user."""

    def _client(user):
        client = APIClient()
        access = RefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client

    return _client


@pytest.fixture
def auth_client(client_for, user):
    return client_for(user)


@pytest.fixture
def subscribe(db):
    """Give a user an active platform subscription to the seeded plan."""
    from apps.billing.models import Plan, Subscription

    def _subscribe(user, **overrides):
        plan = Plan.objects.get(code="platform-30-day")
        return Subscription.objects.create(user=user, plan=plan, **overrides)

    return _subscribe


@pytest.fixture
def platform_key(db):
    """Save an (encrypted) platform OpenAI API key, as the admin panel does."""
    from apps.ai_services.crypto import encrypt_api_key
    from apps.ai_services.models import PlatformAPIKey

    def _save(api_key=PLATFORM_KEY, **extra):
        extra.setdefault("priority", PlatformAPIKey.objects.count() + 1)
        return PlatformAPIKey.objects.create(
            encrypted_key=encrypt_api_key(api_key), key_hint=api_key[-4:], **extra
        )

    return _save


@pytest.fixture
def ai_config(db, platform_key):
    """
    A working AI configuration: Fast mode served by a large OpenAI model (with
    a cheaper fallback) and one platform API key. Platform spend is capped at
    100 (cost currency).
    """
    from apps.ai_services.models import AIMode, AIModelConfig, AIPlatformSettings

    small = AIModelConfig.objects.create(
        model_id=SMALL_MODEL,
        display_name="Small",
        max_input_tokens=100_000,
        max_output_tokens=8_000,
        input_price_per_million=Decimal("1"),
        output_price_per_million=Decimal("5"),
    )
    large = AIModelConfig.objects.create(
        model_id=LARGE_MODEL,
        display_name="Large",
        max_input_tokens=200_000,
        max_output_tokens=32_000,
        input_price_per_million=Decimal("5"),
        output_price_per_million=Decimal("25"),
        fallback=small,
    )
    fast = AIMode.objects.get(key="fast")
    fast.is_enabled = True
    fast.model = large
    fast.allow_fallback = False
    fast.save()
    platform = AIPlatformSettings.load()
    platform.monthly_spend_cap = Decimal("100")
    platform.save()
    key = platform_key()
    return {"large": large, "small": small, "fast": fast, "key": key}


class FakeProvider(providers.AIProvider):
    """
    Stands in for OpenAI. Queue responses with `respond()` (a dict
    becomes JSON text) or failures with `fail()`; every call is recorded.
    """

    def __init__(self):
        self.queue = []
        self.calls = []
        self.verify_error = None
        self.built = []

    def respond(
        self, data, *, finish=providers.FINISH_COMPLETE, input_tokens=1200, output_tokens=800
    ):
        text = data if isinstance(data, str) else json.dumps(data)
        self.queue.append(
            {
                "text": text,
                "finish": finish,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )

    def fail(self, exc):
        self.queue.append(exc)

    def generate_json(self, *, system, prompt, schema, max_output_tokens):
        built = self.built[-1]
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "schema": schema,
                "max_output_tokens": max_output_tokens,
                **built,
            }
        )
        if not self.queue:
            raise AssertionError("FakeProvider called with no queued response")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return providers.ProviderResponse(provider=built["provider"], model=built["model"], **item)

    def verify_key(self):
        if self.verify_error is not None:
            raise self.verify_error

    def check_model(self):
        self.verify_key()


@pytest.fixture
def fake_ai(monkeypatch):
    fake = FakeProvider()

    def _build(provider, *, api_key, model):
        fake.built.append({"provider": provider, "api_key": api_key, "model": model})
        return fake

    monkeypatch.setattr(service, "build_provider", _build)
    return fake
