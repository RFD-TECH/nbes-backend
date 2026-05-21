"""apps/sitting/tests/test_tasks.py — Celery beat handlers for T-30 and reminders."""
import datetime
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.sitting import services, tasks
from apps.sitting.models import Sitting, SittingLockEvent, SubjectPaper


ACTOR = uuid.UUID("00000000-0000-0000-0000-000000000001")
MEETING_ID = uuid.uuid4()
SUBJECTS = ["CIV", "CRIM", "EVID", "ETHICS", "PROP"]


@pytest.fixture
def blueprint_versions(db):
    return {
        code: services.publish_blueprint_version(
            ACTOR, code,
            {"topic_coverage": {"core": 1.0},
             "cognitive_distribution": {"Knowledge": 1.0},
             "difficulty_distribution": {"Medium": 1.0},
             "total_marks": 100},
        )
        for code in SUBJECTS
    }


def _make_approved_sitting(ref: str, sitting_date: datetime.date, blueprint_versions):
    sitting = services.create_sitting(
        ACTOR,
        {
            "ref": ref,
            "sitting_date": sitting_date,
            "sitting_end_date": sitting_date + datetime.timedelta(days=4),
            "pass_mark": Decimal("50.00"),
        },
    )
    for code in SUBJECTS:
        services.add_or_update_paper(
            ACTOR, sitting,
            {
                "subject_code": code, "subject_name": code,
                "mode": SubjectPaper.Mode.CBT, "total_marks": 100,
                "pass_mark": Decimal("50.00"),
                "blueprint_version": blueprint_versions[code],
            },
        )
    services.configure_sitting(ACTOR, sitting)
    services.approve_sitting(ACTOR, sitting, meeting_id=MEETING_ID)
    return sitting


# ── T-30 lock monitor ──────────────────────────────────────────────────────


def test_monitor_t30_locks_eligible_sitting(blueprint_versions):
    today = timezone.localdate()
    s = _make_approved_sitting(
        "BAR-2027-06", today + datetime.timedelta(days=30), blueprint_versions,
    )
    assert s.status == Sitting.Status.CONFIGURED

    summary = tasks.monitor_t30_lock()
    assert s.ref in summary["locked"]
    s.refresh_from_db()
    assert s.status == Sitting.Status.LOCKED
    assert SittingLockEvent.objects.filter(
        sitting=s, kind=SittingLockEvent.Kind.AUTO_LOCK,
    ).exists()


def test_monitor_t30_ignores_far_future_sittings(blueprint_versions):
    today = timezone.localdate()
    s = _make_approved_sitting(
        "BAR-2027-08", today + datetime.timedelta(days=90), blueprint_versions,
    )

    summary = tasks.monitor_t30_lock()
    assert s.ref not in summary["locked"]
    s.refresh_from_db()
    assert s.status == Sitting.Status.CONFIGURED


def test_monitor_t30_is_idempotent(blueprint_versions):
    today = timezone.localdate()
    _make_approved_sitting(
        "BAR-2027-07", today + datetime.timedelta(days=29), blueprint_versions,
    )
    # First run locks; second run is a no-op (no new audit row).
    tasks.monitor_t30_lock()
    first_run_audits = AuditEvent.objects.filter(action="SITTING_LOCKED").count()
    tasks.monitor_t30_lock()
    second_run_audits = AuditEvent.objects.filter(action="SITTING_LOCKED").count()
    assert second_run_audits == first_run_audits


def test_monitor_t30_skips_unapproved_sittings(blueprint_versions):
    today = timezone.localdate()
    sitting = services.create_sitting(
        ACTOR,
        {
            "ref": "BAR-2027-09",
            "sitting_date": today + datetime.timedelta(days=10),
            "sitting_end_date": today + datetime.timedelta(days=14),
        },
    )
    for code in SUBJECTS:
        services.add_or_update_paper(
            ACTOR, sitting,
            {
                "subject_code": code, "subject_name": code,
                "mode": SubjectPaper.Mode.CBT, "total_marks": 100,
                "pass_mark": Decimal("50.00"),
                "blueprint_version": blueprint_versions[code],
            },
        )
    services.configure_sitting(ACTOR, sitting)
    # Deliberately do NOT approve.

    summary = tasks.monitor_t30_lock()
    # Unapproved sittings are filtered out at query level — they don't even
    # reach the lock_sitting call.
    assert sitting.ref not in summary["locked"]
    assert all(e["ref"] != sitting.ref for e in summary["errored"])
    sitting.refresh_from_db()
    assert sitting.status == Sitting.Status.CONFIGURED


# ── T-45 / T-35 / T-31 reminders ──────────────────────────────────────────


def test_reminders_fire_at_t45_t35_t31(blueprint_versions):
    today = timezone.localdate()
    sittings = {
        offset: _make_approved_sitting(
            f"BAR-2027-{10 + offset:02d}",
            today + datetime.timedelta(days=offset),
            blueprint_versions,
        )
        for offset in tasks.REMINDER_OFFSETS_DAYS
    }

    summary = tasks.send_t30_reminders()
    sent_refs = {entry["ref"] for entry in summary["sent"]}
    for offset, s in sittings.items():
        assert s.ref in sent_refs, f"Expected reminder for T-{offset} sitting {s.ref}"


def test_reminders_are_idempotent_same_day(blueprint_versions):
    today = timezone.localdate()
    _make_approved_sitting(
        "BAR-2027-20", today + datetime.timedelta(days=45), blueprint_versions,
    )

    first = tasks.send_t30_reminders()
    second = tasks.send_t30_reminders()
    assert len(first["sent"]) == 1
    assert len(second["sent"]) == 0
    assert any(s["reason"] == "already_sent" for s in second["skipped"])


def test_reminders_skip_closed_sittings(blueprint_versions):
    today = timezone.localdate()
    sitting = _make_approved_sitting(
        "BAR-2027-21", today + datetime.timedelta(days=45), blueprint_versions,
    )
    # Skip past LOCKED → ACTIVE → CLOSED to simulate a sitting already done.
    services.lock_sitting(actor_id=None, sitting=sitting)
    services.activate_sitting(ACTOR, sitting)
    services.close_sitting(ACTOR, sitting)

    summary = tasks.send_t30_reminders()
    assert all(entry["ref"] != sitting.ref for entry in summary["sent"])
