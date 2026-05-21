"""apps/sitting/tests/test_services.py — sitting lifecycle and amendments.

Covers acceptance criteria for NBE-F03:

* F03-01 — five-paper §71 enforcement
* F03-02 — pass-band, section-marks, compensation consistency
* F03-03 — lock state machine (CONFIGURED → LOCKED)
* §4.2.3 — post-lock amendment splits (Chair non-critical / NBEC critical)
* §4.4 — snapshot stability across non-critical amendments
"""
import datetime
import uuid
from decimal import Decimal

import pytest

from apps.sitting import services
from apps.sitting.models import (
    BlueprintVersion,
    Sitting,
    SittingLock,
    SittingLockEvent,
    SubjectPaper,
)


ACTOR = uuid.UUID("00000000-0000-0000-0000-000000000001")
CHAIR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
MEETING_ID = uuid.uuid4()


SUBJECTS = ["CIV", "CRIM", "EVID", "ETHICS", "PROP"]


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def draft_sitting(db):
    return services.create_sitting(
        ACTOR,
        {
            "ref": "BAR-2027-05",
            "sitting_date": datetime.date(2027, 5, 1),
            "sitting_end_date": datetime.date(2027, 5, 5),
            "pass_mark": Decimal("50.00"),
            "pass_rule": Sitting.PassRule.ALL_PASS,
        },
    )


@pytest.fixture
def blueprint_versions(db):
    """One published blueprint version per §71 subject."""
    return {
        code: services.publish_blueprint_version(
            ACTOR, code,
            {
                "topic_coverage": {"core": 1.0},
                "cognitive_distribution": {"Knowledge": 1.0},
                "difficulty_distribution": {"Medium": 1.0},
                "sections": [{"name": "A", "marks": 100}],
                "total_marks": 100,
            },
        )
        for code in SUBJECTS
    }


def _attach_five_papers(sitting, blueprint_versions, *, sections=None):
    sections = sections or [{"name": "A", "marks": 100}]
    for code in SUBJECTS:
        services.add_or_update_paper(
            ACTOR, sitting,
            {
                "subject_code": code,
                "subject_name": code.title(),
                "mode": SubjectPaper.Mode.CBT,
                "total_marks": 100,
                "pass_mark": Decimal("50.00"),
                "duration_minutes": 180,
                "sections": sections,
                "blueprint_version": blueprint_versions[code],
            },
        )


# ── §71 enforcement ────────────────────────────────────────────────────────


def test_configure_rejects_when_fewer_than_five_papers(draft_sitting, blueprint_versions):
    # Attach only four papers.
    for code in SUBJECTS[:4]:
        services.add_or_update_paper(
            ACTOR, draft_sitting,
            {
                "subject_code": code, "subject_name": code, "mode": "cbt",
                "total_marks": 100, "pass_mark": Decimal("50.00"),
                "blueprint_version": blueprint_versions[code],
            },
        )
    with pytest.raises(services.SittingValidationError) as exc:
        services.configure_sitting(ACTOR, draft_sitting)
    assert any("§71" in d for d in exc.value.details), exc.value.details


def test_add_or_update_paper_blocks_sixth(draft_sitting, blueprint_versions):
    _attach_five_papers(draft_sitting, blueprint_versions)
    with pytest.raises(services.SittingValidationError) as exc:
        services.add_or_update_paper(
            ACTOR, draft_sitting,
            {"subject_code": "EXTRA", "subject_name": "Extra", "mode": "cbt"},
        )
    assert exc.value.code == "SUBJECT_COUNT_MISMATCH"


# ── Pass-band + section-marks ─────────────────────────────────────────────-


def test_pass_mark_must_be_within_policy_band(draft_sitting, blueprint_versions):
    # band default is 40..70
    with pytest.raises(services.SittingValidationError) as exc:
        services.add_or_update_paper(
            ACTOR, draft_sitting,
            {
                "subject_code": "CIV", "subject_name": "Civ", "mode": "cbt",
                "total_marks": 100, "pass_mark": Decimal("85.00"),
                "blueprint_version": blueprint_versions["CIV"],
            },
        )
    assert exc.value.code == "OUT_OF_POLICY_PASS_STANDARD"


def test_section_marks_must_sum_to_total(draft_sitting, blueprint_versions):
    with pytest.raises(services.SittingValidationError) as exc:
        services.add_or_update_paper(
            ACTOR, draft_sitting,
            {
                "subject_code": "CIV", "subject_name": "Civ", "mode": "cbt",
                "total_marks": 100, "pass_mark": Decimal("50.00"),
                "sections": [{"name": "A", "marks": 40}, {"name": "B", "marks": 50}],
                "blueprint_version": blueprint_versions["CIV"],
            },
        )
    assert exc.value.code == "INCONSISTENT_MARKS_ALLOCATION"


