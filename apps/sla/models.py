"""apps/sla/models.py — SLA Monitor: configs, instances, escalations."""
import uuid
from django.db import models
from django.utils import timezone


class SLAConfig(models.Model):
    """
    Configurable SLA definition per type.
    Reference: NBES Architecture §10.1 — SLA types
    """
    class SLAType(models.TextChoices):
        RESULTS_PUBLICATION = "results_publication", "Results Publication (21-day)"
        CERT_TRIGGER = "cert_trigger", "Certificate Trigger (1-hour)"
        BORDERLINE_MODERATION = "borderline_moderation", "Borderline Moderation"
        NLEMS_RESPONSE = "nlems_response", "NLEMS Eligibility Response"
        BOARD_RATIFICATION = "board_ratification", "Board Ratification"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sla_type = models.CharField(max_length=50, choices=SLAType.choices, unique=True)
    target_hours = models.DecimalField(max_digits=8, decimal_places=2)
    at_risk_threshold_hours = models.DecimalField(
        max_digits=8, decimal_places=2,
        help_text="Hours before deadline to trigger At-Risk status"
    )
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sla_slaconfig"

    def __str__(self):
        return f"SLA: {self.get_sla_type_display()} ({self.target_hours}h)"


class SLAInstance(models.Model):
    """
    One SLA instance per tracked entity (sitting, cert trigger, etc.).
    Status computed by check_all_slas Celery Beat task every 15 minutes.
    Reference: NBES Architecture §10.2
    """
    class Status(models.TextChoices):
        ON_TRACK = "on_track", "On Track"
        AT_RISK = "at_risk", "At Risk"
        OVERDUE = "overdue", "Overdue"
        CLOSED = "closed", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    config = models.ForeignKey(SLAConfig, on_delete=models.PROTECT, related_name="instances")
    entity_type = models.CharField(max_length=100)
    entity_id = models.UUIDField()
    started_at = models.DateTimeField()
    deadline = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ON_TRACK)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sla_slainstance"
        indexes = [models.Index(fields=["status", "deadline"])]

    def __str__(self):
        return f"SLA {self.config.sla_type} — {self.entity_type}:{self.entity_id} [{self.status}]"

    def close(self):
        self.status = self.Status.CLOSED
        self.closed_at = timezone.now()
        self.save(update_fields=["status", "closed_at"])


class SLAEscalation(models.Model):
    """Record of each escalation notification sent for an SLA instance."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sla_instance = models.ForeignKey(SLAInstance, on_delete=models.CASCADE, related_name="escalations")
    trigger_status = models.CharField(max_length=20)   # at_risk or overdue
    escalated_to = models.JSONField(default=list)       # list of keycloak_sub UUIDs
    escalated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sla_slaescalation"
