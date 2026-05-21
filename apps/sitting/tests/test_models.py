"""apps/sitting/tests/test_models.py — model invariants and field defaults."""
import datetime
import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.sitting.models import (
    BlueprintVersion,
    Sitting,
    SittingLockEvent,
    SubjectPaper,
    Variant,
)


ACTOR = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def sitting(db):
    return Sitting.objects.create(
        ref="BAR-2026-09",
        sitting_date=datetime.date(2026, 9, 1),
        sitting_end_date=datetime.date(2026, 9, 5),
        created_by_id=ACTOR,
    )


def test_sitting_defaults(sitting):
    assert sitting.status == Sitting.Status.DRAFT
    assert sitting.pass_rule == Sitting.PassRule.ALL_PASS
    assert sitting.pass_band_min == Decimal("40.00")
    assert sitting.pass_band_max == Decimal("70.00")
    assert sitting.centres == []
    assert sitting.is_amendable is True
    assert sitting.is_locked is False


def test_sitting_is_locked_predicates(sitting):
    sitting.status = Sitting.Status.LOCKED
    assert sitting.is_locked is True
    assert sitting.is_amendable is False
    sitting.status = Sitting.Status.ACTIVE
    assert sitting.is_locked is True
    sitting.status = Sitting.Status.CLOSED
    assert sitting.is_locked is True


def test_subject_paper_unique_per_sitting(sitting):
    SubjectPaper.objects.create(
        sitting=sitting, subject_code="CIV", subject_name="Civil Procedure",
    )
    # ``unique_together`` raises IntegrityError on both PostgreSQL and SQLite.
    # Wrap the failing insert in its own atomic block so the broken transaction
    # state doesn't poison the rest of the test.
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SubjectPaper.objects.create(
                sitting=sitting, subject_code="CIV", subject_name="dup",
            )


def test_blueprint_version_unique_per_subject(db):
    BlueprintVersion.objects.create(subject_code="CIV", version_no=1)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            BlueprintVersion.objects.create(subject_code="CIV", version_no=1)


def test_variant_unique_per_paper(sitting):
    paper = SubjectPaper.objects.create(
        sitting=sitting, subject_code="CIV", subject_name="Civil",
    )
    Variant.objects.create(paper=paper, variant_no=1, seed=123)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Variant.objects.create(paper=paper, variant_no=1, seed=999)


def test_lock_event_kinds(sitting):
    e = SittingLockEvent.objects.create(
        sitting=sitting, kind=SittingLockEvent.Kind.AUTO_LOCK,
    )
    assert e.kind == "auto_lock"
    assert e.affected_fields == []
    assert e.before_snapshot == {}
