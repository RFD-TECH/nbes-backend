"""apps/results/models.py — Results, Board Ratification and Publication with FSM."""
import uuid
from django.db import models
from django_fsm import FSMField, transition
from workflow.guards import dg_signoff_recorded


class ResultSet(models.Model):
    """
    Full results for a sitting. FSM enforces normalise → ratify → publish.
    Board ratification managed by django-viewflow (BoardRatificationFlow).
    Reference: NBES Architecture §3.5 — Results Publication Workflow States
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting_ref = models.CharField(max_length=15, unique=True)
    status = FSMField(default="drafted", protected=True)
    normalisation_complete = models.BooleanField(default=False)
    ratification_ref = models.UUIDField(null=True, blank=True)
    dg_signoff_ref = models.TextField(blank=True)
    signed_pdf_ref = models.TextField(blank=True)
    publication_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "results_resultset"

    def __str__(self):
        return f"ResultSet {self.sitting_ref} [{self.status}]"

    # ── FSM Transitions ────────────────────────────────────────────────────────

    @transition(field=status, source="drafted", target="normalised",
                conditions=[normalisation_complete])
    def mark_normalised(self):
        from shared.events import publish
        publish("ResultsNormalised", {"sitting_ref": self.sitting_ref})

    @transition(field=status, source="normalised", target="board_review")
    def open_board_review(self):
        """Triggers django-viewflow Board ratification process."""
        from workflow.viewflow.ratification import BoardRatificationFlow
        BoardRatificationFlow.start(result_set=self)

    @transition(field=status, source="board_review", target="board_ratified")
    def complete_ratification(self):
        """Called by viewflow on quorum reached + Chair signature."""
        from shared.events import publish
        publish("BoardRatified", {"sitting_ref": self.sitting_ref})

    @transition(field=status, source="board_ratified", target="ready_to_publish",
                conditions=[dg_signoff_recorded])
    def dg_signoff(self):
        pass

    @transition(field=status, source="ready_to_publish", target="published")
    def publish(self):
        """Verifies hash chain, generates PDFs, publishes results."""
        from apps.results.services import verify_hash_chain, generate_result_pdfs
        verify_hash_chain(self)        # Raises if any hash fails — blocks publication
        generate_result_pdfs(self)
        from shared.events import publish as pub
        pub("ResultsPublished", {"sitting_ref": self.sitting_ref})


class NormalisedResult(models.Model):
    """Per-candidate normalised result for a sitting."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    result_set = models.ForeignKey(ResultSet, on_delete=models.CASCADE, related_name="results")
    candidate = models.ForeignKey(
        "registration.Candidate", on_delete=models.PROTECT, related_name="normalised_results"
    )
    paper_marks = models.JSONField(default=dict)       # {paper_id: raw_mark}
    normalised_marks = models.JSONField(default=dict)  # {paper_id: normalised_mark}
    overall_outcome = models.CharField(
        max_length=20,
        choices=[("pass", "PASS"), ("fail", "FAIL"), ("withheld", "WITHHELD")],
        null=True, blank=True
    )
    signed_pdf_ref = models.TextField(blank=True)

    class Meta:
        db_table = "results_normalisedresult"
        unique_together = [("result_set", "candidate")]


class RatificationRecord(models.Model):
    """
    Immutable Board ratification record.
    is_immutable=True after Chair signature — DB trigger prevents further update.
    Reference: NBES Architecture §7.1 — results & ratification schema
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    result_set = models.OneToOneField(ResultSet, on_delete=models.PROTECT, related_name="ratification")
    date = models.DateTimeField(auto_now_add=True)
    attendees = models.JSONField(default=list)       # list of keycloak_sub UUIDs
    votes = models.JSONField(default=dict)           # {keycloak_sub: {vote, justification}}
    rationale = models.TextField(blank=True)
    chair_signature_ref = models.TextField(blank=True)
    signed_minutes_ref = models.TextField(blank=True)
    is_immutable = models.BooleanField(default=False)

    class Meta:
        db_table = "results_ratificationrecord"


class RemarkRequest(models.Model):
    """Remarking and verification request."""
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ASSIGNED = "assigned", "Assigned"
        COMPLETE = "complete", "Complete"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        "registration.Candidate", on_delete=models.PROTECT, related_name="remark_requests"
    )
    sitting_ref = models.CharField(max_length=15)
    subject_paper_id = models.UUIDField()
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    assigned_examiner_id = models.UUIDField(null=True, blank=True)
    outcome = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "results_remarkrequest"
