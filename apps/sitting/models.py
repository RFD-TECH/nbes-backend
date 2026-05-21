"""apps/sitting/models.py — Exam Sitting configuration and T-30 lock.

Phase 4 — NBE-F03 (Five-Subject Exam Configuration).

Aggregates:
* :class:`Sitting` — top-level examination cycle with T-30 lock state machine.
* :class:`SubjectPaper` — one of the §71-mandated five papers; carries mode,
  marks allocation, sections, pass standard, blueprint reference.
* :class:`BlueprintVersion` — versioned blueprint per subject (topic /
  cognitive level / difficulty distribution). Sittings reference a specific
  version so historical reconstruction is possible.
* :class:`Variant` — deterministic, seed-driven paper variant. Generated
  per :class:`SubjectPaper` for PBT (and per-candidate randomisation for CBT
  is computed at delivery time, not stored as Variant rows).
* :class:`SittingLockEvent` — append-only history of every lock or
  post-lock amendment (auto-lock, chair-amend, resolution-amend).
* :class:`SittingLock` — OneToOne convenience pointer to the T-30 lock
  event (kept for backward compatibility with the initial schema).
"""
import uuid

from django.db import models

from shared.validators import validate_sitting_ref


# ── Sitting ────────────────────────────────────────────────────────────────


