"""apps/sitting/services.py — Phase 4 Sitting lifecycle business logic.

Public functions accept and return Django model instances. Callers (views,
tasks) pass ``actor_id`` (keycloak sub) plus optional ``request_id`` and
``ip_address`` extracted from the DRF request.

State machine for :class:`Sitting`::

    DRAFT ── update_sitting_draft ──┐
      │                              │
      │  add_or_update_paper × 5     │
      │  publish_blueprint_version   │
      │                              │
      ├── configure_sitting ──► CONFIGURED
      │                          │
      │       approve_sitting ───┤ (approved_at, approved_via_meeting_id)
      │                          │
      │       lock_sitting ─────►LOCKED  (auto_lock at T-30 or manual)
      │                          │
      │     amend_non_critical ──┤   (Chair only; non-critical fields)
      │  amend_critical_with_…  ─┤   (NBEC resolution required)
      │                          │
      │      activate_sitting ──►ACTIVE  (Phase 6 trigger)
      │                          │
      │        close_sitting ───►CLOSED

Reference: ``NBES_Phases_1-4_Foundation_Content_Configuration.docx`` §4.4
and SRS §3.3 (NBE-F03).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from shared.events import publish

from . import events as ev
from .models import (
    BlueprintVersion,
    Sitting,
    SittingLock,
    SittingLockEvent,
    SubjectPaper,
)


# ── Configuration ─────────────────────────────────────────────────────────-

# §71 — exactly five subject papers.
REQUIRED_SUBJECT_PAPER_COUNT = 5

# Fields that count as "critical" per spec §4.2.3 — post-lock changes require
# a full NBEC resolution (resolution_amend). Anything not listed here can be
# amended by the Chair under chair_amend.
CRITICAL_SITTING_FIELDS: frozenset[str] = frozenset({
    "pass_mark",
    "pass_rule",
    "pass_band_min",
    "pass_band_max",
    "compensated_min_per_paper",
    "compensated_aggregate_floor",
    "sitting_date",
    "sitting_end_date",
    "normalisation_method",
})

CRITICAL_PAPER_FIELDS: frozenset[str] = frozenset({
    "subject_code",
    "total_marks",
    "pass_mark",
    "mode",
    "duration_minutes",
    "sections",
    "blueprint_version",
    "blueprint_version_id",
    "normalisation_method",
    "normalisation_params",
})


class SittingValidationError(ValueError):
    """Raised for validation failures with a list of human-readable reasons.

    Mirrors the contract used by ``shared/exceptions.py`` so views render a
    consistent error envelope without us re-implementing it here.
    """

    def __init__(self, code: str, message: str, *, details: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []


# ── Audit helper ──────────────────────────────────────────────────────────-


def _audit(
    actor_id,
    action: str,
    entity_type: str,
    entity_id,
    *,
    old_state=None,
    new_state=None,
    request_id=None,
    ip_address=None,
) -> None:
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


# ── Validators ────────────────────────────────────────────────────────────-


def _to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _validate_pass_band(sitting: Sitting, pass_mark: Decimal | None) -> None:
    if pass_mark is None:
        return
    lo, hi = sitting.pass_band_min, sitting.pass_band_max
    if lo is not None and pass_mark < lo:
        raise SittingValidationError(
            "OUT_OF_POLICY_PASS_STANDARD",
            f"Pass standard {pass_mark} is below NBEC policy band minimum {lo}.",
        )
    if hi is not None and pass_mark > hi:
        raise SittingValidationError(
            "OUT_OF_POLICY_PASS_STANDARD",
            f"Pass standard {pass_mark} is above NBEC policy band maximum {hi}.",
        )


def _validate_paper_sections(paper: SubjectPaper) -> None:
    """Section marks must sum to total_marks (F03-02)."""
    sections = paper.sections or []
    if not sections:
        return  # empty section list is permitted during draft
    try:
        total = sum(int(s.get("marks", 0)) for s in sections)
    except (TypeError, ValueError) as exc:
        raise SittingValidationError(
            "INCONSISTENT_MARKS_ALLOCATION",
            f"Paper {paper.subject_code}: malformed section marks ({exc}).",
        ) from exc
    if total != paper.total_marks:
        raise SittingValidationError(
            "INCONSISTENT_MARKS_ALLOCATION",
            f"Paper {paper.subject_code}: section marks sum to {total}, "
            f"expected {paper.total_marks}.",
        )


def _validate_compensation(sitting: Sitting) -> None:
    """Compensation rule must be internally consistent (F03-02)."""
    if sitting.pass_rule != Sitting.PassRule.COMPENSATED:
        return
    if (
        sitting.compensated_min_per_paper is None
        or sitting.compensated_aggregate_floor is None
    ):
        raise SittingValidationError(
            "INCONSISTENT_COMPENSATION_RULE",
            "Compensated pass requires both compensated_min_per_paper "
            "and compensated_aggregate_floor.",
        )


def _validate_sitting_complete(sitting: Sitting) -> None:
    """Pre-flight for configure_sitting / lock_sitting."""
    reasons: list[str] = []

    papers = list(sitting.subject_papers.all())
    if len(papers) != REQUIRED_SUBJECT_PAPER_COUNT:
        reasons.append(
            f"§71 requires exactly {REQUIRED_SUBJECT_PAPER_COUNT} subject papers; "
            f"found {len(papers)}."
        )

    for paper in papers:
        try:
            _validate_pass_band(sitting, paper.pass_mark)
            _validate_paper_sections(paper)
        except SittingValidationError as exc:
            reasons.append(exc.message)
        if paper.blueprint_version_id is None:
            reasons.append(
                f"Paper {paper.subject_code} is missing a blueprint version."
            )

    try:
        _validate_compensation(sitting)
    except SittingValidationError as exc:
        reasons.append(exc.message)

    if sitting.sitting_end_date < sitting.sitting_date:
        reasons.append("sitting_end_date must be on or after sitting_date.")

    if reasons:
        raise SittingValidationError(
            "SITTING_NOT_READY",
            "Sitting configuration is incomplete or inconsistent.",
            details=reasons,
        )


# ── Sitting lifecycle ─────────────────────────────────────────────────────-


@transaction.atomic
def create_sitting(actor_id, data: dict, *, request_id=None, ip_address=None) -> Sitting:
    """Create a Sitting in DRAFT. Caller supplies validated dict from a serializer."""
    sitting = Sitting.objects.create(created_by_id=actor_id, **data)
    _audit(
        actor_id, ev.SITTING_CREATED, "sitting", sitting.ref,
        new_state={
            "ref": sitting.ref,
            "sitting_date": sitting.sitting_date.isoformat(),
            "pass_rule": sitting.pass_rule,
        },
        request_id=request_id, ip_address=ip_address,
    )
    publish("SittingCreated", {"ref": sitting.ref, "sitting_date": sitting.sitting_date.isoformat()})
    return sitting


@transaction.atomic
def update_sitting_draft(
    actor_id, sitting: Sitting, data: dict, *, request_id=None, ip_address=None,
) -> Sitting:
    """Edit a DRAFT (or CONFIGURED) sitting. Refuses once LOCKED."""
    if sitting.is_locked:
        raise SittingValidationError(
            "CONFIGURATION_LOCKED",
            "Configuration is locked. Submit an NBEC resolution to amend "
            "critical fields, or use the Chair amendment flow for non-critical "
            "fields.",
        )

    old_state = {k: _snapshot_value(getattr(sitting, k)) for k in data}
    for field, value in data.items():
        setattr(sitting, field, value)

    # Validate pass-band consistency if pass_mark was changed.
    if "pass_mark" in data:
        _validate_pass_band(sitting, _to_decimal(sitting.pass_mark))
    _validate_compensation(sitting)

    # If we were CONFIGURED, an edit drops us back to DRAFT (must re-configure).
    if sitting.status == Sitting.Status.CONFIGURED:
        sitting.status = Sitting.Status.DRAFT
    sitting.save()

    _audit(
        actor_id, ev.SITTING_DRAFT_UPDATED, "sitting", sitting.ref,
        old_state=old_state,
        new_state={k: _snapshot_value(getattr(sitting, k)) for k in data},
        request_id=request_id, ip_address=ip_address,
    )
    publish("SittingDraftUpdated", {"ref": sitting.ref})
    return sitting


@transaction.atomic
def add_or_update_paper(
    actor_id, sitting: Sitting, data: dict, *, request_id=None, ip_address=None,
) -> SubjectPaper:
    """Add a SubjectPaper to a sitting, or update an existing one (by subject_code).

    Refuses on LOCKED. Caller's serializer is responsible for field shape; this
    enforces §71 (capacity), pass-band, and section-marks invariants.
    """
    if sitting.is_locked:
        raise SittingValidationError(
            "CONFIGURATION_LOCKED",
            "Cannot add or update papers on a locked sitting.",
        )

    subject_code = data["subject_code"]
    pass_mark = _to_decimal(data.get("pass_mark"))
    _validate_pass_band(sitting, pass_mark)

    paper, created = SubjectPaper.objects.get_or_create(
        sitting=sitting, subject_code=subject_code,
        defaults={"subject_name": data.get("subject_name", subject_code)},
    )

    # Capacity check on create — but allow update beyond 5 to be rejected at
    # configure time, since a Secretariat may legitimately be editing one
    # of the five before deleting another.
    if created:
        if sitting.subject_papers.count() > REQUIRED_SUBJECT_PAPER_COUNT:
            paper.delete()
            raise SittingValidationError(
                "SUBJECT_COUNT_MISMATCH",
                f"§71 requires exactly {REQUIRED_SUBJECT_PAPER_COUNT} subject papers; "
                f"would exceed limit by adding {subject_code}.",
            )

    old_state = None if created else {
        k: _snapshot_value(getattr(paper, k))
        for k in data if hasattr(paper, k)
    }

    for field, value in data.items():
        if field in {"sitting", "id"}:
            continue
        setattr(paper, field, value)
    paper.save()

    _validate_paper_sections(paper)

    # Edits invalidate CONFIGURED status — back to DRAFT for re-configure.
    if sitting.status == Sitting.Status.CONFIGURED:
        sitting.status = Sitting.Status.DRAFT
        sitting.save(update_fields=["status", "updated_at"])

    action = ev.PAPER_ADDED if created else ev.PAPER_UPDATED
    _audit(
        actor_id, action, "subject_paper", paper.id,
        old_state=old_state,
        new_state={
            "sitting": sitting.ref,
            "subject_code": paper.subject_code,
            "mode": paper.mode,
            "total_marks": paper.total_marks,
        },
        request_id=request_id, ip_address=ip_address,
    )
    publish(
        "SittingPaperAdded" if created else "SittingPaperUpdated",
        {"sitting_ref": sitting.ref, "paper_id": str(paper.id),
         "subject_code": paper.subject_code},
    )
    return paper


@transaction.atomic
def remove_paper(
    actor_id, paper: SubjectPaper, *, request_id=None, ip_address=None,
) -> None:
    """Remove a SubjectPaper from a draft sitting. Refuses on LOCKED."""
    sitting = paper.sitting
    if sitting.is_locked:
        raise SittingValidationError(
            "CONFIGURATION_LOCKED",
            "Cannot remove papers from a locked sitting.",
        )

    paper_id, subject_code = paper.id, paper.subject_code
    paper.delete()

    if sitting.status == Sitting.Status.CONFIGURED:
        sitting.status = Sitting.Status.DRAFT
        sitting.save(update_fields=["status", "updated_at"])

    _audit(
        actor_id, ev.PAPER_REMOVED, "subject_paper", paper_id,
        old_state={"sitting": sitting.ref, "subject_code": subject_code},
        request_id=request_id, ip_address=ip_address,
    )
    publish(
        "SittingPaperRemoved",
        {"sitting_ref": sitting.ref, "paper_id": str(paper_id),
         "subject_code": subject_code},
    )


@transaction.atomic
def configure_sitting(
    actor_id, sitting: Sitting, *, request_id=None, ip_address=None,
) -> Sitting:
    """Move DRAFT → CONFIGURED. Runs the full readiness check.

    A CONFIGURED sitting can be approved by the NBEC (Phase 2 meeting workflow)
    and then locked at T-30. Edits drop the status back to DRAFT.
    """
    if sitting.status not in {Sitting.Status.DRAFT, Sitting.Status.CONFIGURED}:
        raise SittingValidationError(
            "INVALID_TRANSITION",
            f"Cannot configure sitting in status '{sitting.status}'.",
        )
    _validate_sitting_complete(sitting)

    old_status = sitting.status
    sitting.status = Sitting.Status.CONFIGURED
    sitting.save(update_fields=["status", "updated_at"])

    _audit(
        actor_id, ev.SITTING_CONFIGURED, "sitting", sitting.ref,
        old_state={"status": old_status}, new_state={"status": sitting.status},
        request_id=request_id, ip_address=ip_address,
    )
    publish("SittingConfigured", {"ref": sitting.ref})
    return sitting


@transaction.atomic
def approve_sitting(
    actor_id, sitting: Sitting, *, meeting_id, request_id=None, ip_address=None,
) -> Sitting:
    """Record NBEC approval (pre-lock). Sitting must be CONFIGURED.

    ``meeting_id`` is the Phase 2 :class:`apps.committee.models.Meeting` that
    approved the configuration. We don't import committee models here to keep
    the dependency one-way (committee is approved as a stable upstream);
    the foreign key is stored as UUID only.
    """
    if sitting.status != Sitting.Status.CONFIGURED:
        raise SittingValidationError(
            "INVALID_TRANSITION",
            f"Sitting must be CONFIGURED before approval (current: {sitting.status}).",
        )

    sitting.approved_at = timezone.now()
    sitting.approved_via_meeting_id = meeting_id
    sitting.save(update_fields=["approved_at", "approved_via_meeting_id", "updated_at"])

    _audit(
        actor_id, ev.SITTING_APPROVED, "sitting", sitting.ref,
        new_state={"approved_at": sitting.approved_at.isoformat(),
                   "meeting_id": str(meeting_id)},
        request_id=request_id, ip_address=ip_address,
    )
    publish("SittingApproved", {"ref": sitting.ref, "meeting_id": str(meeting_id)})
    return sitting


@transaction.atomic
def lock_sitting(
    actor_id,
    sitting: Sitting,
    *,
    kind: str = SittingLockEvent.Kind.AUTO_LOCK,
    justification: str = "",
    override: bool = False,
    request_id=None,
    ip_address=None,
) -> Sitting:
    """Move CONFIGURED → LOCKED.

    The T-30 monitor task calls this with ``kind=auto_lock`` and ``actor_id=None``
    (system principal). A Chair-initiated early lock or override sets
    ``override=True`` and supplies a justification.
    """
    if sitting.is_locked:
        # Idempotent — re-running lock on an already-locked sitting is a no-op
        # so the T-30 monitor can replay safely.
        return sitting

    if sitting.status != Sitting.Status.CONFIGURED:
        raise SittingValidationError(
            "INVALID_TRANSITION",
            f"Cannot lock sitting in status '{sitting.status}'; must be CONFIGURED.",
        )
    if sitting.approved_at is None:
        raise SittingValidationError(
            "NOT_APPROVED",
            "Sitting must be NBEC-approved before locking.",
        )

    # Re-run readiness check defensively — guard against data drift since approval.
    _validate_sitting_complete(sitting)

    sitting.status = Sitting.Status.LOCKED
    sitting.locked_at = timezone.now()
    sitting.save(update_fields=["status", "locked_at", "updated_at"])

    SittingLockEvent.objects.create(
        sitting=sitting,
        kind=kind,
        actor_id=actor_id,
        justification=justification or ("Automatic T-30 lock" if kind == SittingLockEvent.Kind.AUTO_LOCK else ""),
    )

    # Convenience pointer row — one per sitting, marks the T-30 lock event.
    SittingLock.objects.get_or_create(
        sitting=sitting,
        defaults={
            "locked_by": "system" if actor_id is None else str(actor_id),
            "override": override,
            "override_reason": justification if override else "",
        },
    )

    _audit(
        actor_id, ev.SITTING_LOCKED, "sitting", sitting.ref,
        new_state={"locked_at": sitting.locked_at.isoformat(), "kind": kind},
        request_id=request_id, ip_address=ip_address,
    )
    publish(
        "SittingLocked",
        {"ref": sitting.ref, "locked_at": sitting.locked_at.isoformat(), "kind": kind},
    )
    return sitting


@transaction.atomic
def activate_sitting(
    actor_id, sitting: Sitting, *, request_id=None, ip_address=None,
) -> Sitting:
    """Move LOCKED → ACTIVE (Phase 6 will trigger this when sitting opens)."""
    if sitting.status != Sitting.Status.LOCKED:
        raise SittingValidationError(
            "INVALID_TRANSITION",
            f"Cannot activate sitting in status '{sitting.status}'; must be LOCKED.",
        )
    sitting.status = Sitting.Status.ACTIVE
    sitting.save(update_fields=["status", "updated_at"])
    _audit(
        actor_id, ev.SITTING_ACTIVATED, "sitting", sitting.ref,
        new_state={"status": sitting.status},
        request_id=request_id, ip_address=ip_address,
    )
    publish("SittingActivated", {"ref": sitting.ref})
    return sitting


@transaction.atomic
def close_sitting(
    actor_id, sitting: Sitting, *, request_id=None, ip_address=None,
) -> Sitting:
    """Move ACTIVE → CLOSED (post-sitting)."""
    if sitting.status != Sitting.Status.ACTIVE:
        raise SittingValidationError(
            "INVALID_TRANSITION",
            f"Cannot close sitting in status '{sitting.status}'; must be ACTIVE.",
        )
    sitting.status = Sitting.Status.CLOSED
    sitting.save(update_fields=["status", "updated_at"])
    _audit(
        actor_id, ev.SITTING_CLOSED, "sitting", sitting.ref,
        new_state={"status": sitting.status},
        request_id=request_id, ip_address=ip_address,
    )
    publish("SittingClosed", {"ref": sitting.ref})
    return sitting


# ── Amendments ────────────────────────────────────────────────────────────-


def _classify_changes(
    changes: dict, critical_fields: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Return (critical, non_critical) lists of field names in ``changes``."""
    critical, non_critical = [], []
    for field in changes:
        (critical if field in critical_fields else non_critical).append(field)
    return critical, non_critical


