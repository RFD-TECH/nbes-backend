"""apps/committee/tasks.py — NBEC Committee Celery tasks.

Identity management belongs to IAM. NBES never calls the Keycloak Admin API
directly. When a tenure expires we mark the member Expired locally (so the
gateway's authorisation checks immediately refuse NBEC-gated actions for
that ``keycloak_sub``) and publish a ``MemberExpired`` event for IAM to
consume — IAM is responsible for revoking the user's NBEC client role.
"""
import datetime
import logging
from datetime import date, timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# SRS §2.2.4: default annual COI refresh cadence.
COI_REFRESH_INTERVAL_DAYS = 365


@shared_task(queue="sla-monitor")
def monitor_tenure_expiry():
    """Daily: expire NBECMember records whose ``tenure_end`` has passed.

    Runs every day at 00:30 UTC via Celery Beat (config/celery.py).
    For each expired member:
      1. DB status is set to Expired.
      2. An AuditEvent is recorded.
      3. A ``MemberExpired`` domain event is published so IAM can revoke
         the user's NBEC role grant within the 60-second window (REQ-F000-02).

    NBES does NOT call the Keycloak Admin API directly — that responsibility
    sits with IAM (AMS) per the system boundary.
    """
    from apps.audit.models import AuditEvent
    from shared.events import publish
    from . import events as ev
    from .models import NBECMember

    today = date.today()
    due = NBECMember.objects.filter(
        status=NBECMember.Status.ACTIVE,
        tenure_end__lt=today,
    )
    expired_count = 0
    for member in due:
        try:
            member.expire()

            AuditEvent.record(
                actor_id=None,
                action=ev.MEMBER_EXPIRED,
                entity_type="committee_member",
                entity_id=member.id,
                old_state={"status": "active"},
                new_state={"status": "expired", "tenure_end": str(member.tenure_end)},
            )

            # IAM listens for this event and revokes the user's NBEC role.
            publish("MemberExpired", {
                "member_id": str(member.id),
                "keycloak_sub": str(member.keycloak_sub),
                "designation": member.designation,
                "tenure_end": str(member.tenure_end),
            })
            expired_count += 1
        except Exception:
            logger.exception("Failed to expire member %s", member.id)

    if expired_count:
        logger.info("monitor_tenure_expiry: expired %d member(s)", expired_count)
    return {"expired": expired_count}


@shared_task(queue="sla-monitor")
def escalate_overdue_actions():
    """Daily: mark ActionItems as Overdue when due_date < today and still open.

    Runs every day at 01:30 UTC via Celery Beat (config/celery.py).
    Emits an audit event and a domain event for each escalation so that
    System 21 (Notifications) can send a reminder to the assignee.
    """
    from apps.audit.models import AuditEvent
    from shared.events import publish
    from . import events as ev
    from .models import ActionItem

    today = date.today()
    retry_cutoff = timezone.now() - datetime.timedelta(hours=24)
    overdue = ActionItem.objects.filter(due_date__lt=today).filter(
        Q(status__in=[ActionItem.Status.OPEN, ActionItem.Status.IN_PROGRESS])
        | Q(status=ActionItem.Status.OVERDUE, last_escalated_at__isnull=True)
        | Q(status=ActionItem.Status.OVERDUE, last_escalated_at__lt=retry_cutoff)
    )
    escalated_count = 0
    for item in overdue:
        try:
            item.status = ActionItem.Status.OVERDUE
            item.last_escalated_at = timezone.now()
            item.save(update_fields=["status", "last_escalated_at"])
            AuditEvent.record(
                actor_id=None,
                action=ev.ACTION_ITEM_ESCALATED,
                entity_type="action_item",
                entity_id=item.id,
                new_state={
                    "due_date": str(item.due_date),
                    "assigned_to_id": str(item.assigned_to_id),
                },
            )
            publish("ActionItemEscalated", {
                "action_item_id": str(item.id),
                "assigned_to_id": str(item.assigned_to_id),
                "due_date": str(item.due_date),
            })
            escalated_count += 1
        except Exception:
            logger.exception("Failed to escalate action item %s", item.id)

    if escalated_count:
        logger.info("escalate_overdue_actions: escalated %d action item(s)", escalated_count)
    return {"escalated": escalated_count}


