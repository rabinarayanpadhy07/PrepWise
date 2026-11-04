"""
Snapshot every existing plan as version 1 and attach existing subscriptions to
their plan's snapshot, so purchased terms are preserved from here on.
"""

from django.db import migrations

VERSIONED_FIELDS = (
    "name",
    "currency",
    "price_minor",
    "duration_days",
    "renewal_mode",
    "ai_token_quota",
    "daily_ai_token_limit",
    "interview_quota",
    "daily_interview_limit",
    "voice_seconds_quota",
    "daily_voice_seconds_limit",
)


def backfill(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanVersion = apps.get_model("billing", "PlanVersion")
    Subscription = apps.get_model("billing", "Subscription")
    for plan in Plan.objects.all():
        version = PlanVersion.objects.filter(plan=plan).order_by("-version").first()
        if version is None:
            version = PlanVersion.objects.create(
                plan=plan,
                version=1,
                **{field: getattr(plan, field) for field in VERSIONED_FIELDS},
            )
        Subscription.objects.filter(plan=plan, plan_version__isnull=True).update(
            plan_version=version
        )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0004_plan_versions_and_daily_limits"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
