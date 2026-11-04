"""
Add platform AI quotas to plans and introduce subscriptions.

The initial plan's quotas are starting values; administrators can change them
in the Django admin. Existing subscriptions keep the quotas they were created with.
"""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

INITIAL_PLAN_QUOTAS = {
    "ai_token_quota": 3_000_000,
    "interview_quota": 60,
    "voice_seconds_quota": 5 * 60 * 60,
}


def set_initial_plan_quotas(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(code="platform-30-day").update(**INITIAL_PLAN_QUOTAS)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_seed_initial_plan"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="ai_token_quota",
            field=models.PositiveBigIntegerField(
                default=0, help_text="Platform AI tokens (input + output) included per period."
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="plan",
            name="interview_quota",
            field=models.PositiveIntegerField(
                default=0, help_text="Mock interview sessions included per period."
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="plan",
            name="voice_seconds_quota",
            field=models.PositiveIntegerField(
                default=0, help_text="Seconds of recorded voice answers included per period."
            ),
            preserve_default=False,
        ),
        migrations.RunPython(set_initial_plan_quotas, migrations.RunPython.noop),
        migrations.CreateModel(
            name="Subscription",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("active", "Active"), ("cancelled", "Cancelled")], default="active", max_length=20)),
                ("source", models.CharField(choices=[("admin", "Granted by an administrator"), ("payment", "Payment")], default="admin", max_length=20)),
                ("starts_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("ends_at", models.DateTimeField(blank=True)),
                ("ai_token_quota", models.PositiveBigIntegerField(blank=True)),
                ("interview_quota", models.PositiveIntegerField(blank=True)),
                ("voice_seconds_quota", models.PositiveIntegerField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("plan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="subscriptions", to="billing.plan")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="subscriptions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-starts_at", "-id"],
                "indexes": [models.Index(fields=["user", "status", "ends_at"], name="billing_sub_user_active_idx")],
                "constraints": [
                    models.CheckConstraint(condition=models.Q(("ends_at__gt", models.F("starts_at"))), name="billing_subscription_period_valid"),
                    models.CheckConstraint(condition=models.Q(("status__in", ["active", "cancelled"])), name="billing_subscription_status_valid"),
                    models.CheckConstraint(condition=models.Q(("source__in", ["admin", "payment"])), name="billing_subscription_source_valid"),
                ],
            },
        ),
    ]