# ── System 05 archive bridge (SRS §2.2.5) ─────────────────────────────────────

@shared_task(
    queue="sla-monitor",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=3600,           # cap exponential backoff at 1 hour
    retry_jitter=True,
    max_retries=20,                    # ~24h spread of retries
    retry_kwargs={"countdown": 60},
)
def archive_minutes_to_system05(self, minutes_id: str):
    """Archive a signed Minutes record to System 05.

    Triggered from ``apps.committee.services.sign_minutes`` via
    ``transaction.on_commit``. Retries with exponential backoff up to 24h
    on transient failures (SRS §2.2.5). Permanent rejection (4xx) is
    audit-logged as a critical event so the Administrator can intervene.
    """
    from apps.audit.models import AuditEvent
    from shared.events import publish
    from shared.integrations.system05 import System05Client, System05Error

    from . import events as ev
    from .models import Minutes

    try:
        minutes = Minutes.objects.select_related("meeting").get(pk=minutes_id)
    except Minutes.DoesNotExist:
        logger.warning("archive_minutes_to_system05: minutes %s not found", minutes_id)
        return {"archived": False, "reason": "not_found"}

    if not minutes.approved or not minutes.immutable_at:
        logger.warning(
            "archive_minutes_to_system05: minutes %s not signed yet — skipping",
            minutes_id,
        )
        return {"archived": False, "reason": "not_signed"}

    if minutes.archive_ref:
        # Already archived — idempotent no-op.
        return {"archived": True, "archive_ref": minutes.archive_ref, "noop": True}

    try:
        archive_ref = System05Client().archive_minutes(
            minutes_id=str(minutes.id),
            meeting_reference=minutes.meeting.reference,
            content=minutes.content,
            signed_by=str(minutes.approved_by_id),
            signed_at=minutes.immutable_at.isoformat(),
            signature_ref=minutes.signature_ref,
            document_ref=minutes.document_ref,
        )
    except System05Error as exc:
        if not exc.retryable:
            # Permanent rejection — escalate, do not retry forever.
            AuditEvent.record(
                actor_id=None,
                action=ev.MINUTES_ARCHIVE_FAILED,
                entity_type="minutes",
                entity_id=minutes.id,
                new_state={
                    "reason": str(exc),
                    "correlation_id": exc.correlation_id,
                    "retryable": False,
                },
            )
            publish("MinutesArchiveFailed", {
                "minutes_id": str(minutes.id),
                "meeting_id": str(minutes.meeting_id),
                "reason": str(exc),
            })
            logger.error(
                "archive_minutes_to_system05: permanent rejection for %s: %s",
                minutes.id, exc,
            )
            return {"archived": False, "reason": "permanent_rejection"}
        raise  # let Celery retry

    minutes.archive_ref = archive_ref
    minutes.save(update_fields=["archive_ref", "updated_at"])
    AuditEvent.record(
        actor_id=None,
        action=ev.MINUTES_ARCHIVED,
        entity_type="minutes",
        entity_id=minutes.id,
        new_state={"archive_ref": archive_ref},
    )
    publish("MinutesArchived", {
        "minutes_id": str(minutes.id),
        "meeting_id": str(minutes.meeting_id),
        "archive_ref": archive_ref,
    })
    logger.info(
        "archive_minutes_to_system05: minutes %s archived as %s",
        minutes.id, archive_ref,
    )
    return {"archived": True, "archive_ref": archive_ref}