def test_compensated_rule_requires_both_thresholds(draft_sitting):
    draft_sitting.pass_rule = Sitting.PassRule.COMPENSATED
    with pytest.raises(services.SittingValidationError) as exc:
        services._validate_compensation(draft_sitting)
    assert exc.value.code == "INCONSISTENT_COMPENSATION_RULE"


# ── Lifecycle ─────────────────────────────────────────────────────────────-


def test_full_lifecycle(draft_sitting, blueprint_versions):
    _attach_five_papers(draft_sitting, blueprint_versions)

    s = services.configure_sitting(ACTOR, draft_sitting)
    assert s.status == Sitting.Status.CONFIGURED

    s = services.approve_sitting(ACTOR, s, meeting_id=MEETING_ID)
    assert s.approved_at is not None
    assert s.approved_via_meeting_id == MEETING_ID

    s = services.lock_sitting(actor_id=None, sitting=s)
    assert s.status == Sitting.Status.LOCKED
    assert s.locked_at is not None

    # Convenience pointer was written + event recorded.
    assert SittingLock.objects.filter(sitting=s).exists()
    assert SittingLockEvent.objects.filter(
        sitting=s, kind=SittingLockEvent.Kind.AUTO_LOCK,
    ).exists()

    s = services.activate_sitting(ACTOR, s)
    assert s.status == Sitting.Status.ACTIVE

    s = services.close_sitting(ACTOR, s)
    assert s.status == Sitting.Status.CLOSED


def test_lock_requires_approval(draft_sitting, blueprint_versions):
    _attach_five_papers(draft_sitting, blueprint_versions)
    services.configure_sitting(ACTOR, draft_sitting)
    with pytest.raises(services.SittingValidationError) as exc:
        services.lock_sitting(actor_id=None, sitting=draft_sitting)
    assert exc.value.code == "NOT_APPROVED"


def test_lock_is_idempotent(draft_sitting, blueprint_versions):
    _attach_five_papers(draft_sitting, blueprint_versions)
    services.configure_sitting(ACTOR, draft_sitting)
    services.approve_sitting(ACTOR, draft_sitting, meeting_id=MEETING_ID)
    services.lock_sitting(actor_id=None, sitting=draft_sitting)
    # Second call must not raise and must not double-record the convenience row.
    services.lock_sitting(actor_id=None, sitting=draft_sitting)
    assert SittingLock.objects.filter(sitting=draft_sitting).count() == 1


def test_edit_after_configure_drops_back_to_draft(draft_sitting, blueprint_versions):
    _attach_five_papers(draft_sitting, blueprint_versions)
    services.configure_sitting(ACTOR, draft_sitting)
    assert draft_sitting.status == Sitting.Status.CONFIGURED

    services.update_sitting_draft(
        ACTOR, draft_sitting, {"normalisation_method": "linear"},
    )
    draft_sitting.refresh_from_db()
    assert draft_sitting.status == Sitting.Status.DRAFT


# ── Amendments ────────────────────────────────────────────────────────────-


def _approved_and_locked(draft_sitting, blueprint_versions):
    _attach_five_papers(draft_sitting, blueprint_versions)
    services.configure_sitting(ACTOR, draft_sitting)
    services.approve_sitting(ACTOR, draft_sitting, meeting_id=MEETING_ID)
    services.lock_sitting(actor_id=None, sitting=draft_sitting)
    draft_sitting.refresh_from_db()
    return draft_sitting


def test_pre_lock_edit_after_lock_is_blocked(draft_sitting, blueprint_versions):
    s = _approved_and_locked(draft_sitting, blueprint_versions)
    with pytest.raises(services.SittingValidationError) as exc:
        services.update_sitting_draft(ACTOR, s, {"normalisation_method": "linear"})
    assert exc.value.code == "CONFIGURATION_LOCKED"


def test_chair_amendment_blocks_critical_fields(draft_sitting, blueprint_versions):
    s = _approved_and_locked(draft_sitting, blueprint_versions)
    with pytest.raises(services.SittingValidationError) as exc:
        services.amend_non_critical(
            CHAIR, s,
            changes={"pass_mark": Decimal("55.00")},
            justification="Standard correction for the cycle",
        )
    assert exc.value.code == "CRITICAL_FIELD_REQUIRES_RESOLUTION"


