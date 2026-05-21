"""apps/sitting/variants.py — Deterministic paper variant generation (F03-05).

Variants are produced per :class:`apps.sitting.models.SubjectPaper`:

* Each variant is keyed by a ``seed`` (BigInteger) so the exact ordering can
  be reproduced bit-for-bit for audit.
* The generator selects items from a candidate pool — by default the pool is
  Phase 3 ``Item`` rows in status ``LOCKED_FOR_USE`` for the paper's
  ``blueprint_version.subject_code`` and not used in the last
  ``cool_down_sittings`` sittings.
* Each generated variant is run through the **blueprint coverage validator**.
  When Phase 3 ships ``validate_blueprint_coverage`` (in either
  ``apps.itembank.services`` or ``shared.blueprint``) we'll resolve it via
  :func:`_resolve_validator`; until then we fall back to a permissive stub
  that returns PASSED with a marker so the Phase 4 demo can run end-to-end.

CBT papers do not store discrete variants — per-candidate question order and
MCQ option order are computed at delivery time using the candidate's
``candidate_index + sitting_ref`` as the seed. PBT papers materialise
multiple variants here, one row per physical paper layout.
"""
from __future__ import annotations

import hashlib
import random
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

from django.db import transaction

from apps.audit.models import AuditEvent
from shared.events import publish

from . import events as ev
from .models import SubjectPaper, Variant


# ── Validator resolution ──────────────────────────────────────────────────-


@dataclass
class CoverageReport:
    """Shape returned by the (eventual) Phase 3 blueprint coverage validator.

    Phase 4 only consumes this — the canonical implementation should live
    next to paper construction in ``apps.itembank.services``. Mirrors the
    contract negotiated in the Phase 3 hand-off.
    """

    valid: bool
    total_marks_actual: int = 0
    total_marks_expected: int = 0
    topic_coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    cognitive_level_distribution: dict[str, dict[str, Any]] = field(default_factory=dict)
    difficulty_distribution: dict[str, dict[str, Any]] = field(default_factory=dict)
    section_structure: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    used_stub: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "total_marks_actual": self.total_marks_actual,
            "total_marks_expected": self.total_marks_expected,
            "topic_coverage": self.topic_coverage,
            "cognitive_level_distribution": self.cognitive_level_distribution,
            "difficulty_distribution": self.difficulty_distribution,
            "section_structure": self.section_structure,
            "violations": self.violations,
            "used_stub": self.used_stub,
        }


ValidatorFn = Callable[..., CoverageReport]


def _resolve_validator() -> ValidatorFn:
    """Try to import the real Phase 3 validator; fall back to the stub.

    The Phase 3 hand-off agreed on this contract::

        validate_blueprint_coverage(items, blueprint, tolerance=0.05) -> Report

    We probe a small set of plausible locations so we don't break when Phase 3
    lands the function. The stub never fails — Phase 4 demos run, and any
    real validation kicks in the moment Phase 3 merges its implementation.
    """
    candidates = (
        ("apps.itembank.services", "validate_blueprint_coverage"),
        ("shared.blueprint", "validate_blueprint_coverage"),
        ("workflow.guards", "validate_blueprint_coverage"),
    )
    for module_path, attr in candidates:
        try:
            module = __import__(module_path, fromlist=[attr])
            fn = getattr(module, attr, None)
            if callable(fn):
                return fn
        except ImportError:
            continue
    return _stub_validator


def _stub_validator(items, blueprint, tolerance=0.05, **_) -> CoverageReport:
    """Permissive stub — returns PASSED with a ``used_stub`` marker.

    Replace with the Phase 3 implementation. Until then this lets Phase 4
    integrate, build variants, and exercise the audit / outbox plumbing
    without blocking on Phase 3.
    """
    return CoverageReport(
        valid=True,
        total_marks_actual=sum(getattr(i, "marks", 0) or 0 for i in items),
        total_marks_expected=getattr(blueprint, "total_marks", 0) or 0,
        used_stub=True,
    )