@shared_task(queue="sla-monitor")
def verify_archive_integrity():
    """Daily: verify the integrity hash of every archived Minutes record.

    SRS §2.2.5: "Daily integrity checksum verified between NBES local copy
    and System 05 archive copy." A mismatch raises a critical alert and is
    audit-logged so the Administrator can investigate.
    """
    from apps.audit.models import AuditEvent
    from shared.events import publish
    from shared.integrations.system05 import System05Client, System05Error

    from . import events as ev
    from .models import Minutes

    client = System05Client()
    archived = Minutes.objects.exclude(archive_ref="").exclude(archive_ref__isnull=True)
    checked = 0
    mismatched = 0

    for minutes in archived.iterator():
        local_hash = System05Client.integrity_hash({
            "source": "nbes",
            "kind": "nbec_minutes",
            "record_id": str(minutes.id),
            "meeting_reference": minutes.meeting.reference,
            "content": minutes.content,
            "signed_by": str(minutes.approved_by_id),
            "signed_at": minutes.immutable_at.isoformat() if minutes.immutable_at else "",
            "signature_ref": minutes.signature_ref,
            "document_ref": minutes.document_ref,
        })
        try:
            matched = client.verify_integrity(
                archive_ref=minutes.archive_ref, local_hash=local_hash
            )
        except System05Error as exc:
            logger.warning(
                "verify_archive_integrity: transport error for %s (%s) — skipping",
                minutes.archive_ref, exc,
            )
            continue
        checked += 1
        if not matched:
            mismatched += 1
            AuditEvent.record(
                actor_id=None,
                action=ev.MINUTES_ARCHIVE_INTEGRITY_MISMATCH,
                entity_type="minutes",
                entity_id=minutes.id,
                new_state={
                    "archive_ref": minutes.archive_ref,
                    "local_hash": local_hash,
                },
            )
            publish("MinutesArchiveIntegrityMismatch", {
                "minutes_id": str(minutes.id),
                "archive_ref": minutes.archive_ref,
            })
            logger.critical(
                "verify_archive_integrity: MISMATCH for minutes %s (archive_ref=%s)",
                minutes.id, minutes.archive_ref,
            )

    logger.info(
        "verify_archive_integrity: checked=%d mismatched=%d", checked, mismatched
    )
    return {"checked": checked, "mismatched": mismatched}


# ── Annual COI refresh (SRS §2.2.4) ───────────────────────────────────────────

@shared_task(queue="sla-monitor")
def monitor_coi_refresh_due():
    """Daily: find approved COIs whose review_date has passed and prompt refresh.

    SRS §2.2.4: "system prompts members to re-confirm on a configurable
    cadence (default annual)." NBES publishes a ``COIRefreshDue`` event;
    notification dispatch is downstream (IAM / System 21).
    """
    from apps.audit.models import AuditEvent
    from shared.events import publish

    from . import events as ev
    from .models import ConflictDeclaration

    today = date.today()
    fallback_threshold = today - timedelta(days=COI_REFRESH_INTERVAL_DAYS)

    # COIs due if:
    #  - review_date is set and has passed today, OR
    #  - no review_date but effective_from is older than the refresh interval
    qs = ConflictDeclaration.objects.filter(
        status=ConflictDeclaration.Status.APPROVED,
    ).filter(
        Q(review_date__lt=today)
        | Q(review_date__isnull=True, effective_from__lt=fallback_threshold)
    )

    prompted = 0
    for coi in qs.iterator():
        try:
            AuditEvent.record(
                actor_id=None,
                action=ev.COI_REFRESH_DUE,
                entity_type="conflict_declaration",
                entity_id=coi.id,
                new_state={
                    "member_id": str(coi.member_id),
                    "review_date": str(coi.review_date) if coi.review_date else None,
                    "effective_from": str(coi.effective_from) if coi.effective_from else None,
                },
            )
            publish("COIRefreshDue", {
                "coi_id": str(coi.id),
                "member_id": str(coi.member_id),
                "subject_type": coi.subject_type,
                "review_date": str(coi.review_date) if coi.review_date else None,
            })
            prompted += 1
        except Exception:
            logger.exception("monitor_coi_refresh_due: failed to prompt for COI %s", coi.id)

    if prompted:
        logger.info("monitor_coi_refresh_due: prompted %d COI(s) for refresh", prompted)
    return {"prompted": prompted}
