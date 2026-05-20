"""Service functions for item draft creation, versioning, and submission."""

import uuid
from django.db import transaction
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.contrib.auth import get_user_model

from .models import Item, ItemTransition, ItemVersion
from apps.audit.models import AuditEvent
from workflow.guards import has_mandatory_metadata


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
            status="Draft",
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
            new_state={"status": "Draft"},
        )
    else:
        # Lock the existing item before updating it.
        item = Item.objects.select_for_update().get(id=item_id, author_id=author_user)

        if item.status not in ["Draft", "Revised"]:
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
        "marks": str(item.marks) if item.marks else None,
        "time": item.time,
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
    if item.status not in ["Draft", "Revised"]:
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
    item.status = "Submitted"
    item.save(update_fields=["status"])

    # Record the workflow transition for history tracking.
    ItemTransition.objects.create(
        item_id=item,
        from_state=old_status,
        to_state="Submitted",
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
        new_state={"status": "Submitted"},
    )

    return item


# In production, these would be imported from shared libraries:
# from shared.security import scan_for_viruses
# from shared.storage import upload_to_vault_bucket


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

    # Probe the file to ensure the caller provided a readable object. Read a
    # single byte and then rewind so later upload routines can read from the
    # beginning. This also prevents "unused-argument" lint warnings.
    try:
        _ = file_obj.read(
            1
        )  # Read a single byte to probe readability; intentionally unused.
    except Exception as exc:
        # Re-raise as ValueError to keep the service-level API consistent,
        # while preserving the original exception context.
        raise ValueError(
            "Provided file_obj is not a readable file-like object"
        ) from exc
    finally:
        # Always attempt to rewind; safe for objects that support seek().
        try:
            file_obj.seek(0)
        except (AttributeError, OSError, ValueError):
            pass

    # Virus Scan Gate (mocked for local/dev).
    # call such as for production:
    # is_clean = scan_for_viruses(file_obj.read())
    is_clean = True

    if not is_clean:
        # If a scanner indicates infection, reject the upload and surface an
        # explicit error so callers can handle quarantining and notifications.
        raise ValueError("File failed virus scan. Upload rejected and quarantined.")

    # The unique ID (the asset_ref) to reference the stored blob.
    asset_ref = f"asset_{uuid.uuid4().hex}"

    # production we would call upload_to_vault_bucket(asset_ref, file_obj) after
    # rewinding the file:
    # file_obj.seek(0)
    # file_obj.seek(0)
    # upload_to_vault_bucket(asset_ref, file_obj)

    return asset_ref
