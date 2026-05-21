"""Service functions for item draft creation, versioning, and submission."""

import logging
import shutil
import subprocess
import tempfile
import uuid
import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from shared.storage import get_storage_backend

from .tasks import dispatch_item_status_notification

from .models import (
    Item,
    ItemTransition,
    ItemVersion,
    ItemComment,
    PanelVote,
    VaultExportRequest,
)
from apps.audit.models import AuditEvent
from workflow.guards import has_mandatory_metadata

logger = logging.getLogger(__name__)


@transaction.atomic
def create_or_update_item_draft(
    data: dict, author_auth: dict, item_id: str = None
) -> Item:
    """
    Create a new draft item or auto-save an existing item by creating a new version.

    Args:
        data: Item metadata and version content payload.
        author_auth: Auth payload containing the author's subject identifier.
        item_id: Existing item identifier for auto-save updates.

    Returns:
        The saved Item instance.
    """
    # Extract version-specific data that belongs on ItemVersion, not Item.
    content = data.pop("content", None)
    # Use None as the sentinel so omitted asset_refs do not wipe existing refs.
    asset_refs = data.pop("asset_refs", None)

    # Resolve author to a local User model instance (maps Keycloak `sub` to user.pk).
    User = get_user_model()
    try:
        author_user = User.objects.get(keycloak_sub=author_auth["sub"])
    except ObjectDoesNotExist as exc:
        raise ObjectDoesNotExist("Author user not found for provided auth sub") from exc

    if not item_id:
        # Create a brand new draft item.
        item = Item.objects.create(
            author_id=author_user,
            status=Item.Status.DRAFT,
            **data,
        )
        version_no = 1

        # Audit the initial draft creation so the item lifecycle starts with a
        # tamper-evident event in the platform audit log.
        AuditEvent.record(
            actor_id=author_auth["sub"],
            action="ITEM_DRAFT_CREATED",
            entity_type="item",
            entity_id=str(item.id),
            old_state=None,
            new_state={"status": Item.Status.DRAFT},
        )
    else:
        # Lock the existing item before updating it.
        item = Item.objects.select_for_update().get(id=item_id, author_id=author_user)

        if item.status not in [Item.Status.DRAFT, Item.Status.REVISED]:
            raise ValueError("You can only auto-save items in Draft or Revised states.")

        # Determine the next version number.
        last_version = item.versions.order_by("-version_no").first()
        version_no = last_version.version_no + 1 if last_version else 1

        if content is None:
            content = last_version.content if last_version else ""

        # If asset_refs omitted in an autosave, preserve the previous version refs.
        if asset_refs is None:
            asset_refs = last_version.asset_refs if last_version else []

        # Update the item-level metadata.
        for key, value in data.items():
            setattr(item, key, value)
        item.save()

    # Capture a metadata snapshot for the version record.
    metadata_snapshot = {
        "subject": item.subject,
        "topic": item.topic,
        "cognitive_level": item.cognitive_level,
        "difficulty": item.difficulty,
        "marks": str(item.marks) if item.marks is not None else None,
        "time": item.time,
        "source": item.source,
        "blueprint_ref": item.blueprint_ref,
    }

    # For new items, default asset_refs to empty list if none provided.
    if asset_refs is None:
        asset_refs = []

    new_version = ItemVersion.objects.create(
        item_id=item,
        version_no=version_no,
        content=content,
        metadata_snapshot=metadata_snapshot,
        asset_refs=asset_refs,
        saved_by=author_user,
    )

    # Link the item to the newly created active version.
    item.current_version_id = new_version.id
    item.save(update_fields=["current_version_id"])

    return item


