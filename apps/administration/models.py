from django.conf import settings
from django.db import models


class AuditLogEntry(models.Model):
    """
    Append-only record of an administrator action through the admin API: who
    did it, what changed (field -> [before, after]), and when. Secrets are never
    recorded; changes to secret-bearing fields are not possible through the API.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    # Snapshot, so the entry stays readable if the actor's account is deleted.
    actor_email = models.EmailField()
    action = models.CharField(max_length=60, db_index=True)
    target_type = models.CharField(max_length=60)
    target_id = models.CharField(max_length=64)
    target_label = models.CharField(max_length=200, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
        ]
        verbose_name = "audit log entry"
        verbose_name_plural = "audit log"

    def __str__(self):
        return f"{self.actor_email} {self.action} {self.target_type}:{self.target_id}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("Audit log entries can't be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit log entries can't be deleted.")
