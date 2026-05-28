"""apps/committee/services.py — NBEC Committee business logic.

All public functions accept and return Django model instances.
Callers (views) are responsible for passing request_id / ip_address
extracted from the DRF request object.
"""
import uuid
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from shared.events import publish

from . import events as ev
from .models import (
    ActionItem,
    Agenda,
    ConflictDeclaration,
    Meeting,
    Minutes,
    MinutesAddendum,
    NBECMember,
)


def _audit(actor_id, action, entity_type, entity_id, old_state=None, new_state=None,
           request_id=None, ip_address=None):
    AuditEvent.record(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_state=old_state,
        new_state=new_state,
        request_id=request_id,
        ip_address=ip_address,
    )


# ── NBECMember ────────────────────────────────────────────────────────────────

@transaction.atomic
def create_member(actor_id, data: dict, *, request_id=None, ip_address=None) -> NBECMember:
    """Create a new NBEC member record in Draft status.

    The Keycloak/IAM user identified by ``keycloak_sub`` MUST already exist —
    IAM owns user creation, invitations, MFA enrolment, and role grants. This
    function only creates the NBES-side domain record linked to that identity.
    """
    member = NBECMember.objects.create(**data)
    _audit(actor_id, ev.MEMBER_CREATED, "committee_member", member.id,
           new_state={"designation": member.designation, "contact": member.contact},
           request_id=request_id, ip_address=ip_address)
    publish("MemberCreated", {"member_id": str(member.id), "designation": member.designation})
    return member


@transaction.atomic
def amend_member(actor_id, member: NBECMember, data: dict, *,
                 request_id=None, ip_address=None) -> NBECMember:
    """Amend an existing member record. All field changes are audit-logged."""
    old_state = {
        "full_name": member.full_name,
        "designation": member.designation,
        "contact": member.contact,
        "tenure_end": str(member.tenure_end) if member.tenure_end else None,
    }
    for field, value in data.items():
        setattr(member, field, value)
    member.save()
    _audit(actor_id, ev.MEMBER_AMENDED, "committee_member", member.id,
           old_state=old_state,
           new_state={
               "full_name": member.full_name,
               "designation": member.designation,
               "contact": member.contact,
               "tenure_end": str(member.tenure_end) if member.tenure_end else None,
           },
           request_id=request_id, ip_address=ip_address)
    publish("MemberAmended", {"member_id": str(member.id)})
    return member


@transaction.atomic
def activate_member(actor_id, member: NBECMember, *,
                    request_id=None, ip_address=None) -> NBECMember:
    """Activate a Draft or Renewed member."""
    old_status = member.status
    member.activate()
    _audit(actor_id, ev.MEMBER_ACTIVATED, "committee_member", member.id,
           old_state={"status": old_status}, new_state={"status": member.status},
           request_id=request_id, ip_address=ip_address)
    publish("MemberActivated", {"member_id": str(member.id)})
    return member


@transaction.atomic
def expire_member(actor_id, member: NBECMember, *,
                  request_id=None, ip_address=None) -> NBECMember:
    """Expire a member whose tenure has ended."""
    old_status = member.status
    member.expire()
    _audit(actor_id, ev.MEMBER_EXPIRED, "committee_member", member.id,
           old_state={"status": old_status}, new_state={"status": member.status},
           request_id=request_id, ip_address=ip_address)
    publish("MemberExpired", {"member_id": str(member.id)})
    return member


# ── ConflictDeclaration (COI) ─────────────────────────────────────────────────

@transaction.atomic
def declare_coi(actor_id, data: dict, *,
                request_id=None, ip_address=None) -> ConflictDeclaration:
    """Member self-declares a COI. Status starts as Pending."""
    coi = ConflictDeclaration.objects.create(**data)
    _audit(actor_id, ev.COI_DECLARED, "conflict_declaration", coi.id,
           new_state={
               "member_id": str(coi.member_id),
               "subject_type": coi.subject_type,
               "nature": coi.nature,
           },
           request_id=request_id, ip_address=ip_address)
    publish("ConflictDeclared", {
        "coi_id": str(coi.id),
        "member_id": str(coi.member_id),
        "subject_type": coi.subject_type,
    })
    return coi


