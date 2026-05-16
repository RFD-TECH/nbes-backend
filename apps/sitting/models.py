"""apps/sitting/models.py — Exam Sitting configuration and T-30 lock."""
import uuid
from django.db import models
from shared.validators import validate_sitting_ref


class Sitting(models.Model):
    """
    Examination cycle configuration.
    Auto-locked 30 days before sitting date (T-30) by Celery Beat task.
    Reference: NBES Architecture §2.3 — sitting app
    """
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CONFIGURED = "configured", "Configured"
        LOCKED = "locked", "Locked"
        ACTIVE = "active", "Active (Sitting in Progress)"
        CLOSED = "closed", "Closed"

    ref = models.CharField(
        max_length=15, primary_key=True,
        validators=[validate_sitting_ref],
        help_text="Format: BAR-YYYY-MM e.g. BAR-2026-05"
    )
    sitting_date = models.DateField()
    sitting_end_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    pass_mark = models.DecimalField(max_digits=5, decimal_places=2, default=50.00)
    # Normalisation settings — populated by NormalisationConfig
    normalisation_method = models.CharField(max_length=30, blank=True)
    created_by_id = models.UUIDField()
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sitting_sitting"

    def __str__(self):
        return f"Sitting {self.ref} — {self.get_status_display()}"


class SubjectPaper(models.Model):
    """
    One of the five subject papers per sitting (§71).
    Defines marks allocation and pass standard per paper.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting = models.ForeignKey(Sitting, on_delete=models.CASCADE, related_name="subject_papers")
    subject_code = models.CharField(max_length=20)
    subject_name = models.CharField(max_length=255)
    total_marks = models.PositiveSmallIntegerField(default=100)
    pass_mark = models.DecimalField(max_digits=5, decimal_places=2, default=50.00)
    duration_minutes = models.PositiveSmallIntegerField(default=180)

    class Meta:
        db_table = "sitting_subjectpaper"
        unique_together = [("sitting", "subject_code")]

    def __str__(self):
        return f"{self.sitting_id} — {self.subject_code}"


class Blueprint(models.Model):
    """Blueprint and curriculum alignment for a sitting."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting = models.OneToOneField(Sitting, on_delete=models.CASCADE, related_name="blueprint")
    content = models.JSONField(default=dict)  # topic weights, cognitive level distribution
    validated = models.BooleanField(default=False)
    validated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sitting_blueprint"


class SittingLock(models.Model):
    """Audit record of T-30 lock event."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting = models.OneToOneField(Sitting, on_delete=models.CASCADE, related_name="lock_record")
    locked_at = models.DateTimeField(auto_now_add=True)
    locked_by = models.CharField(max_length=20, default="system")  # "system" or keycloak_sub
    override = models.BooleanField(default=False)  # True if manually overridden before T-30
    override_reason = models.TextField(blank=True)

    class Meta:
        db_table = "sitting_sittinglock"
