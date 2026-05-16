"""apps/marking/models.py — AI Marking, Moderation and Reconciliation with FSM."""
import uuid
from django.db import models
from django_fsm import FSMField, transition
from workflow.guards import (
    ai_scoring_complete, is_borderline, no_moderator_conflict,
    has_justification, reconciliation_required,
)


class Script(models.Model):
    """
    Candidate answer script. FSM tracks marking workflow.
    audit_hash: SHA-256 over all marking fields — recomputed when final_mark set.
    Reference: NBES Architecture §3.4 — Marking & Moderation Workflow States
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    registration = models.ForeignKey(
        "registration.Registration", on_delete=models.PROTECT, related_name="scripts"
    )
    subject_paper = models.ForeignKey(
        "sitting.SubjectPaper", on_delete=models.PROTECT, related_name="scripts"
    )
    # Source: CBT = System 10B response ingestion; PBT = scanned + OCR
    source = models.CharField(max_length=10, choices=[("cbt", "CBT"), ("pbt", "PBT")])
    script_ref = models.CharField(max_length=50, unique=True)
    status = FSMField(default="received", protected=True)
    borderline_flagged = models.BooleanField(default=False)
    reconciliation_required = models.BooleanField(default=False)
    audit_hash = models.CharField(max_length=64, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "marking_script"

    def __str__(self):
        return f"Script {self.script_ref} [{self.status}]"

    # ── FSM Transitions ────────────────────────────────────────────────────────

    @transition(field=status, source="received", target="ai_marking")
    def start_ai_marking(self):
        """Triggers AI scoring Celery task."""
        from apps.marking.tasks import run_ai_scoring
        run_ai_scoring.delay(str(self.id))

    @transition(field=status, source="ai_marking", target="ai_complete",
                conditions=[ai_scoring_complete])
    def complete_ai_marking(self):
        """AI scored. Computes borderline flag and audit hash."""
        from apps.marking.services import compute_borderline_flag, compute_audit_hash
        compute_borderline_flag(self)
        compute_audit_hash(self)
        from shared.events import publish
        publish("AIMarkingComplete", {"script_id": str(self.id)})

    @transition(field=status, source="ai_complete", target="borderline",
                conditions=[is_borderline])
    def flag_borderline(self):
        """Mark ±5% of pass mark — mandatory human moderation required."""
        from shared.events import publish
        publish("BorderlineFlagged", {"script_id": str(self.id)})

    @transition(field=status, source="borderline", target="moderation_complete",
                conditions=[no_moderator_conflict, has_justification])
    def complete_moderation(self):
        """Human moderator confirmed mark."""
        from apps.marking.services import check_reconciliation_needed
        check_reconciliation_needed(self)
        from shared.events import publish
        publish("ModerationComplete", {"script_id": str(self.id)})

    @transition(field=status, source=["moderation_complete", "ai_complete"],
                target="reconciliation", conditions=[reconciliation_required])
    def escalate_to_reconciliation(self):
        """AI vs human disagreement beyond threshold — second marker assigned."""
        from shared.events import publish
        publish("ReconciliationOpened", {"script_id": str(self.id)})

    @transition(field=status,
                source=["reconciliation", "moderation_complete", "ai_complete"],
                target="final_mark_locked")
    def lock_final_mark(self):
        """Final mark locked. Recomputes audit hash for pre-publication verification."""
        from apps.marking.services import recompute_audit_hash
        recompute_audit_hash(self)
        from shared.events import publish
        publish("FinalMarkLocked", {"script_id": str(self.id)})


class MarkingDecision(models.Model):
    """
    All marking data for a script — AI and human.
    Reference: NBES Architecture §7.1 — marking_decisions schema
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.OneToOneField(Script, on_delete=models.CASCADE, related_name="marking_decision")
    # AI scoring fields
    ai_mark = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    ai_confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    ai_model_version = models.CharField(max_length=50, blank=True)
    ai_rubric_breakdown = models.JSONField(null=True, blank=True)
    # Human moderation fields
    moderator_id = models.UUIDField(null=True, blank=True)
    moderator_mark = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    justification = models.TextField(blank=True)  # ≥30 words if moderator adjusted AI mark
    # Reconciliation fields
    second_marker_id = models.UUIDField(null=True, blank=True)
    second_mark = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    arbitration_outcome = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    arbitration_rationale = models.TextField(blank=True)
    # Final mark — canonical value after final_mark_locked
    final_mark = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    audit_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "marking_markingdecision"


class DoubleMarkSample(models.Model):
    """Random double-marking sample — 5% of scripts per sitting."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.OneToOneField(Script, on_delete=models.PROTECT, related_name="double_mark")
    sitting_ref = models.CharField(max_length=15)
    second_marker_id = models.UUIDField()
    sampled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "marking_doublemarksample"
