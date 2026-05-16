"""apps/reporting/models.py — KPI aggregation, dashboard snapshots, audit exports."""
import uuid
from django.db import models
from django.utils import timezone


class ReportSnapshot(models.Model):
    """Pre-computed KPI snapshot for a sitting — refreshed on-demand or by schedule."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting_ref = models.CharField(max_length=15)
    snapshot_type = models.CharField(max_length=50)   # e.g. "marking_progress", "sla_dashboard"
    data = models.JSONField(default=dict)
    generated_at = models.DateTimeField(default=timezone.now)
    generated_by_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "reporting_reportsnapshot"
        indexes = [models.Index(fields=["sitting_ref", "snapshot_type"])]


class KPIMetric(models.Model):
    """Individual KPI metric record — aggregated from domain models."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting_ref = models.CharField(max_length=15, blank=True)
    metric_name = models.CharField(max_length=100)
    metric_value = models.JSONField()
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "reporting_kpimetric"


class AuditExport(models.Model):
    """
    Signed audit export package for judicial review.
    Generated on request — package signed and stored in MinIO.
    Reference: NBES Architecture §2.3 — reporting app
    """
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        GENERATING = "generating", "Generating"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requested_by_id = models.UUIDField()
    sitting_ref = models.CharField(max_length=15, blank=True)
    date_range_start = models.DateField(null=True, blank=True)
    date_range_end = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    document_ref = models.TextField(blank=True)   # MinIO path of signed export package
    requested_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "reporting_auditexport"
