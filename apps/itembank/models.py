"""apps/itembank/models.py — Item Bank with AES-256 vault and FSM workflow."""
import uuid
from django.db import models
from django_fsm import FSMField, transition
from workflow.guards import (
    has_mandatory_metadata, has_valid_mcq_config, has_reviewer_assigned,
    has_sufficient_panel_votes, no_active_conflict, is_moderation_panel_member,
)


class Item(models.Model):
    """
    Exam item. Content stored AES-256-GCM encrypted in vault.
    Status transitions enforced by django-fsm.
    Reference: NBES Architecture §3.2 — Item Bank Workflow States
    """
    class Type(models.TextChoices):
        MCQ = "mcq", "Multiple Choice"
        MULTIPLE_RESPONSE = "multiple_response", "Multiple Response"
        SHORT_ANSWER = "short_answer", "Short Answer"
        ESSAY = "essay", "Essay"
        PRACTICAL = "practical", "Practical"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField(default=1)
    type = models.CharField(max_length=30, choices=Type.choices)
    # Vault fields — populated by shared.vault.encrypt_item()
    content_encrypted = models.BinaryField(null=True, blank=True)
    vault_nonce = models.BinaryField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    # Metadata: subject, topic, difficulty, cognitive_level, marks, time, etc.
    metadata = models.JSONField(default=dict)
    # FSM status field — ONLY transition methods may change this
    status = FSMField(default="draft", protected=True)
    author_id = models.UUIDField()  # keycloak_sub of item writer
    reviewer_id = models.UUIDField(null=True, blank=True)
    audit_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "itembank_item"

    def __str__(self):
        return f"Item {self.id} [{self.type}] — {self.status}"

    # ── FSM Transitions ────────────────────────────────────────────────────────

    @transition(field=status, source="draft", target="submitted",
                conditions=[has_mandatory_metadata, has_valid_mcq_config])
    def submit(self):
        """Item Writer submits for peer review."""
        from shared.events import publish
        publish("ItemSubmitted", {"item_id": str(self.id)})

    @transition(field=status, source="submitted", target="in_review",
                conditions=[has_reviewer_assigned])
    def assign_for_review(self):
        """Moderator assigned — review window opens."""
        pass

    @transition(field=status, source="in_review", target="reviewed")
    def submit_review(self):
        """Reviewer submits feedback."""
        from shared.events import publish
        publish("ItemReviewed", {"item_id": str(self.id)})

    @transition(field=status, source="reviewed", target="revised")
    def resubmit(self):
        """Item Writer addresses reviewer feedback and resubmits."""
        pass

    @transition(field=status, source=["reviewed", "revised"], target="moderation_panel",
                conditions=[is_moderation_panel_member])
    def send_to_panel(self):
        """Routed to Moderation Panel for final decision."""
        from shared.events import publish
        publish("ItemSentToPanel", {"item_id": str(self.id)})

    @transition(field=status, source="moderation_panel", target="approved",
                conditions=[has_sufficient_panel_votes, no_active_conflict])
    def approve(self):
        """2 of 3 panellists voted Approve."""
        from shared.events import publish
        publish("ItemApproved", {"item_id": str(self.id)})

    @transition(field=status, source="moderation_panel", target="rejected")
    def reject(self):
        """2 of 3 panellists voted Reject."""
        from shared.events import publish
        publish("ItemRejected", {"item_id": str(self.id)})

    @transition(field=status, source="approved", target="locked_for_use")
    def lock(self):
        """Auto-called after approval — item eligible for paper construction."""
        pass


class ItemVersion(models.Model):
    """Version history for every item save."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    snapshot = models.JSONField()  # full item data at this version
    changed_by_id = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "itembank_itemversion"
        unique_together = [("item", "version")]
        ordering = ["-version"]


class ExamPaper(models.Model):
    """Constructed exam paper for a sitting + subject."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting_ref = models.CharField(max_length=15)  # FK to sitting.Sitting by ref
    subject = models.CharField(max_length=100)
    blueprint_validated = models.BooleanField(default=False)
    validated_at = models.DateTimeField(null=True, blank=True)
    validated_by_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "itembank_exampaper"
        unique_together = [("sitting_ref", "subject")]


class ItemUsage(models.Model):
    """Tracks which sittings used each item — for quality monitoring."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="usages")
    paper = models.ForeignKey(ExamPaper, on_delete=models.PROTECT, related_name="item_usages")
    sitting_ref = models.CharField(max_length=15)
    used_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "itembank_itemusage"