@transaction.atomic
def amend_non_critical(
    actor_id,
    sitting: Sitting,
    changes: dict,
    justification: str,
    *,
    request_id=None,
    ip_address=None,
) -> Sitting:
    """Chair-only amendment of non-critical fields on a LOCKED sitting.

    Refuses if any critical field is in ``changes``. Records a SittingLockEvent
    of kind=chair_amend with before/after snapshots.
    """
    if not sitting.is_locked:
        raise SittingValidationError(
            "NOT_LOCKED",
            "Use update_sitting_draft for pre-lock edits.",
        )
    if not justification or len(justification.strip()) < 10:
        raise SittingValidationError(
            "MISSING_JUSTIFICATION",
            "Post-lock amendments require a justification of at least 10 characters.",
        )

    critical, non_critical = _classify_changes(changes, CRITICAL_SITTING_FIELDS)
    if critical:
        raise SittingValidationError(
            "CRITICAL_FIELD_REQUIRES_RESOLUTION",
            f"Critical fields cannot be amended without an NBEC resolution: "
            f"{', '.join(critical)}.",
        )

    before = {k: _snapshot_value(getattr(sitting, k)) for k in non_critical}
    for field, value in changes.items():
        setattr(sitting, field, value)
    sitting.save()
    after = {k: _snapshot_value(getattr(sitting, k)) for k in non_critical}

    event = SittingLockEvent.objects.create(
        sitting=sitting,
        kind=SittingLockEvent.Kind.CHAIR_AMEND,
        actor_id=actor_id,
        justification=justification,
        affected_fields=non_critical,
        before_snapshot=before,
        after_snapshot=after,
    )
    _audit(
        actor_id, ev.SITTING_AMENDED_CHAIR, "sitting", sitting.ref,
        old_state=before, new_state=after,
        request_id=request_id, ip_address=ip_address,
    )
    publish(
        "SittingAmendedChair",
        {"ref": sitting.ref, "event_id": str(event.id),
         "fields": non_critical},
    )
    return sitting