# ── Candidate pool resolution ─────────────────────────────────────────────-


def _resolve_item_pool_fn() -> Callable[..., list]:
    """Same probing strategy for the item-pool query.

    Phase 3 will expose a helper that returns the locked-for-use items
    eligible for a paper (filtered by subject, blueprint, cool-down). Until
    then, we try to read Items directly from ``apps.itembank.models``.
    """
    try:
        from apps.itembank.services import locked_items_for_paper  # type: ignore
        return locked_items_for_paper
    except ImportError:
        pass
    return _stub_item_pool


def _stub_item_pool(paper: SubjectPaper, cool_down_sittings: int = 3) -> list:
    """Minimal item-pool implementation that queries itembank.Item directly.

    Skipped silently if itembank.Item is not importable (early-Phase-3 state).
    """
    try:
        from apps.itembank.models import Item  # type: ignore
    except ImportError:
        return []

    blueprint = paper.blueprint_version
    if blueprint is None:
        return []

    # We deliberately keep the filter narrow — anything more elaborate
    # (cool-down enforcement, distractor analytics) belongs to Phase 3.
    qs = Item.objects.filter(subject=blueprint.subject_code)
    status_field = getattr(Item, "Status", None)
    if status_field is not None and hasattr(status_field, "LOCKED_FOR_USE"):
        qs = qs.filter(status=status_field.LOCKED_FOR_USE)
    return list(qs)


# ── Seed handling ────────────────────────────────────────────────────────-


def _derive_seed(paper: SubjectPaper, variant_no: int, supplied_seed: int | None) -> int:
    """Choose / derive a deterministic seed.

    If the caller supplies a seed, we use it verbatim. Otherwise we derive a
    repeatable seed from ``(paper.id, variant_no)`` so the same combination
    always produces the same variant — important for audit, less important
    for unpredictability since the pool itself is the secret.
    """
    if supplied_seed is not None:
        return int(supplied_seed)

    digest = hashlib.sha256(
        f"{paper.id}:{variant_no}".encode("utf-8")
    ).digest()
    # 63-bit positive int fits comfortably in BigIntegerField.
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


# ── Public API ────────────────────────────────────────────────────────────-


@dataclass
class GenerateResult:
    """Outcome of :func:`generate_variants` — what was created and what failed."""

    created: list[Variant] = field(default_factory=list)
    rejected: list[Variant] = field(default_factory=list)
    used_validator_stub: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "created_variant_ids": [str(v.id) for v in self.created],
            "rejected_variant_ids": [str(v.id) for v in self.rejected],
            "used_validator_stub": self.used_validator_stub,
        }


