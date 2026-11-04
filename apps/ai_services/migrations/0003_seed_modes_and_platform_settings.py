"""
Create the three AI modes (disabled until an administrator assigns models) and
the AI platform settings row. No model IDs or prices are seeded: administrators
enter them from the providers' documentation.
"""

from django.db import migrations

MODES = [
    ("fast", "Quickest responses at the lowest cost.", 0),
    ("balanced", "A balance of speed, cost and depth.", 1),
    ("advanced", "The most thorough analysis and feedback.", 2),
]


def seed(apps, schema_editor):
    AIMode = apps.get_model("ai_services", "AIMode")
    AIPlatformSettings = apps.get_model("ai_services", "AIPlatformSettings")
    for key, description, order in MODES:
        AIMode.objects.get_or_create(
            key=key, defaults={"description": description, "sort_order": order}
        )
    AIPlatformSettings.objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [
        ("ai_services", "0002_ai_routing_and_byok"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
