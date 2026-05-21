"""apps/sitting/blueprint.py — Blueprint coverage validator (F03-04).

Owns ``validate_blueprint_coverage`` for Phase 4. Operates on
:class:`apps.sitting.models.BlueprintVersion` and :class:`apps.itembank.models.Item`
rows. The Phase 3 NBES-14 work shipped a private validator in
``apps.itembank.services._validate_blueprint`` that reads a static settings
catalogue (``settings.NBES_BLUEPRINTS``); that catalogue is the legacy stub
the Phase 3 work itself flagged for replacement.

This module is the replacement. Phase 4 variant generation
(``apps.sitting.variants``) calls into it. Phase 3 paper construction
continues to use its private validator + settings catalogue for backward
compatibility with its existing tests; a follow-up can collapse both
into this module once the catalogue is migrated out of settings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable


@dataclass
class CoverageReport:
    """Result of :func:`validate_blueprint_coverage`.

    ``valid`` is the top-level pass/fail. Individual distribution dicts give
    a per-bucket breakdown so the UI can render *why* a paper failed.
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


# ── Helpers ───────────────────────────────────────────────────────────────-


def _item_attr(item, name, default=None):
    """Attribute lookup that tolerates dicts as well as model instances."""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _item_marks(item) -> int:
    value = _item_attr(item, "marks", 0) or 0
    if isinstance(value, Decimal):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bucket(items: Iterable, key: str) -> dict[str, int]:
    """Sum marks per ``key`` bucket. Empty values map to 'Unspecified'."""
    out: dict[str, int] = {}
    for item in items:
        bucket = _item_attr(item, key) or "Unspecified"
        out[bucket] = out.get(bucket, 0) + _item_marks(item)
    return out


def _within(actual_share: float, target_share: float, tolerance: float) -> bool:
    return abs(actual_share - target_share) <= tolerance


# ── Public entry point ────────────────────────────────────────────────────-


def validate_blueprint_coverage(
    items,
    blueprint,
    tolerance: float | Decimal | None = None,
) -> CoverageReport:
    """Validate ``items`` against ``blueprint``.

    Args:
        items: iterable of ``apps.itembank.models.Item`` instances (or dicts
            duck-typed with ``subject``, ``topic``, ``cognitive_level``,
            ``difficulty``, ``marks``).
        blueprint: a ``BlueprintVersion`` instance (or any object exposing
            the same JSON-field shape: ``topic_coverage``,
            ``cognitive_distribution``, ``difficulty_distribution``,
            ``sections``, ``total_marks``, ``tolerance``).
        tolerance: per-bucket ±share permitted (e.g. 0.05 = ±5%). Falls back
            to ``blueprint.tolerance``.

    Always returns a :class:`CoverageReport` — never raises. The caller
    (variant generator, paper builder) decides what to do with a failed
    report (reject the variant, surface the violations to the user).
    """
    items = list(items or [])
    tol = float(tolerance if tolerance is not None else getattr(blueprint, "tolerance", 0.05) or 0.05)
    total_expected = int(getattr(blueprint, "total_marks", 0) or 0)
    total_actual = sum(_item_marks(i) for i in items)

    report = CoverageReport(
        valid=True,
        total_marks_actual=total_actual,
        total_marks_expected=total_expected,
        section_structure=list(getattr(blueprint, "sections", []) or []),
    )

    # Marks total — strict equality (Phase 3 paper construction enforces the
    # same rule; we mirror it here so variants stay self-consistent).
    if total_expected and total_actual != total_expected:
        report.violations.append(
            f"Total marks {total_actual} does not match blueprint total {total_expected}."
        )

    # Per-bucket distribution checks.
    _check_distribution(
        report, items, total_actual, tol,
        key="topic", label_key="topic_coverage",
        target=getattr(blueprint, "topic_coverage", {}) or {},
        violation_prefix="Topic",
        report_attr="topic_coverage",
    )
    _check_distribution(
        report, items, total_actual, tol,
        key="cognitive_level", label_key="cognitive_distribution",
        target=getattr(blueprint, "cognitive_distribution", {}) or {},
        violation_prefix="Cognitive level",
        report_attr="cognitive_level_distribution",
    )
    _check_distribution(
        report, items, total_actual, tol,
        key="difficulty", label_key="difficulty_distribution",
        target=getattr(blueprint, "difficulty_distribution", {}) or {},
        violation_prefix="Difficulty",
        report_attr="difficulty_distribution",
    )

    # Section structure — if the blueprint declares sections, the sum of
    # section marks must match total_marks. Per-item section assignment is
    # the responsibility of the paper builder, not this validator.
    sections = getattr(blueprint, "sections", []) or []
    if sections:
        section_total = sum(int(s.get("marks", 0) or 0) for s in sections)
        if total_expected and section_total != total_expected:
            report.violations.append(
                f"Blueprint sections sum to {section_total}, "
                f"expected {total_expected}."
            )

    report.valid = not report.violations
    return report


def _check_distribution(
    report: CoverageReport,
    items: list,
    total_actual: int,
    tolerance: float,
    *,
    key: str,
    label_key: str,
    target: dict,
    violation_prefix: str,
    report_attr: str,
) -> None:
    """Compare actual mark share per bucket to the blueprint's target."""
    if not target:
        return  # blueprint did not declare this dimension — skip silently

    buckets = _bucket(items, key)
    detail: dict[str, dict[str, Any]] = {}
    for bucket, target_share in target.items():
        target_share = float(target_share)
        actual_marks = buckets.get(bucket, 0)
        actual_share = (actual_marks / total_actual) if total_actual else 0.0
        within = _within(actual_share, target_share, tolerance)
        detail[bucket] = {
            "target_share": target_share,
            "actual_share": round(actual_share, 4),
            "actual_marks": actual_marks,
            "delta": round(actual_share - target_share, 4),
            "within_tolerance": within,
        }
        if not within:
            report.violations.append(
                f"{violation_prefix} {bucket}: target {target_share:.2%}, "
                f"actual {actual_share:.2%} (Δ {actual_share - target_share:+.2%})."
            )

    # Flag any buckets that have items but weren't declared in the blueprint.
    for bucket, marks in buckets.items():
        if bucket not in target and bucket != "Unspecified":
            detail[bucket] = {
                "target_share": 0.0,
                "actual_share": round(marks / total_actual, 4) if total_actual else 0.0,
                "actual_marks": marks,
                "delta": round(marks / total_actual, 4) if total_actual else 0.0,
                "within_tolerance": False,
            }
            report.violations.append(
                f"{violation_prefix} {bucket}: not declared in blueprint."
            )

    setattr(report, report_attr, detail)