@transaction.atomic
def amend_critical_with_resolution(
    actor_id,
    sitting: Sitting,
    changes: dict,
    resolution_ref: str,
    justification: str,
    *,
    request_id=None,
    ip_address=None,
) -> Sitting:
    """Amend critical fields on a LOCKED sitting under an NBEC resolution.

    ``resolution_ref`` is a free-form reference to the signed Minutes /
    Resolution (Phase 2). The caller is responsible for verifying that the
    resolution exists and authorises this specific change — Phase 2 does not
    yet expose a query API, so we trust the caller's check here.
    """
    if not sitting.is_locked:
        raise SittingValidationError(
            "NOT_LOCKED",
            "Use update_sitting_draft for pre-lock edits.",
        )
    if not resolution_ref:
        raise SittingValidationError(
            "MISSING_RESOLUTION_REF",
            "Critical amendments require a recorded NBEC resolution reference.",
        )
    if not justification or len(justification.strip()) < 30:
        raise SittingValidationError(
            "MISSING_JUSTIFICATION",
            "Critical amendments require a justification of at least 30 characters.",
        )

    affected = list(changes.keys())
    before = {k: _snapshot_value(getattr(sitting, k)) for k in affected}
    for field, value in changes.items():
        setattr(sitting, field, value)

    # Re-validate after applying — even with a resolution we don't allow the
    # sitting to land in an inconsistent state (e.g. pass_rule outside enum).
    if "pass_mark" in changes:
        _validate_pass_band(sitting, _to_decimal(sitting.pass_mark))
    _validate_compensation(sitting)
    sitting.save()
    after = {k: _snapshot_value(getattr(sitting, k)) for k in affected}

    event = SittingLockEvent.objects.create(
        sitting=sitting,
        kind=SittingLockEvent.Kind.RESOLUTION_AMEND,
        actor_id=actor_id,
        justification=justification,
        resolution_ref=resolution_ref,
        affected_fields=affected,
        before_snapshot=before,
        after_snapshot=after,
    )
    _audit(
        actor_id, ev.SITTING_AMENDED_RESOLUTION, "sitting", sitting.ref,
        old_state=before, new_state=after,
        request_id=request_id, ip_address=ip_address,
    )
    publish(
        "SittingAmendedResolution",
        {"ref": sitting.ref, "event_id": str(event.id),
         "fields": affected, "resolution_ref": resolution_ref},
    )
    return sitting