class Sitting(models.Model):
    """Examination cycle configuration.

    Lifecycle: DRAFT → CONFIGURED → LOCKED → ACTIVE → CLOSED.

    Auto-locked at midnight T-30 by the Celery beat task
    ``apps.sitting.tasks.monitor_t30_lock``. After lock, only the NBEC Chair
    (non-critical fields) or a full NBEC resolution (critical fields) can
    amend the configuration.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CONFIGURED = "configured", "Configured"
        LOCKED = "locked", "Locked"
        ACTIVE = "active", "Active (Sitting in Progress)"
        CLOSED = "closed", "Closed"

    class PassRule(models.TextChoices):
        ALL_PASS = "all_pass", "All papers individually pass"
        AGGREGATE = "aggregate", "Aggregate threshold"
        COMPENSATED = "compensated", "Compensated pass with cap"

    ref = models.CharField(
        max_length=15,
        primary_key=True,
        validators=[validate_sitting_ref],
        help_text="Format: BAR-YYYY-MM e.g. BAR-2026-05",
    )
    sitting_date = models.DateField()
    sitting_end_date = models.DateField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )

    # Pass standards (NBE-F03-02). pass_mark is the default per-paper standard;
    # individual SubjectPaper rows may override within the NBEC policy band.
    pass_mark = models.DecimalField(max_digits=5, decimal_places=2, default=50.00)
    pass_band_min = models.DecimalField(
        max_digits=5, decimal_places=2, default=40.00,
        help_text="NBEC policy band — minimum permissible per-paper pass standard.",
    )
    pass_band_max = models.DecimalField(
        max_digits=5, decimal_places=2, default=70.00,
        help_text="NBEC policy band — maximum permissible per-paper pass standard.",
    )

    # Overall pass rule (F03-02). Compensated thresholds only apply when
    # pass_rule = COMPENSATED.
    pass_rule = models.CharField(
        max_length=20, choices=PassRule.choices, default=PassRule.ALL_PASS,
    )
    compensated_min_per_paper = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Minimum mark per paper for a compensated pass (F03-02).",
    )
    compensated_aggregate_floor = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Aggregate floor across papers for a compensated pass (F03-02).",
    )

    # Normalisation per paper is configured in SubjectPaper.normalisation_method.
    # This field is the sitting-wide default (legacy; retained).
    normalisation_method = models.CharField(max_length=30, blank=True)

    # Centres available for this sitting (subset of master centre list — Phase 6
    # owns the master list; this is just the per-sitting selection).
    centres = models.JSONField(
        default=list, blank=True,
        help_text="List of centre refs available for this sitting.",
    )

    created_by_id = models.UUIDField()
    locked_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_via_meeting_id = models.UUIDField(
        null=True, blank=True,
        help_text="Phase 2 Meeting that approved the configuration (pre-lock).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sitting_sitting"

    def __str__(self):
        return f"Sitting {self.ref} — {self.get_status_display()}"

    # Convenience predicates used by services and the T-30 monitor.
    @property
    def is_locked(self) -> bool:
        return self.status in {self.Status.LOCKED, self.Status.ACTIVE, self.Status.CLOSED}

    @property
    def is_amendable(self) -> bool:
        """True when configuration can still be edited normally (pre-lock)."""
        return self.status in {self.Status.DRAFT, self.Status.CONFIGURED}


# ── SubjectPaper ───────────────────────────────────────────────────────────


class SubjectPaper(models.Model):
    """One of the five subject papers per sitting (§71)."""

    class Mode(models.TextChoices):
        CBT = "cbt", "Computer-Based Testing"
        PBT = "pbt", "Paper-Based Testing"
        HYBRID = "hybrid", "Hybrid"

    class NormalisationMethod(models.TextChoices):
        NONE = "none", "None"
        LINEAR = "linear", "Linear"
        EQUIPERCENTILE = "equipercentile", "Equipercentile"
        BESPOKE = "bespoke", "NBEC-bespoke"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting = models.ForeignKey(
        Sitting, on_delete=models.CASCADE, related_name="subject_papers",
    )
    subject_code = models.CharField(max_length=20)
    subject_name = models.CharField(max_length=255)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.CBT)
    total_marks = models.PositiveSmallIntegerField(default=100)
    pass_mark = models.DecimalField(max_digits=5, decimal_places=2, default=50.00)
    duration_minutes = models.PositiveSmallIntegerField(default=180)

    # Section structure. Each entry is {"name": str, "marks": int, "time_minutes": int}.
    # Validated by services: sum(marks) MUST equal total_marks (F03-02).
    sections = models.JSONField(
        default=list, blank=True,
        help_text="[{name, marks, time_minutes}] — section marks must sum to total_marks.",
    )

    normalisation_method = models.CharField(
        max_length=30, choices=NormalisationMethod.choices,
        default=NormalisationMethod.NONE,
    )
    normalisation_params = models.JSONField(
        default=dict, blank=True,
        help_text="Method-specific parameters; frozen at T-30 lock.",
    )

    # Blueprint reference — a SubjectPaper points at a specific BlueprintVersion.
    # Nullable during draft authoring; required before status moves to LOCKED.
    blueprint_version = models.ForeignKey(
        "sitting.BlueprintVersion",
        on_delete=models.PROTECT, null=True, blank=True,
        related_name="subject_papers",
    )

    class Meta:
        db_table = "sitting_subjectpaper"
        unique_together = [("sitting", "subject_code")]

    def __str__(self):
        return f"{self.sitting_id} — {self.subject_code} ({self.get_mode_display()})"


# ── BlueprintVersion ───────────────────────────────────────────────────────


class BlueprintVersion(models.Model):
    """Versioned blueprint per subject (F03-04).

    Each ``SubjectPaper`` references a specific version. Phase 3 paper
    construction validates against the referenced version; Phase 4 variant
    generation reuses the same validator (see ``shared.blueprint`` once Phase
    3 lands it). Blueprint changes between sittings are captured as new
    versions — old versions are never mutated.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject_code = models.CharField(max_length=20)
    version_no = models.PositiveIntegerField()

    # Structured distribution targets. Service-layer schema:
    #   topic_coverage:           {"<topic>": <weight 0..1>, ...}  — must sum to 1.0
    #   cognitive_distribution:   {"Knowledge": 0.2, ...}          — must sum to 1.0
    #   difficulty_distribution:  {"Easy": 0.3, ...}               — must sum to 1.0
    #   sections:                 [{"name": str, "marks": int}, ...]
    #   total_marks:              int
    topic_coverage = models.JSONField(default=dict)
    cognitive_distribution = models.JSONField(default=dict)
    difficulty_distribution = models.JSONField(default=dict)
    sections = models.JSONField(default=list, blank=True)
    total_marks = models.PositiveSmallIntegerField(default=100)

    # Coverage tolerance applied during paper construction and variant
    # generation; configurable per blueprint version (0.05 = ±5%).
    tolerance = models.DecimalField(max_digits=4, decimal_places=3, default=0.050)

    description = models.TextField(blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sitting_blueprintversion"
        unique_together = [("subject_code", "version_no")]
        ordering = ["subject_code", "-version_no"]

    def __str__(self):
        return f"{self.subject_code} v{self.version_no}"

    @property
    def is_published(self) -> bool:
        return self.published_at is not None


# ── Variant ────────────────────────────────────────────────────────────────


class Variant(models.Model):
    """A deterministically-generated paper variant (F03-05).

    For PBT papers we materialise multiple variants to reduce copying risk.
    Each variant carries:

    * the random ``seed`` used to generate the item ordering — persisted so
      the variant can be regenerated bit-for-bit for audit;
    * the ordered ``items`` list (item ids) and ``item_order`` map;
    * the ``coverage_report`` produced by the blueprint validator;
    * ``validation_status`` — PASSED variants are eligible for use; FAILED
      variants are retained for audit (with ``failed_constraints``) so we can
      explain *why* the generator rejected them.
    """

    class ValidationStatus(models.TextChoices):
        PENDING = "pending", "Pending Validation"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed Coverage Check"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    paper = models.ForeignKey(
        SubjectPaper, on_delete=models.CASCADE, related_name="variants",
    )
    variant_no = models.PositiveIntegerField()
    seed = models.BigIntegerField(help_text="Deterministic generator seed.")
    items = models.JSONField(
        default=list,
        help_text="Ordered list of item ids (UUIDs as strings).",
    )
    item_order = models.JSONField(
        default=dict, blank=True,
        help_text="Per-item option-order map for MCQ randomisation.",
    )
    coverage_report = models.JSONField(default=dict, blank=True)
    failed_constraints = models.JSONField(default=list, blank=True)
    validation_status = models.CharField(
        max_length=10, choices=ValidationStatus.choices,
        default=ValidationStatus.PENDING,
    )

    generated_by_id = models.UUIDField(null=True, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sitting_variant"
        unique_together = [("paper", "variant_no")]
        ordering = ["paper", "variant_no"]

    def __str__(self):
        return f"{self.paper_id} variant {self.variant_no} ({self.get_validation_status_display()})"


# ── Lock / amendment history ───────────────────────────────────────────────


class SittingLockEvent(models.Model):
    """Append-only history of lock and post-lock amendment events.

    Every entry — auto T-30 lock, Chair non-critical amendment, NBEC
    resolution amendment — is recorded here. ``before_snapshot`` and
    ``after_snapshot`` capture the affected fields for full traceability.
    """

    class Kind(models.TextChoices):
        AUTO_LOCK = "auto_lock", "Auto T-30 Lock"
        CHAIR_AMEND = "chair_amend", "Chair Non-Critical Amendment"
        RESOLUTION_AMEND = "resolution_amend", "NBEC Resolution Amendment"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting = models.ForeignKey(
        Sitting, on_delete=models.CASCADE, related_name="lock_events",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    actor_id = models.UUIDField(
        null=True, blank=True,
        help_text="Keycloak sub of the actor; null for system-fired auto-lock.",
    )
    justification = models.TextField(blank=True)
    resolution_ref = models.CharField(
        max_length=100, blank=True,
        help_text="NBEC resolution / minutes reference for resolution_amend.",
    )
    affected_fields = models.JSONField(default=list, blank=True)
    before_snapshot = models.JSONField(default=dict, blank=True)
    after_snapshot = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sitting_sittinglockevent"
        ordering = ["-occurred_at"]

    def __str__(self):
        return f"{self.sitting_id} {self.kind} @ {self.occurred_at:%Y-%m-%d %H:%M}"


class SittingLock(models.Model):
    """Convenience pointer to the T-30 auto-lock for a sitting.

    Retained from the initial schema. The authoritative history is in
    :class:`SittingLockEvent`; this row is kept for fast lookups
    ("when did this sitting lock?") without a join.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting = models.OneToOneField(
        Sitting, on_delete=models.CASCADE, related_name="lock_record",
    )
    locked_at = models.DateTimeField(auto_now_add=True)
    locked_by = models.CharField(max_length=20, default="system")
    override = models.BooleanField(default=False)
    override_reason = models.TextField(blank=True)

    class Meta:
        db_table = "sitting_sittinglock"


# ── Legacy alias ──────────────────────────────────────────────────────────-


class Blueprint(models.Model):
    """Legacy per-sitting blueprint blob (deprecated by BlueprintVersion).

    Retained so the original migration still applies cleanly; not used by
    Phase 4 services. New work should reference
    :class:`BlueprintVersion` via ``SubjectPaper.blueprint_version``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sitting = models.OneToOneField(
        Sitting, on_delete=models.CASCADE, related_name="blueprint",
    )
    content = models.JSONField(default=dict)
    validated = models.BooleanField(default=False)
    validated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sitting_blueprint"