@transaction.atomic
def review_coi(actor_id, coi: ConflictDeclaration, approve: bool, review_date=None, *,
               request_id=None, ip_address=None) -> ConflictDeclaration:
    """Secretariat or Chair reviews a COI declaration."""
    if coi.status != ConflictDeclaration.Status.PENDING:
        raise ValueError(f"COI is already in status '{coi.status}'; cannot re-review.")
    old_status = coi.status
    coi.status = ConflictDeclaration.Status.APPROVED if approve else ConflictDeclaration.Status.DISMISSED
    coi.reviewed_at = timezone.now()
    coi.reviewed_by_id = actor_id
    if review_date:
        coi.review_date = review_date
    coi.save(update_fields=["status", "reviewed_at", "reviewed_by_id", "review_date"])
    action = ev.COI_APPROVED if approve else ev.COI_DISMISSED
    _audit(actor_id, action, "conflict_declaration", coi.id,
           old_state={"status": old_status}, new_state={"status": coi.status},
           request_id=request_id, ip_address=ip_address)
    publish("ConflictReviewed", {"coi_id": str(coi.id), "approved": approve})
    return coi


def check_coi(member_id, entity_type: str, entity_id=None) -> dict:
    """Return active (approved) conflicts for a member against an entity.

    Used by the internal ``GET /api/v1/nbec/policy/coi`` endpoint consumed
    by other NBES apps (itembank, marking) before assigning work.
    """
    qs = ConflictDeclaration.objects.filter(
        member_id=member_id,
        status=ConflictDeclaration.Status.APPROVED,
    )
    if entity_type:
        qs = qs.filter(affected_entity_type=entity_type)
    if entity_id:
        qs = qs.filter(affected_entity_id=entity_id)

    conflict_ids = list(qs.values_list("id", flat=True))
    return {
        "has_active_conflict": bool(conflict_ids),
        "member_id": member_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "conflict_ids": conflict_ids,
    }


# ── Meeting ───────────────────────────────────────────────────────────────────

@transaction.atomic
def schedule_meeting(actor_id, data: dict, *,
                     request_id=None, ip_address=None) -> Meeting:
    """Secretariat schedules a meeting (status: Draft)."""
    meeting = Meeting.objects.create(**data)
    _audit(actor_id, ev.MEETING_SCHEDULED, "meeting", meeting.id,
           new_state={
               "reference": meeting.reference,
               "meeting_type": meeting.meeting_type,
               "scheduled_date": meeting.scheduled_date.isoformat(),
           },
           request_id=request_id, ip_address=ip_address)
    publish("MeetingScheduled", {
        "meeting_id": str(meeting.id),
        "reference": meeting.reference,
        "scheduled_date": meeting.scheduled_date.isoformat(),
    })
    return meeting


@transaction.atomic
def publish_agenda(actor_id, meeting: Meeting, items: list, document_ref: str = "", *,
                   request_id=None, ip_address=None) -> Agenda:
    """Publish a new agenda version for the meeting."""
    last_version = (
        Agenda.objects.filter(meeting=meeting).order_by("-version")
        .values_list("version", flat=True).first() or 0
    )
    agenda = Agenda.objects.create(
        meeting=meeting,
        version=last_version + 1,
        items=items,
        document_ref=document_ref,
        published_at=timezone.now(),
        created_by_id=actor_id,
    )
    # Advance meeting to Agenda Issued if still Draft
    if meeting.status == Meeting.Status.DRAFT:
        meeting.status = Meeting.Status.AGENDA_ISSUED
        meeting.save(update_fields=["status"])

    _audit(actor_id, ev.MEETING_AGENDA_PUBLISHED, "meeting", meeting.id,
           new_state={"agenda_version": agenda.version, "item_count": len(items)},
           request_id=request_id, ip_address=ip_address)
    publish("MeetingAgendaPublished", {
        "meeting_id": str(meeting.id),
        "agenda_id": str(agenda.id),
        "version": agenda.version,
    })
    return agenda


@transaction.atomic
def record_attendance(actor_id, meeting: Meeting, attendee_ids: list, *,
                      request_id=None, ip_address=None) -> Meeting:
    """Record the list of attending member keycloak subs."""
    meeting.attendees = list(dict.fromkeys(str(a) for a in attendee_ids))
    meeting.save(update_fields=["attendees"])
    _audit(actor_id, ev.MEETING_ATTENDANCE_RECORDED, "meeting", meeting.id,
           new_state={"attendee_count": len(attendee_ids)},
           request_id=request_id, ip_address=ip_address)
    return meeting


