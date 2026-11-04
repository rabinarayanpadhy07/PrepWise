from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Cross-cutting infrastructure: errors, logging, health checks. Defines no models."""

    name = "common"
    verbose_name = "Common"
