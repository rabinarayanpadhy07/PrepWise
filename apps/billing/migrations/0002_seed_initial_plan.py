"""
Seed the initial subscription plan: INR 5,000 for 30 days, manual renewal.

Prices are edited later through the admin (or a new migration); the frontend
always reads them from /api/v1/billing/plans/.
"""

from django.db import migrations

INITIAL_PLAN = {
    "code": "platform-30-day",
    "name": "Platform AI · 30 days",
    "description": (
        "Use PrepWise's platform AI for resume analysis, mock interviews, "
        "and answer feedback without supplying your own API key."
    ),
    "currency": "INR",
    "price_minor": 500_000,  # INR 5,000.00 in paise
    "duration_days": 30,
    "renewal_mode": "manual",
    "is_active": True,
    "display_order": 0,
}


def create_initial_plan(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    defaults = {key: value for key, value in INITIAL_PLAN.items() if key != "code"}
    Plan.objects.get_or_create(code=INITIAL_PLAN["code"], defaults=defaults)


def remove_initial_plan(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(code=INITIAL_PLAN["code"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_initial_plan, remove_initial_plan),
    ]