@transaction.atomic
def submit_item_for_review(item_id: str, author_auth: dict) -> Item:
    """
    Submit a draft item for peer review after validating state and metadata.

    Args:
        item_id: The item identifier to submit.
        author_auth: Auth payload containing the author's subject identifier.

    Returns:
        The submitted Item instance.
    """
    # Resolve author to a local User model instance (maps Keycloak `sub` to user.pk).
    User = get_user_model()
    try:
        author_user = User.objects.get(keycloak_sub=author_auth["sub"])
    except ObjectDoesNotExist as exc:
        raise ObjectDoesNotExist("Author user not found for provided auth sub") from exc

    # Fetch and lock the item row for a safe state transition.
    item = Item.objects.select_for_update().get(id=item_id, author_id=author_user)

    # Ensure only draft-like states can be submitted.
    if item.status not in [Item.Status.DRAFT, Item.Status.REVISED]:
        raise ValidationError(
            f"Cannot submit an item currently in state: {item.status}"
        )

    # Require all mandatory metadata before submission.
    if not has_mandatory_metadata(item):
        raise ValidationError(
            "All mandatory metadata fields (subject, topic, cognitive level, difficulty, "
            "time, marks, source, syllabus reference) must be completed before submission."
        )

    old_status = item.status

    # Transition the item into the submitted state.
    item.status = Item.Status.SUBMITTED
    item.save(update_fields=["status"])

    # Record the workflow transition for history tracking.
    ItemTransition.objects.create(
        item_id=item,
        from_state=old_status,
        to_state=Item.Status.SUBMITTED,
        actor_id=author_user,
        justification="Item submitted for peer review",
    )

    # Record the system audit event.
    AuditEvent.record(
        actor_id=author_auth["sub"],
        action="ITEM_SUBMITTED",
        entity_type="item",
        entity_id=str(item.id),
        old_state={"status": old_status},
        new_state={"status": Item.Status.SUBMITTED},
    )

    return item