# ── Blueprint versioning ──────────────────────────────────────────────────-


@transaction.atomic
def publish_blueprint_version(
    actor_id, subject_code: str, data: dict, *, request_id=None, ip_address=None,
) -> BlueprintVersion:
    """Publish a new BlueprintVersion. ``version_no`` is auto-incremented per subject.

    The dict is expected to carry ``topic_coverage``, ``cognitive_distribution``,
    ``difficulty_distribution``, ``sections``, ``total_marks``, optional
    ``tolerance`` and ``description``. Distribution maps must sum to ~1.0 but
    the actual coverage check is enforced by the Phase 3 / Phase 4 blueprint
    validator at paper construction / variant generation time, not here.
    """
    next_version = (
        BlueprintVersion.objects.filter(subject_code=subject_code)
        .order_by("-version_no")
        .values_list("version_no", flat=True)
        .first()
        or 0
    ) + 1

    version = BlueprintVersion.objects.create(
        subject_code=subject_code,
        version_no=next_version,
        topic_coverage=data.get("topic_coverage", {}),
        cognitive_distribution=data.get("cognitive_distribution", {}),
        difficulty_distribution=data.get("difficulty_distribution", {}),
        sections=data.get("sections", []),
        total_marks=data.get("total_marks", 100),
        tolerance=data.get("tolerance", Decimal("0.050")),
        description=data.get("description", ""),
        published_at=timezone.now(),
        published_by_id=actor_id,
    )
    _audit(
        actor_id, ev.BLUEPRINT_VERSION_PUBLISHED, "blueprint_version", version.id,
        new_state={"subject_code": subject_code, "version_no": next_version},
        request_id=request_id, ip_address=ip_address,
    )
    publish(
        "BlueprintVersionPublished",
        {"subject_code": subject_code, "version_no": next_version,
         "version_id": str(version.id)},
    )
    return version


