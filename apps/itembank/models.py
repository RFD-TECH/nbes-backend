"""Itembank models.

This module defines the database models used by the itembank
application: Item, ItemVersion, ItemComment, ItemTransition,
PanelVote, VaultAccess, VaultExportRequest, Paper and ItemUsage.

Each model represents a persisted entity used for managing
assessment items, versions, comments, transitions, panel votes
and vault access/export auditing.
"""

import uuid
from django.db import models
from django.conf import settings


class Item(models.Model):
    """Represents an assessment item.

    Fields
    - id: UUID primary key for the item.
    - current_version_id: optional UUID pointing to the active ItemVersion.
    - status: workflow status of the item.
    - blueprint_ref: optional reference to a blueprint entry.
    - subject, topic: classification fields.
    - difficulty, cognitive_level: metadata for item tagging.
    - marks: numeric marks assigned to the item.
    - time: suggested time (in seconds) for the item.
    - source: optional provenance information.
    - author_id: reference to the user who authored the item.
    - audit_hash: optional hash used for auditing integrity.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    current_version_id = models.UUIDField(
        null=True,
        blank=True,
    )  # Will point to item_version.id
    status = models.CharField(max_length=50)
    blueprint_ref = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )
    subject = models.CharField(max_length=255, null=True, blank=True)
    topic = models.CharField(max_length=255, null=True, blank=True)
    difficulty = models.CharField(max_length=50, null=True, blank=True)
    cognitive_level = models.CharField(max_length=50, null=True, blank=True)
    marks = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    time = models.IntegerField(null=True, blank=True)
    source = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )
    author_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        related_name="authored_items",
    )
    audit_hash = models.CharField(max_length=256, null=True, blank=True)


class ItemVersion(models.Model):
    """Stores a historical version of an Item.

    - item_id: FK to the Item this version belongs to.
    - version_no: incrementing integer version number.
    - content: textual or serialized item content.
    - metadata_snapshot: JSON snapshot of item metadata at save time.
    - asset_refs: list of referenced asset identifiers.
    - saved_by / saved_at: audit information for who saved it and when.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item_id = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_no = models.IntegerField()
    content = models.TextField()
    metadata_snapshot = models.JSONField()
    asset_refs = models.JSONField(default=list)  # Maps to asset_refs[]
    saved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING)
    saved_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["item_id", "version_no"],
                name="unique_item_version_per_item",
            )
        ]


class ItemComment(models.Model):
    """Comment anchored to a specific ItemVersion.

    - item_version_id: version the comment refers to.
    - anchor_path: location within the content where the comment applies.
    - body: comment text.
    - status: open or resolved.
    - created_by: user who created the comment.
    """

    STATUS_CHOICES = [("open", "Open"), ("resolved", "Resolved")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item_version_id = models.ForeignKey(
        ItemVersion,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    anchor_path = models.CharField(max_length=255)
    body = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="open",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING
    )


class ItemTransition(models.Model):
    """Records a state transition for an Item.

    - item_id: the item whose state changed.
    - from_state / to_state: previous and new workflow states.
    - actor_id: user who performed the transition.
    - justification: optional freeform reason.
    - occurred_at: timestamp of the transition.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item_id = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="transitions",
    )
    from_state = models.CharField(max_length=50)
    to_state = models.CharField(max_length=50)
    actor_id = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING)
    justification = models.TextField(null=True, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)


class PanelVote(models.Model):
    """A vote cast by a panellist regarding an Item.

    - item_id: referenced item.
    - panellist_id: user casting the vote.
    - vote: vote value (e.g. accept/reject/needs edits).
    - justification: panellist rationale.
    - voted_at: timestamp of vote.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item_id = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="panel_votes",
    )
    panellist_id = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING
    )
    vote = models.CharField(max_length=50)
    justification = models.TextField()
    voted_at = models.DateTimeField(auto_now_add=True)


class VaultAccess(models.Model):
    """Audit record for accesses to the secure vault.

    - item_id: item that was accessed.
    - actor_id: user who accessed the vault.
    - kind: type of access (read/export).
    - session_id, ip: optional context for the access.
    - occurred_at: timestamp of access.
    """

    KIND_CHOICES = [("read", "Read"), ("export", "Export")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item_id = models.ForeignKey(Item, on_delete=models.CASCADE)
    actor_id = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.DO_NOTHING)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    session_id = models.CharField(max_length=255, null=True, blank=True)
    ip = models.CharField(max_length=45, null=True, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)


class VaultExportRequest(models.Model):
    """Represents a request to export items from the vault.

    - scope: textual description of the export scope.
    - requester_id: user who requested the export.
    - cosigner_id: optional cosigner user.
    - status: current status of the request.
    - expires_at: expiry of the request.
    - created_at: timestamp when request was created.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=255)
    requester_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        related_name="requested_exports",
    )
    cosigner_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        related_name="cosigned_exports",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=50)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class Paper(models.Model):
    """Represents an assembled examination paper.

    - sitting_ref: external identifier for the sitting.
    - subject, mode: classification and delivery mode.
    - total_marks, time_limit: overall paper constraints.
    - item_ids: ordered list of item UUIDs included.
    - variants: optional variant definitions.
    - blueprint_ref: reference to the source blueprint used.
    - status: workflow status of the paper.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting_ref = models.CharField(max_length=255)
    subject = models.CharField(max_length=255)
    mode = models.CharField(max_length=50)
    total_marks = models.DecimalField(max_digits=6, decimal_places=2)
    time_limit = models.IntegerField()
    item_ids = models.JSONField(default=list)  # Maps to item_ids[]
    variants = models.JSONField(default=list)  # Maps to variants[]
    blueprint_ref = models.CharField(max_length=255)
    status = models.CharField(max_length=50)


class ItemUsage(models.Model):
    """Statistical usage record for an Item.

    - item_id: referenced item.
    - sitting_ref: identifier where the item was used.
    - count: number of times used in that sitting.
    - facility_index / discrimination_index: psychometric metrics when available.
    - recorded_at: when the usage was recorded.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )  # Adding standard Django PK
    item_id = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="usage_history",
    )
    sitting_ref = models.CharField(max_length=255)
    count = models.IntegerField()
    facility_index = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    discrimination_index = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    recorded_at = models.DateTimeField(auto_now_add=True)
