"""
Client-supplied idempotency keys for operations that call an AI provider.

    record, existing_id = claim(user, "interview_start", key)
    if existing_id: return the existing resource
    try: ...do the work...; complete(record, resource.pk)
    except: abandon(record); raise

A repeated request with the same key returns the first result instead of
paying for a second provider call. A request that arrives while the first is
still running gets 409 `request_in_progress`. A failed attempt releases its
key so the client can retry with it.
"""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException

from apps.usage.models import IdempotencyRecord

# An in-progress claim older than this is treated as abandoned (worker crash).
STALE_AFTER = timedelta(minutes=15)


class RequestInProgress(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "request_in_progress"
    default_detail = "This request is already being processed. Please wait a moment."


def claim(user, scope, key):
    """Return (record, existing_resource_id). Both are None when no key is given."""
    if not key:
        return None, None
    try:
        with transaction.atomic():
            return IdempotencyRecord.objects.create(user=user, scope=scope, key=key), None
    except IntegrityError:
        pass

    record = IdempotencyRecord.objects.get(user=user, scope=scope, key=key)
    if record.status == IdempotencyRecord.Status.COMPLETED:
        return record, record.resource_id
    if record.updated_at > timezone.now() - STALE_AFTER:
        raise RequestInProgress()
    # Take over an abandoned claim, unless another request just did.
    taken = IdempotencyRecord.objects.filter(
        pk=record.pk,
        status=IdempotencyRecord.Status.IN_PROGRESS,
        updated_at=record.updated_at,
    ).update(updated_at=timezone.now())
    if not taken:
        raise RequestInProgress()
    return record, None


def complete(record, resource_id):
    if record is not None:
        IdempotencyRecord.objects.filter(pk=record.pk).update(
            status=IdempotencyRecord.Status.COMPLETED,
            resource_id=str(resource_id),
            updated_at=timezone.now(),
        )


def abandon(record):
    if record is not None:
        IdempotencyRecord.objects.filter(
            pk=record.pk, status=IdempotencyRecord.Status.IN_PROGRESS
        ).delete()