def test_chair_amendment_allows_non_critical(draft_sitting, blueprint_versions):
    s = _approved_and_locked(draft_sitting, blueprint_versions)
    services.amend_non_critical(
        CHAIR, s,
        changes={"centres": ["GSL-ACCRA", "GSL-KUMASI"]},
        justification="Centre roster update before sitting day",
    )
    s.refresh_from_db()
    assert s.centres == ["GSL-ACCRA", "GSL-KUMASI"]
    assert SittingLockEvent.objects.filter(
        sitting=s, kind=SittingLockEvent.Kind.CHAIR_AMEND,
    ).exists()


def test_critical_amendment_requires_resolution_and_justification(
    draft_sitting, blueprint_versions,
):
    s = _approved_and_locked(draft_sitting, blueprint_versions)
    with pytest.raises(services.SittingValidationError) as exc:
        services.amend_critical_with_resolution(
            CHAIR, s,
            changes={"pass_mark": Decimal("55.00")},
            resolution_ref="",
            justification="x" * 40,
        )
    assert exc.value.code == "MISSING_RESOLUTION_REF"

    with pytest.raises(services.SittingValidationError) as exc:
        services.amend_critical_with_resolution(
            CHAIR, s,
            changes={"pass_mark": Decimal("55.00")},
            resolution_ref="MIN-2027-005",
            justification="too short",
        )
    assert exc.value.code == "MISSING_JUSTIFICATION"


def test_critical_amendment_applies_change(draft_sitting, blueprint_versions):
    s = _approved_and_locked(draft_sitting, blueprint_versions)
    services.amend_critical_with_resolution(
        CHAIR, s,
        changes={"pass_mark": Decimal("55.00")},
        resolution_ref="MIN-2027-005",
        justification="Board resolution to raise the default pass mark by five points.",
    )
    s.refresh_from_db()
    assert s.pass_mark == Decimal("55.00")
    assert SittingLockEvent.objects.filter(
        sitting=s, kind=SittingLockEvent.Kind.RESOLUTION_AMEND,
        resolution_ref="MIN-2027-005",
    ).exists()


# ── Snapshot ──────────────────────────────────────────────────────────────-


def test_snapshot_shape(draft_sitting, blueprint_versions):
    _attach_five_papers(draft_sitting, blueprint_versions)
    snap = services.get_sitting_snapshot(draft_sitting.ref)
    assert snap["ref"] == draft_sitting.ref
    assert snap["status"] == Sitting.Status.DRAFT
    assert len(snap["papers"]) == 5
    assert {p["subject_code"] for p in snap["papers"]} == set(SUBJECTS)


def test_snapshot_is_stable_across_chair_amendments(draft_sitting, blueprint_versions):
    """SRS §4.4 acceptance — snapshot is identical regardless of non-critical
    Chair amendments applied between lock and the consumer's call."""
    s = _approved_and_locked(draft_sitting, blueprint_versions)
    before = services.get_sitting_snapshot(s.ref)

    services.amend_non_critical(
        CHAIR, s,
        changes={"centres": ["GSL-ACCRA", "GSL-KUMASI"]},
        justification="Late centre allocation update.",
    )

    after = services.get_sitting_snapshot(s.ref)
    # The live row changed; the snapshot must not.
    assert after == before
    s.refresh_from_db()
    assert s.centres == ["GSL-ACCRA", "GSL-KUMASI"]


def test_snapshot_rebuilds_after_resolution_amendment(draft_sitting, blueprint_versions):
    """A resolution-backed critical amendment legitimately rebuilds the snapshot."""
    s = _approved_and_locked(draft_sitting, blueprint_versions)
    before = services.get_sitting_snapshot(s.ref)

    services.amend_critical_with_resolution(
        CHAIR, s,
        changes={"pass_mark": Decimal("55.00")},
        resolution_ref="MIN-2027-007",
        justification="Board resolution to lift the default pass mark by five points.",
    )

    after = services.get_sitting_snapshot(s.ref)
    assert after != before
    assert after["pass_mark"] == "55.00"


# ── Blueprint versioning ──────────────────────────────────────────────────-


def test_publish_blueprint_version_auto_increments(db):
    v1 = services.publish_blueprint_version(
        ACTOR, "CIV",
        {"topic_coverage": {"core": 1.0},
         "cognitive_distribution": {"Knowledge": 1.0},
         "difficulty_distribution": {"Medium": 1.0},
         "total_marks": 100},
    )
    v2 = services.publish_blueprint_version(
        ACTOR, "CIV",
        {"topic_coverage": {"core": 1.0},
         "cognitive_distribution": {"Knowledge": 1.0},
         "difficulty_distribution": {"Medium": 1.0},
         "total_marks": 100},
    )
    assert v1.version_no == 1
    assert v2.version_no == 2
