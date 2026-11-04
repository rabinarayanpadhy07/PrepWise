"""
Switch the AI provider from Google Gemini to OpenAI.

Gemini model rows and the saved Gemini API keys can't be used with OpenAI, so
they are removed. AI modes that pointed at a Gemini model are left with no
model and switched off until an administrator picks an OpenAI model in the
admin panel. Provider call logs and usage history are kept as they are.
"""

import django.db.models.deletion
from django.db import migrations, models


def _flush_deferred_checks(schema_editor):
    # Run deferred FK checks now so the ALTER TABLEs that follow in this
    # transaction don't fail with "pending trigger events" on PostgreSQL.
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        schema_editor.execute("SET CONSTRAINTS ALL DEFERRED")


def remove_gemini(apps, schema_editor):
    AIMode = apps.get_model("ai_services", "AIMode")
    AIModelConfig = apps.get_model("ai_services", "AIModelConfig")
    PlatformAPIKey = apps.get_model("ai_services", "PlatformAPIKey")

    gemini = AIModelConfig.objects.exclude(provider="openai")
    AIMode.objects.filter(model__in=gemini).update(model=None, is_enabled=False)
    AIModelConfig.objects.filter(fallback__in=gemini).update(fallback=None)
    gemini.delete()
    # Every key saved so far is a Gemini key (the pool was Gemini-only).
    PlatformAPIKey.objects.all().delete()
    _flush_deferred_checks(schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("ai_services", "0006_gemini_platform_keys"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="aimodelconfig",
            name="ai_model_provider_valid",
        ),
        migrations.RunPython(remove_gemini, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="aimode",
            name="model",
            field=models.ForeignKey(
                blank=True,
                help_text="The OpenAI model that serves this mode.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ai_services.aimodelconfig",
            ),
        ),
        migrations.AlterField(
            model_name="aimodelconfig",
            name="model_id",
            field=models.CharField(
                help_text="The exact model identifier from OpenAI's documentation.",
                max_length=120,
            ),
        ),
        migrations.AlterField(
            model_name="aimodelconfig",
            name="provider",
            field=models.CharField(
                choices=[("openai", "OpenAI")], default="openai", max_length=20
            ),
        ),
        migrations.AlterField(
            model_name="platformapikey",
            name="status",
            field=models.CharField(
                choices=[("active", "Working"), ("rejected", "Rejected by OpenAI")],
                default="active",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="providercalllog",
            name="provider",
            field=models.CharField(
                choices=[("openai", "OpenAI")], default="openai", max_length=20
            ),
        ),
        migrations.AddConstraint(
            model_name="aimodelconfig",
            constraint=models.CheckConstraint(
                condition=models.Q(("provider__in", ["openai"])),
                name="ai_model_provider_valid",
            ),
        ),
    ]