@transaction.atomic
def generate_variants(
    actor_id,
    paper: SubjectPaper,
    *,
    count: int = 4,
    seeds: list[int] | None = None,
    request_id=None,
    ip_address=None,
) -> GenerateResult:
    """Generate ``count`` variants for ``paper``.

    Variants are persisted whether they pass or fail validation — failed
    variants carry ``validation_status=FAILED`` and ``failed_constraints``
    populated so an Auditor can later inspect *why* a variant was rejected.

    Returns a :class:`GenerateResult` with the persisted rows split into
    ``created`` (PASSED) and ``rejected`` (FAILED).
    """
    if paper.blueprint_version is None:
        raise ValueError(
            f"Paper {paper.subject_code} has no blueprint_version; assign one "
            "before generating variants."
        )
    if seeds is not None and len(seeds) != count:
        raise ValueError("len(seeds) must equal count when seeds are supplied.")

    validator = _resolve_validator()
    item_pool_fn = _resolve_item_pool_fn()
    pool = list(item_pool_fn(paper))

    result = GenerateResult()

    # variant_no starts after the highest existing one so re-running the
    # generator is additive, not destructive.
    last_no = (
        paper.variants.order_by("-variant_no")
        .values_list("variant_no", flat=True)
        .first()
        or 0
    )

    for i in range(count):
        variant_no = last_no + i + 1
        seed = _derive_seed(paper, variant_no, seeds[i] if seeds else None)
        items_order = _shuffle_pool(pool, seed)
        report = validator(
            items_order,
            paper.blueprint_version,
            tolerance=float(paper.blueprint_version.tolerance),
        )
        # Allow either a CoverageReport or a duck-typed dict from Phase 3.
        report_dict, valid, violations, used_stub = _normalise_report(report)

        variant = Variant.objects.create(
            paper=paper,
            variant_no=variant_no,
            seed=seed,
            items=[_item_id(item) for item in items_order],
            coverage_report=report_dict,
            failed_constraints=violations if not valid else [],
            validation_status=(
                Variant.ValidationStatus.PASSED if valid
                else Variant.ValidationStatus.FAILED
            ),
            generated_by_id=actor_id,
        )
        (result.created if valid else result.rejected).append(variant)
        if used_stub:
            result.used_validator_stub = True

        _audit_variant(
            actor_id, variant, valid,
            request_id=request_id, ip_address=ip_address,
        )

    publish(
        "VariantsGenerated",
        {
            "paper_id": str(paper.id),
            "sitting_ref": paper.sitting_id,
            "created": len(result.created),
            "rejected": len(result.rejected),
            "used_validator_stub": result.used_validator_stub,
        },
    )
    return result


# ── Internals ────────────────────────────────────────────────────────────-


def _shuffle_pool(pool: list, seed: int) -> list:
    """Deterministically reorder the pool. Pure ``random.Random(seed).shuffle``."""
    if not pool:
        return []
    rng = random.Random(seed)
    ordered = list(pool)
    rng.shuffle(ordered)
    return ordered


def _item_id(item) -> str:
    """Best-effort id extraction — accepts Items, dicts, UUIDs, or strings."""
    pk = getattr(item, "pk", None) or getattr(item, "id", None) or item
    return str(pk)


def _normalise_report(report) -> tuple[dict, bool, list[str], bool]:
    """Accept either CoverageReport, dict, or duck-typed object."""
    if isinstance(report, CoverageReport):
        return report.as_json(), report.valid, list(report.violations), report.used_stub
    if isinstance(report, dict):
        return (
            report,
            bool(report.get("valid", False)),
            list(report.get("violations", []) or []),
            bool(report.get("used_stub", False)),
        )
    # Duck-typed object.
    return (
        getattr(report, "as_json", lambda: {"valid": False, "violations": ["Unknown report shape"]})(),
        bool(getattr(report, "valid", False)),
        list(getattr(report, "violations", []) or []),
        bool(getattr(report, "used_stub", False)),
    )


def _audit_variant(actor_id, variant: Variant, passed: bool, *, request_id, ip_address):
    action = ev.VARIANT_GENERATED if passed else ev.VARIANT_REJECTED
    AuditEvent.record(
        actor_id=actor_id,
        action=action,
        entity_type="variant",
        entity_id=variant.id,
        new_state={
            "paper_id": str(variant.paper_id),
            "variant_no": variant.variant_no,
            "validation_status": variant.validation_status,
            "seed": variant.seed,
        },
        request_id=request_id,
        ip_address=ip_address,
    )


def regenerate_variant_for_audit(variant: Variant) -> list[str]:
    """Reproduce a variant's item ordering from its persisted seed.

    Used by Auditors to verify that a stored ``items`` list matches what the
    generator would produce today. Returns the list of item ids — comparing
    against ``variant.items`` confirms reproducibility.
    """
    item_pool_fn = _resolve_item_pool_fn()
    pool = list(item_pool_fn(variant.paper))
    return [_item_id(item) for item in _shuffle_pool(pool, variant.seed)]
