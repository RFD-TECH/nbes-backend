"""apps/cert_trigger/models.py — Certificate trigger on confirmed PASS result."""
import uuid
from django.db import models
from django.utils import timezone


class CertTriggerRecord(models.Model):
    """
    One record per PASS candidate. Tracks System 14 webhook fire + acknowledgement.
    1-hour SLA monitored by Celery Beat task.
    Reference: NBES Architecture §2.3 — cert_trigger app
    """
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        FIRED = "fired", "Fired — Awaiting Acknowledgement"
        ACKNOWLEDGED = "acknowledged", "Acknowledged by System 14"
        FAILED = "failed", "Failed — Retrying"
        SLA_BREACHED = "sla_breached", "SLA Breached"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        "registration.Candidate", on_delete=models.PROTECT, related_name="cert_triggers"
    )
    sitting_ref = models.CharField(max_length=15)
    result_ref = models.UUIDField()                  # NormalisedResult.id
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    webhook_payload = models.JSONField(null=True, blank=True)
    fired_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledgement_ref = models.CharField(max_length=100, blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    sla_deadline = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "cert_trigger_certtriggerrecord"
        indexes = [models.Index(fields=["status", "sla_deadline"])]

    def __str__(self):
        return f"CertTrigger {self.candidate_id} — {self.sitting_ref} [{self.status}]"

    def fire(self):
        """Mark as fired and set 1-hour SLA deadline."""
        self.status = self.Status.FIRED
        self.fired_at = timezone.now()
        self.sla_deadline = timezone.now() + timezone.timedelta(hours=1)
        self.save(update_fields=["status", "fired_at", "sla_deadline"])
        from shared.events import publish
        publish("CertTriggerFired", {
            "trigger_id": str(self.id),
            "candidate_id": str(self.candidate_id),
        })

    def acknowledge(self, ref: str):
        """Record System 14 acknowledgement."""
        self.status = self.Status.ACKNOWLEDGED
        self.acknowledged_at = timezone.now()
        self.acknowledgement_ref = ref
        self.save(update_fields=["status", "acknowledged_at", "acknowledgement_ref"])
        from shared.events import publish
        publish("CertTriggerAcknowledged", {"trigger_id": str(self.id)})


class PassRecord(models.Model):
    """
    Held pass record queryable by System 14 for certificate verification.
    Immutable once written.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        "registration.Candidate", on_delete=models.PROTECT, related_name="pass_records"
    )
    sitting_ref = models.CharField(max_length=15)
    index_number = models.CharField(max_length=15)
    full_name = models.CharField(max_length=255)
    pass_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "cert_trigger_passrecord"
        unique_together = [("candidate", "sitting_ref")]