@transaction.atomic
def convene_meeting(actor_id, meeting: Meeting, *,
                    request_id=None, ip_address=None) -> Meeting:
    """Move meeting to Convened — quorum must be met."""
    allowed = {Meeting.Status.DRAFT, Meeting.Status.AGENDA_ISSUED, Meeting.Status.SCHEDULED}
    if meeting.status not in allowed:
        raise ValueError(
            f"Cannot convene meeting in status '{meeting.status}'."
        )
    if not meeting.quorum_met:
        raise ValueError(
            f"Quorum not met: {len(meeting.attendees)} attending, "
            f"{meeting.quorum_required} required."
        )
    old_status = meeting.status
    meeting.status = Meeting.Status.CONVENED
    meeting.convened_at = timezone.now()
    meeting.save(update_fields=["status", "convened_at"])
    _audit(actor_id, ev.MEETING_CONVENED, "meeting", meeting.id,
           old_state={"status": old_status}, new_state={"status": meeting.status},
           request_id=request_id, ip_address=ip_address)
    publish("MeetingConvened", {"meeting_id": str(meeting.id)})
    return meeting


@transaction.atomic
def adjourn_meeting(actor_id, meeting: Meeting, *,
                    request_id=None, ip_address=None):
    """Move meeting to Adjourned and create a draft Minutes record."""
    if meeting.status != Meeting.Status.CONVENED:
        raise ValueError(
            f"Cannot adjourn meeting in status '{meeting.status}'; must be Convened."
        )
    meeting.status = Meeting.Status.ADJOURNED
    meeting.adjourned_at = timezone.now()
    meeting.save(update_fields=["status", "adjourned_at"])
    minutes = Minutes.objects.create(
        meeting=meeting,
        content=(
            f"Draft minutes of {meeting.reference} "
            f"({meeting.get_meeting_type_display()}) held on "
            f"{meeting.scheduled_date.strftime('%d %B %Y')}."
        ),
    )
    _audit(actor_id, ev.MEETING_ADJOURNED, "meeting", meeting.id,
           old_state={"status": "convened"}, new_state={"status": meeting.status},
           request_id=request_id, ip_address=ip_address)
    publish("MeetingAdjourned", {"meeting_id": str(meeting.id)})
    return meeting, minutes


# ── Minutes ───────────────────────────────────────────────────────────────────

@transaction.atomic
def sign_minutes(actor_id, minutes: Minutes, signature_ref: str = "", *,
                 request_id=None, ip_address=None) -> Minutes:
    """Chair digitally signs and seals the minutes — immutable from this point.

    On commit, schedules archival to System 05 (SRS §2.4.2: archive must land
    within 1 hour of sign-off).
    """
    if minutes.approved:
        raise ValueError("Minutes have already been signed.")
    minutes.sign(chair_id=actor_id, signature_ref=signature_ref)
    # Advance the meeting to Minuted
    meeting = minutes.meeting
    if meeting.status == Meeting.Status.ADJOURNED:
        meeting.status = Meeting.Status.MINUTED
        meeting.save(update_fields=["status"])
    # Link action items to these minutes
    ActionItem.objects.filter(meeting=meeting, minutes__isnull=True).update(minutes=minutes)
    _audit(actor_id, ev.MINUTES_SIGNED, "minutes", minutes.id,
           new_state={"signed_at": minutes.immutable_at.isoformat(),
                      "signature_ref": signature_ref},
           request_id=request_id, ip_address=ip_address)
    publish("MinutesSigned", {
        "minutes_id": str(minutes.id),
        "meeting_id": str(minutes.meeting_id),
    })
    # Kick off System 05 archival after this transaction commits so the
    # worker reads the persisted row.
    minutes_id = str(minutes.id)
    transaction.on_commit(lambda: _enqueue_archive(minutes_id))
    return minutes


def _enqueue_archive(minutes_id: str) -> None:
    """Indirection so tests can patch the dispatch without importing Celery."""
    from .tasks import archive_minutes_to_system05
    archive_minutes_to_system05.delay(minutes_id)


@transaction.atomic
def issue_addendum(actor_id, minutes: Minutes, content: str, document_ref: str = "", *,
                   request_id=None, ip_address=None) -> MinutesAddendum:
    """Chair issues an addendum to already-signed minutes."""
    if not minutes.approved:
        raise ValueError("Cannot issue addendum to unsigned minutes.")
    addendum = MinutesAddendum.objects.create(
        minutes=minutes,
        content=content,
        issued_by_id=actor_id,
        issued_at=timezone.now(),
        document_ref=document_ref,
    )
    _audit(actor_id, ev.MINUTES_ADDENDUM_ISSUED, "minutes", minutes.id,
           new_state={"addendum_id": str(addendum.id)},
           request_id=request_id, ip_address=ip_address)
    publish("MinutesAddendumIssued", {
        "addendum_id": str(addendum.id),
        "minutes_id": str(minutes.id),
    })
    return addendum