def scan_for_viruses(blob: bytes) -> bool:
    """Run a best-effort ClamAV scan against an uploaded blob."""

    clamscan = shutil.which("clamscan")
    if not clamscan:
        raise RuntimeError("Virus scanner unavailable: clamscan binary was not found.")

    with tempfile.NamedTemporaryFile(suffix=".upload", delete=True) as temp_file:
        temp_file.write(blob)
        temp_file.flush()
        try:
            completed = subprocess.run(
                [clamscan, "--no-summary", temp_file.name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error("Virus scan timeout for temp file %s: %s", temp_file.name, exc)
            # Treat scan timeout as a failed (non-clean) result to be conservative
            return False

    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False

    raise RuntimeError(
        completed.stderr.strip() or completed.stdout.strip() or "Virus scan failed."
    )


def upload_to_vault_bucket(asset_ref: str, file_obj) -> str:
    """Persist an uploaded asset to the configured storage backend."""

    storage_backend = get_storage_backend()
    return storage_backend.save(asset_ref, File(file_obj))


def process_asset_upload(file_obj) -> str:
    """Process an uploaded asset file: virus-scan, store in vault, and return asset_ref.

    This is a lightweight, documented implementation used in development. In
    production the ClamAV scanner and object-storage helpers should be used by
    importing shared.security.scan_for_viruses and
    shared.storage.upload_to_vault_bucket.

    Steps performed:
    1. Minimal probe of the incoming file-like object to ensure it is readable
       (this also prevents the function from appearing unused by linters).
    2. (Placeholder) Run virus-scan logic. Currently mocked to pass.
    3. Generate a stable unique asset reference string.
    4. (Placeholder) Upload the file object to the blob vault.

    Args:
        file_obj: A file-like object (must support read() and seek()).

    Returns:
        A unique asset reference string to be stored on the ItemVersion.
    """

    # Probe the file to ensure the caller provided a readable object, then scan
    # and persist the upload before returning a vault reference.
    try:
        file_bytes = file_obj.read()
    except Exception as exc:
        raise ValueError(
            "Provided file_obj is not a readable file-like object"
        ) from exc

    # Ensure we have a fresh, seekable stream. If the original file-like
    # object does not support seek, recreate a BytesIO from the raw bytes.
    try:
        file_obj.seek(0)
    except (AttributeError, OSError, ValueError):
        file_obj = io.BytesIO(file_bytes)

    is_clean = scan_for_viruses(file_bytes)

    # Rewind or recreate the stream before upload to ensure the upload sees
    # the full content at position 0.
    try:
        file_obj.seek(0)
    except (AttributeError, OSError, ValueError):
        file_obj = io.BytesIO(file_bytes)

    if not is_clean:
        raise ValueError("File failed virus scan. Upload rejected and quarantined.")

    asset_ref = f"asset_{uuid.uuid4().hex}"

    try:
        upload_to_vault_bucket(asset_ref, file_obj)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to upload asset {asset_ref} to vault storage."
        ) from exc

    return asset_ref


@transaction.atomic
def restore_item_version(item_id: str, version_id: str, actor_auth: dict) -> Item:
    """Restore a previous item version by creating a new version copy.

    This is a non-destructive restore operation: the historical
    ItemVersion identified by ``version_id`` is copied into a brand new
    ItemVersion record. The item's metadata fields are reverted to the
    values recorded in the historical version's ``metadata_snapshot`` and
    the item's ``current_version_id`` is updated to point to the newly
    created version.

    Preconditions / Validation:
    - The caller (``actor_auth["sub"]``) must be the item's assigned
      author.
    - The item must be in a state that allows restores ("Draft" or
      "Revised").

    Side effects:
    - Creates a new ItemVersion instance.
    - Updates and saves the Item instance.
    - Emits an AuditEvent recording the restore operation.

    Args:
        item_id: UUID/PK of the Item to act on.
        version_id: UUID/PK of the historical ItemVersion to copy.
        actor_auth: Auth information for the acting user; expects a
            "sub" key containing the user id.

    Returns:
        The updated Item instance (with current_version_id set to the
        newly created version).

    Raises:
        ValueError: If validation fails or the requested historical
            version does not exist.
    """
    User = get_user_model()
    try:
        resolved_user = User.objects.get(keycloak_sub=actor_auth["sub"])
    except ObjectDoesNotExist as exc:
        raise ValueError("Author user not found for provided auth sub") from exc

    try:
        item = Item.objects.select_for_update().get(id=item_id)
    except ObjectDoesNotExist as exc:
        raise ValueError("Item not found.") from exc

    # Validation Constraints
    if item.author_id_id != resolved_user.id:
        raise ValueError("Only the assigned author can restore item versions.")
    if item.status not in [Item.Status.DRAFT, Item.Status.REVISED]:
        raise ValueError(
            f"Cannot restore versions while item is in {item.status} state."
        )

    # Fetch historical snapshot
    try:
        historical_version = item.versions.get(id=version_id)
    except ObjectDoesNotExist as exc:
        raise ValueError("The requested version does not exist for this item.") from exc

    last_version = item.versions.order_by("-version_no").first()
    new_version_no = last_version.version_no + 1 if last_version else 1

    # Create the new version by perfectly copying the historical one
    new_version = ItemVersion.objects.create(
        item_id=item,
        version_no=new_version_no,
        content=historical_version.content,
        metadata_snapshot=historical_version.metadata_snapshot,
        asset_refs=historical_version.asset_refs,
        saved_by=resolved_user,
    )

    # Revert Item metadata to match the restored snapshot
    snapshot = historical_version.metadata_snapshot
    item.current_version_id = new_version.id
    item.subject = snapshot.get("subject", item.subject)
    item.topic = snapshot.get("topic", item.topic)
    item.cognitive_level = snapshot.get("cognitive_level", item.cognitive_level)
    item.difficulty = snapshot.get("difficulty", item.difficulty)
    item.time = snapshot.get("time", item.time)
    item.source = snapshot.get("source", item.source)
    item.blueprint_ref = snapshot.get("blueprint_ref", item.blueprint_ref)

    marks_val = snapshot.get("marks")
    item.marks = float(marks_val) if marks_val else None

    item.save()

    AuditEvent.record(
        actor_id=actor_auth["sub"],
        action="ITEM_VERSION_RESTORED",
        entity_type="item",
        entity_id=str(item.id),
        new_state={
            "restored_to_version": historical_version.version_no,
            "new_version_no": new_version_no,
        },
    )

    return item


@transaction.atomic
def process_suggestion_decision(
    item_id: str, suggestion_id: str, data: dict, actor_auth: dict
) -> dict:
    """Process a decision on an inline suggestion (accept or decline).

    The system stores inline suggestions as ItemComment records. This
    function resolves a suggestion by marking it "resolved" and
    optionally creates a rationale reply record also stored as an
    ItemComment (linked via the ``anchor_path``).

    Validation rules:
    - Only the Item's assigned author may accept or decline suggestions.
    - Suggestions may only be processed while the item is in
      "In Review" or "Revised" states.
    - Suggestions that are already resolved cannot be processed again.

    Args:
        item_id: UUID/PK of the Item being acted on.
        suggestion_id: UUID/PK of the ItemComment representing the
            suggestion.
        data: Dictionary containing the decision payload. Expected keys
            include "decision" (e.g. "accept" or "decline") and
            optionally "rationale" (a free-text explanation).
        actor_auth: Auth information for the acting user; expects a
            "sub" key containing the user id.

    Returns:
        A dict summarising the outcome containing the suggestion id,
        its updated status, and the id of any rationale reply that was
        created.

    Raises:
        ValueError: If validation fails or the suggestion cannot be
            located.
    """
    User = get_user_model()
    try:
        resolved_user = User.objects.get(keycloak_sub=actor_auth["sub"])
    except ObjectDoesNotExist as exc:
        raise ValueError("Author user not found for provided auth sub") from exc

    try:
        item = Item.objects.select_for_update().get(id=item_id)
    except ObjectDoesNotExist as exc:
        raise ValueError("Item not found.") from exc

    try:
        suggestion = ItemComment.objects.get(
            id=suggestion_id, item_version_id__item_id=item
        )
    except ObjectDoesNotExist as exc:
        raise ValueError("Suggestion not found.") from exc

    # RBAC/State Validation
    if item.author_id_id != resolved_user.id:
        raise ValueError("Only the Item Writer can accept or decline suggestions.")
    if item.status not in [Item.Status.IN_REVIEW, Item.Status.REVISED]:
        raise ValueError(
            f"Cannot process suggestions while item is in {item.status} state."
        )
    if suggestion.status == "resolved":
        raise ValueError("This suggestion has already been resolved.")

    suggestion.status = "resolved"
    suggestion.save(update_fields=["status"])

    # using the ItemComment table, leveraging the anchor_path to link it(If declined (or if a rationale was provided for an accept))
    rationale_record = None
    if data.get("rationale"):
        rationale_record = ItemComment.objects.create(
            item_version_id=suggestion.item_version_id,
            anchor_path=f"reply_to_{suggestion.id}",  # Links the rationale to the original suggestion
            body=f"[{data['decision'].upper()}] Rationale: {data['rationale']}",
            status="resolved",  # Replies are born resolved so they don't clutter the open queue
            created_by=resolved_user,
        )

    AuditEvent.record(
        actor_id=actor_auth["sub"],
        action=f"SUGGESTION_{data['decision'].upper()}",
        entity_type="item_comment",
        entity_id=str(suggestion.id),
        new_state={"rationale_provided": bool(data.get("rationale"))},
    )

    return {
        "suggestion_id": str(suggestion.id),
        "status": suggestion.status,
        "rationale_id": str(rationale_record.id) if rationale_record else None,
    }


def check_and_escalate_overdue_reviews() -> int:
    """Find and escalate items that have exceeded the review SLA.

    This routine computes a cutoff datetime representing 5 business days
    prior to now (weekdays only), then scans items currently in the
    "In Review" status. For each item it determines when the item
    entered the "In Review" state by inspecting recorded transitions;
    if that timestamp is older than the cutoff the item is considered
    to have breached the SLA. Each breach is logged and an AuditEvent
    is recorded. The function returns the total number of escalated
    items.

    Returns:
        int: Number of items escalated due to SLA breach.
    """
    # 5 business days by counting weekdays backward.
    sla_cutoff = timezone.now()
    business_days = 0

    while business_days < 5:
        sla_cutoff -= timedelta(days=1)
        if sla_cutoff.weekday() < 5:
            business_days += 1

    # Only look at items currently stuck in the review queue
    stuck_items = Item.objects.filter(status=Item.Status.IN_REVIEW)
    escalated_count = 0

    for item in stuck_items:
        # Find the exact moment this item entered the 'In Review' state
        entry_transition = (
            item.transitions.filter(to_state=Item.Status.IN_REVIEW)
            .order_by("-occurred_at")
            .first()
        )

        if entry_transition and entry_transition.occurred_at < sla_cutoff:
            # The SLA is breached.
            # 1. Log the breach for the system administrators
            logger.warning(
                "SLA BREACH: Item %s has been In Review since %s.",
                item.id,
                entry_transition.occurred_at,
            )

            # Avoid duplicate SLA escalation audit events within the recent window
            recent_since = timezone.now() - timedelta(days=1)
            already_recorded = AuditEvent.objects.filter(
                actor_id="SYSTEM",
                action="SLA_ESCALATION_TRIGGERED",
                entity_type="item",
                entity_id=str(item.id),
                created_at__gte=recent_since,
            ).exists()

            if not already_recorded:
                AuditEvent.record(
                    actor_id="SYSTEM",  # Triggered by the server, not a user
                    action="SLA_ESCALATION_TRIGGERED",
                    entity_type="item",
                    entity_id=str(item.id),
                    new_state={
                        "days_overdue": (
                            timezone.now() - entry_transition.occurred_at
                        ).days
                    },
                )

                escalated_count += 1

    return escalated_count


@transaction.atomic
def register_panel_vote(
    item_id: str, panellist_id: str, vote_type: str, justification: str
) -> Item:
    """
    Casts an item panel vote, checks word thresholds,
    and evaluates consensus state changes automatically.
    """
    item = Item.objects.select_for_update().get(id=item_id)

    if item.status != Item.Status.MODERATION_PANEL:
        raise ValueError(
            f"Item is not in the moderation phase. Current status: {item.status}"
        )

    # Enforce word limit constraint
    word_count = len(justification.split())
    if word_count < 30:
        raise ValueError(
            f"Justification narrative must be at least 30 words. Current count: {word_count}"
        )

    # Prevent duplicate panellist votes (unique_together enforced at DB level).
    if PanelVote.objects.filter(item_id=item, panellist_id=panellist_id).exists():
        raise ValueError("This panellist has already voted on this item.")

    # Save the vote
    PanelVote.objects.create(
        item_id=item,
        panellist_id=panellist_id,
        vote=vote_type,
        justification=justification,
    )

    # Evaluate current matching votes
    votes = item.panel_votes.all()
    approve_count = votes.filter(vote="Approve").count()
    reject_count = votes.filter(vote="Reject").count()

    # Consensus rules (Default 2 out of 3 match wins)
    if approve_count >= 2:
        # Record the SRS-defined approval step before auto-locking the item for use.
        previous_status = item.status
        item.status = Item.Status.APPROVED
        item.save(update_fields=["status"])

        ItemTransition.objects.create(
            item_id=item,
            from_state=previous_status,
            to_state=Item.Status.APPROVED,
            actor_id=panellist_id,
            justification=justification,
        )

        item.status = Item.Status.LOCKED_FOR_USE
        item.save(update_fields=["status"])

        ItemTransition.objects.create(
            item_id=item,
            from_state=Item.Status.APPROVED,
            to_state=Item.Status.LOCKED_FOR_USE,
            actor_id=panellist_id,
            justification="Item approved and automatically locked for use",
        )

        # Fire async notification only after the transaction commits.
        transaction.on_commit(
            lambda item_id=str(item.id), author_id=str(item.author_id_id): (
                dispatch_item_status_notification.delay(
                    item_id,
                    author_id,
                    Item.Status.LOCKED_FOR_USE,
                )
            )
        )

        # Log forensic snapshot to security audit
        AuditEvent.record(
            actor_id="SYSTEM",
            action="ITEM_CONSENSUAL_APPROVAL",
            entity_type="item",
            entity_id=str(item.id),
            new_state={
                "status": Item.Status.LOCKED_FOR_USE,
                "approvers": [
                    str(v.panellist_id) for v in votes.filter(vote="Approve")
                ],
            },
        )
    elif reject_count >= 2:
        previous_status = item.status
        item.status = Item.Status.REJECTED
        item.save(update_fields=["status"])

        # Record workflow transition for the consensual rejection
        ItemTransition.objects.create(
            item_id=item,
            from_state=previous_status,
            to_state=Item.Status.REJECTED,
            actor_id=panellist_id,
            justification="Consensual panel rejection",
        )

        # Consolidate rejection rationale for the 5-minute notification trigger
        consolidated_rationale = [v.justification for v in votes.filter(vote="Reject")]
        transaction.on_commit(
            lambda item_id=str(item.id), author_id=str(item.author_id_id), rationales=consolidated_rationale: (
                dispatch_item_status_notification.delay(
                    item_id,
                    author_id,
                    Item.Status.REJECTED,
                    rationales=rationales,
                )
            )
        )

        AuditEvent.record(
            actor_id="SYSTEM",
            action="ITEM_CONSENSUAL_REJECTION",
            entity_type="item",
            entity_id=str(item.id),
            new_state={"status": Item.Status.REJECTED},
        )

    return item


@transaction.atomic
def execute_vault_cosign(request_id: str, cosigner_id: str) -> VaultExportRequest:
    """
    Co-signs and executes an export request with strict anti-circumvention validations.
    """
    User = get_user_model()
    try:
        cosigner_user = User.objects.get(keycloak_sub=cosigner_id)
    except ObjectDoesNotExist as exc:
        raise ValueError("Cosigner user not found for provided auth sub.") from exc

    req = VaultExportRequest.objects.select_for_update().get(id=request_id)

    if req.status != "Pending":
        raise ValueError(
            f"This export request is no longer active. Status: {req.status}"
        )
    if timezone.now() > req.expires_at:
        req.status = "Expired"
        req.save()
        raise ValueError("The 72-hour validation window for this request has expired.")

    # Prevent self-signing anti-circumvention rule
    if req.requester_id_id == cosigner_user.id:
        raise ValueError(
            "Security Boundary Exception: Initiating officer cannot act as the co-signing authoriser."
        )

    # Execute request
    req.cosigner_id = cosigner_user
    req.status = "Executed"
    req.save()

    # Record the actual physical vault access log line
    AuditEvent.record(
        actor_id=str(cosigner_id),
        action="VAULT_CONTENT_EXPORTED",
        entity_type="vault",
        entity_id=str(req.id),
        new_state={"scope_length": len(req.scope)},
    )

    return req
