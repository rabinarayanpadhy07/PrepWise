"""
Audit logging for administrator actions.

    before = snapshot(instance, FIELDS)
    ...change and save instance...
    record(request, "plan.update", instance, diff(before, snapshot(instance, FIELDS)))

Values are converted to JSON-safe forms (decimals and dates as strings,
related objects as their primary key). Never pass secret values here.
"""

import datetime
import decimal

from django.db import models

from apps.administration.models import AuditLogEntry
from common.logging import request_id_var


def _plain(value):
    if isinstance(value, models.Model):
        return value.pk
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    return value


def snapshot(instance, fields):
    return {field: _plain(getattr(instance, field)) for field in fields}


def diff(before, after):
    """{field: [before, after]} for every field whose value changed."""
    return {
        field: [before.get(field), after.get(field)]
        for field in after
        if before.get(field) != after.get(field)
    }


def record(request, action, target, changes=None, *, reason="", target_type=None, label=None):
    """Append an audit entry for `target` (a model instance)."""
    actor = request.user
    return AuditLogEntry.objects.create(
        actor=actor,
        actor_email=actor.email,
        action=action,
        target_type=target_type or target._meta.label_lower,
        target_id=str(target.pk),
        target_label=(label if label is not None else str(target))[:200],
        changes=changes or {},
        reason=(reason or "")[:300],
        request_id=getattr(request, "request_id", None) or request_id_var.get(None) or "",
    )
