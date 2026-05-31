"""apps/sitting/tests/test_variants.py — Deterministic variant generation."""
import datetime
import uuid
from decimal import Decimal

import pytest

from apps.sitting import services, variants
from apps.sitting.blueprint import validate_blueprint_coverage
from apps.sitting.models import SubjectPaper, Variant


ACTOR = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def paper(db):
    sitting = services.create_sitting(
        ACTOR,
        {
            "ref": "BAR-2027-11",
            "sitting_date": datetime.date(2027, 11, 1),
            "sitting_end_date": datetime.date(2027, 11, 5),
        },
    )
    bp = services.publish_blueprint_version(
        ACTOR, "CIV",
        {
            "topic_coverage": {"core": 1.0},
            "cognitive_distribution": {"Knowledge": 1.0},
            "difficulty_distribution": {"Medium": 1.0},
            "sections": [{"name": "A", "marks": 100}],
            "total_marks": 100,
        },
    )
    return services.add_or_update_paper(
        ACTOR, sitting,
        {
            "subject_code": "CIV", "subject_name": "Civil",
            "mode": SubjectPaper.Mode.PBT, "total_marks": 100,
            "pass_mark": Decimal("50.00"),
            "blueprint_version": bp,
        },
    )


# ── Validator ──────────────────────────────────────────────────────────────


def test_blueprint_validator_passes_perfect_paper():
    class FakeItem:
        def __init__(self, marks, **kw):
            self.marks = marks
            for k, v in kw.items():
                setattr(self, k, v)

    class FakeBlueprint:
        topic_coverage = {"Contract": 0.5, "Tort": 0.5}
        cognitive_distribution = {"Knowledge": 1.0}
        difficulty_distribution = {"Medium": 1.0}
        sections = []
        total_marks = 100
        tolerance = Decimal("0.05")

    items = [
        FakeItem(50, topic="Contract", cognitive_level="Knowledge", difficulty="Medium"),
        FakeItem(50, topic="Tort", cognitive_level="Knowledge", difficulty="Medium"),
    ]
    report = validate_blueprint_coverage(items, FakeBlueprint())
    assert report.valid, report.violations
    assert report.total_marks_actual == 100


def test_blueprint_validator_flags_topic_drift():
    class FakeItem:
        def __init__(self, marks, topic):
            self.marks = marks
            self.topic = topic

    class FakeBlueprint:
        topic_coverage = {"Contract": 0.5, "Tort": 0.5}
        cognitive_distribution = {}
        difficulty_distribution = {}
        sections = []
        total_marks = 100
        tolerance = Decimal("0.05")

    # 90/10 split breaks the 50/50 target by far more than 5%.
    items = [FakeItem(90, "Contract"), FakeItem(10, "Tort")]
    report = validate_blueprint_coverage(items, FakeBlueprint())
    assert not report.valid
    assert any("Contract" in v for v in report.violations)


# ── Deterministic generation ──────────────────────────────────────────────-


def test_generate_variants_with_explicit_seed_is_deterministic(paper):
    """Two runs with the same seed must produce identical item ordering."""
    seeds = [42424242]
    result1 = variants.generate_variants(ACTOR, paper, count=1, seeds=seeds)
    # Wipe the produced variant and regenerate.
    Variant.objects.filter(paper=paper).delete()
    result2 = variants.generate_variants(ACTOR, paper, count=1, seeds=seeds)

    v1 = result1.created[0] if result1.created else result1.rejected[0]
    v2 = result2.created[0] if result2.created else result2.rejected[0]
    assert v1.seed == v2.seed
    assert v1.items == v2.items  # identical ordering


def test_generate_variants_records_audit_and_publishes(paper):
    from apps.audit.models import AuditEvent, OutboxEvent

    result = variants.generate_variants(ACTOR, paper, count=2)
    assert len(result.created) + len(result.rejected) == 2
    # Per-variant audit events.
    audit_actions = AuditEvent.objects.filter(
        entity_type="variant",
    ).values_list("action", flat=True)
    assert len(audit_actions) == 2
    # One aggregate VariantsGenerated outbox event.
    assert OutboxEvent.objects.filter(event_name="VariantsGenerated").exists()


def test_regenerate_variant_for_audit_matches_stored_order(paper):
    result = variants.generate_variants(ACTOR, paper, count=1, seeds=[7777])
    variant = (result.created + result.rejected)[0]
    replay = variants.regenerate_variant_for_audit(variant)
    assert replay == variant.items


def test_generate_variants_requires_blueprint(db):
    sitting = services.create_sitting(
        ACTOR,
        {
            "ref": "BAR-2027-12",
            "sitting_date": datetime.date(2027, 12, 1),
            "sitting_end_date": datetime.date(2027, 12, 5),
        },
    )
    paper = services.add_or_update_paper(
        ACTOR, sitting,
        {"subject_code": "CIV", "subject_name": "Civil", "mode": "pbt"},
    )
    with pytest.raises(ValueError, match="blueprint_version"):
        variants.generate_variants(ACTOR, paper, count=1)


def test_generate_variants_validates_seed_count(paper):
    with pytest.raises(ValueError, match="len\\(seeds\\)"):
        variants.generate_variants(ACTOR, paper, count=2, seeds=[1, 2, 3])


def test_variant_numbers_are_additive(paper):
    """Re-running the generator on a paper that already has variants must
    append, not collide on variant_no."""
    variants.generate_variants(ACTOR, paper, count=2)
    variants.generate_variants(ACTOR, paper, count=2)
    nos = sorted(Variant.objects.filter(paper=paper).values_list("variant_no", flat=True))
    assert nos == [1, 2, 3, 4]
