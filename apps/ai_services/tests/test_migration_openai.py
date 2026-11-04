"""Migration 0007: Gemini models and keys are removed and the modes switched off."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("ai_services", "0006_gemini_platform_keys")]
AFTER = [("ai_services", "0007_openai_platform_keys")]
LIMITS = {"max_input_tokens": 1000, "max_output_tokens": 100}


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)
    return executor.loader.project_state(target).apps


@pytest.mark.django_db(transaction=True)
def test_gemini_rows_are_removed_and_modes_disabled(seeded_transactional_db):
    old = _migrate(BEFORE)
    try:
        AIModelConfig = old.get_model("ai_services", "AIModelConfig")
        AIMode = old.get_model("ai_services", "AIMode")
        PlatformAPIKey = old.get_model("ai_services", "PlatformAPIKey")
        ProviderCallLog = old.get_model("ai_services", "ProviderCallLog")

        small = AIModelConfig.objects.create(
            provider="gemini", model_id="gemini-small", display_name="Small", **LIMITS
        )
        large = AIModelConfig.objects.create(
            provider="gemini",
            model_id="gemini-large",
            display_name="Large",
            fallback=small,
            **LIMITS,
        )
        AIMode.objects.filter(key="fast").update(model=large, is_enabled=True)
        key = PlatformAPIKey.objects.create(encrypted_key="x", key_hint="WXYZ")
        ProviderCallLog.objects.create(
            provider="gemini", model="gemini-large", api_key=key, feature="f", outcome="success"
        )
    finally:
        new = _migrate(AFTER)

    assert not new.get_model("ai_services", "AIModelConfig").objects.exists()
    assert not new.get_model("ai_services", "PlatformAPIKey").objects.exists()
    fast = new.get_model("ai_services", "AIMode").objects.get(key="fast")
    assert (fast.model_id, fast.is_enabled) == (None, False)
    # Call history is kept, detached from the deleted key.
    [log] = new.get_model("ai_services", "ProviderCallLog").objects.all()
    assert (log.provider, log.api_key_id) == ("gemini", None)