# ── Read-only snapshot ────────────────────────────────────────────────────-


def get_sitting_snapshot(sitting_ref: str) -> dict:
    """Return a frozen, JSON-serialisable view of a Sitting and its papers.

    Downstream phases (5, 6, 9, 10) consume this snapshot rather than reading
    the live Sitting record directly. Non-critical post-lock amendments do
    update the underlying fields, but the snapshot remains identical for
    fields that were not amended — callers wanting the *original* locked
    snapshot should read the first AUTO_LOCK SittingLockEvent.
    """
    sitting = (
        Sitting.objects
        .prefetch_related("subject_papers__blueprint_version")
        .get(pk=sitting_ref)
    )
    return {
        "ref": sitting.ref,
        "status": sitting.status,
        "sitting_date": sitting.sitting_date.isoformat(),
        "sitting_end_date": sitting.sitting_end_date.isoformat(),
        "pass_rule": sitting.pass_rule,
        "pass_mark": str(sitting.pass_mark),
        "pass_band": {
            "min": str(sitting.pass_band_min),
            "max": str(sitting.pass_band_max),
        },
        "compensation": {
            "min_per_paper": (
                str(sitting.compensated_min_per_paper)
                if sitting.compensated_min_per_paper is not None else None
            ),
            "aggregate_floor": (
                str(sitting.compensated_aggregate_floor)
                if sitting.compensated_aggregate_floor is not None else None
            ),
        },
        "normalisation_method": sitting.normalisation_method,
        "centres": sitting.centres,
        "locked_at": sitting.locked_at.isoformat() if sitting.locked_at else None,
        "approved_at": sitting.approved_at.isoformat() if sitting.approved_at else None,
        "approved_via_meeting_id": (
            str(sitting.approved_via_meeting_id)
            if sitting.approved_via_meeting_id else None
        ),
        "papers": [
            {
                "id": str(paper.id),
                "subject_code": paper.subject_code,
                "subject_name": paper.subject_name,
                "mode": paper.mode,
                "total_marks": paper.total_marks,
                "pass_mark": str(paper.pass_mark),
                "duration_minutes": paper.duration_minutes,
                "sections": paper.sections,
                "normalisation_method": paper.normalisation_method,
                "normalisation_params": paper.normalisation_params,
                "blueprint_version_id": (
                    str(paper.blueprint_version_id)
                    if paper.blueprint_version_id else None
                ),
            }
            for paper in sitting.subject_papers.all()
        ],
    }


# ── Internal helpers ──────────────────────────────────────────────────────-


def _snapshot_value(value):
    """Coerce a Django field value into a JSON-safe form for audit snapshots."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (str, int, float, bool, list, dict)):
        return value
    return str(value)
