"""apps/resit/models.py — Re-sit management, attempt counter, §73 enforcement."""
import uuid
from django.db import models
from django_fsm import FSMField, transition
from workflow.guards import resit_fee_confirmed, below_attempt_limit


class AttemptCounter(models.Model):
    """
    Per-candidate, per-paper attempt counter.
    Increments on confirmed attendance (Sat) regardless of outcome.
    Does NOT increment on withdrawals before sitting day.
    Enforces §73 maximum attempt limit.
    Reference: NBES Architecture §3.6 — Attempt Counter
    """
    id = models.BigAutoField(primary_key=True)
    candidate = models.ForeignKey(
        "registration.Candidate", on_delete=models.PROTECT, related_name="attempt_counters"
    )
    paper = models.ForeignKey(
        "sitting.SubjectPaper", on_delete=models.PROTECT, related_name="attempt_counters"
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_sitting_ref = models.CharField(max_length=15, null=True, blank=True)
    # Stores NBEC exception grants — each adds exactly one additional attempt
    exception_grants = models.JSONField(default=list)

    class Meta:
        db_table = "resit_attemptcounter"
        unique_together = [("candidate", "paper")]

    def __str__(self):
        return f"{self.candidate} — {self.paper} [{self.attempts} attempts]"

    def increment(self, sitting_ref: str):
        """Increment attempt count. Called after confirmed sitting attendance."""
        self.attempts += 1
        self.last_sitting_ref = sitting_ref
        self.save(update_fields=["attempts", "last_sitting_ref"])
        from shared.events import publish
        publish("AttemptIncremented", {
            "candidate_id": str(self.candidate_id),
            "paper_id": str(self.paper_id),
            "attempts": self.attempts,
        })

    def is_at_limit(self) -> bool:
        """Check §73 limit including any NBEC exception grants."""
        from apps.resit.services import get_max_attempts
        limit = get_max_attempts() + len(self.exception_grants)
        if self.attempts >= limit:
            from shared.events import publish
            publish("MaxAttemptReached", {
                "candidate_id": str(self.candidate_id),
                "paper_id": str(self.paper_id),
            })
            return True
        return False


class ResitRegistration(models.Model):
    """
    Re-sit registration for a specific paper and sitting.
    Reference: NBES Architecture §3.6 — Re-sit Workflow States
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        "registration.Candidate", on_delete=models.PROTECT, related_name="resit_registrations"
    )
    paper = models.ForeignKey(
        "sitting.SubjectPaper", on_delete=models.PROTECT, related_name="resit_registrations"
    )
    sitting_ref = models.CharField(max_length=15)
    status = FSMField(default="registered", protected=True)
    fee_confirmed = models.BooleanField(default=False)
    fee_reference = models.CharField(max_length=100, blank=True)
    withdrawal_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "resit_resitregistration"

    @transition(field=status, source="registered", target="fee_pending",
                conditions=[below_attempt_limit])
    def initiate(self):
        """Trigger System 20 re-sit fee payment request."""
        from apps.resit.tasks import trigger_resit_fee
        trigger_resit_fee.delay(str(self.id))

    @transition(field=status, source="fee_pending", target="confirmed",
                conditions=[resit_fee_confirmed])
    def confirm(self):
        from shared.events import publish
        publish("ResitRegistered", {"resit_id": str(self.id)})

    @transition(field=status, source=["registered", "fee_pending", "confirmed"],
                target="withdrawn")
    def withdraw(self, reason: str = ""):
        self.withdrawal_reason = reason


class ExceptionGrant(models.Model):
    """NBEC exception grant — adds exactly one additional attempt per §73."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attempt_counter = models.ForeignKey(
        AttemptCounter, on_delete=models.PROTECT, related_name="exception_records"
    )
    granted_by_id = models.UUIDField()   # NBEC member keycloak_sub
    rationale = models.TextField()
    granted_at = models.DateTimeField(auto_now_add=True)
    meeting_ref = models.CharField(max_length=50, blank=True)

    class Meta:
        db_table = "resit_exceptiongrant"


class GracePeriodConfig(models.Model):
    """Configurable grace period and late surcharge rules per sitting."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting_ref = models.CharField(max_length=15, unique=True)
    grace_period_days = models.PositiveSmallIntegerField(default=0)
    late_surcharge_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "resit_graceperiodconfig"
