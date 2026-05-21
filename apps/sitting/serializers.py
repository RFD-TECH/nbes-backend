"""apps/sitting/serializers.py — Phase 4 Sitting input / output serializers."""
from rest_framework import serializers

from .models import (
    BlueprintVersion,
    Sitting,
    SittingLockEvent,
    SubjectPaper,
    Variant,
)


# ── Sitting ────────────────────────────────────────────────────────────────


class SittingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sitting
        fields = [
            "ref",
            "sitting_date",
            "sitting_end_date",
            "status",
            "pass_mark",
            "pass_band_min",
            "pass_band_max",
            "pass_rule",
            "compensated_min_per_paper",
            "compensated_aggregate_floor",
            "normalisation_method",
            "centres",
            "created_by_id",
            "locked_at",
            "approved_at",
            "approved_via_meeting_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "status", "locked_at", "approved_at", "approved_via_meeting_id",
            "created_by_id", "created_at", "updated_at",
        ]


class SittingCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sitting
        fields = [
            "ref",
            "sitting_date",
            "sitting_end_date",
            "pass_mark",
            "pass_band_min",
            "pass_band_max",
            "pass_rule",
            "compensated_min_per_paper",
            "compensated_aggregate_floor",
            "normalisation_method",
            "centres",
        ]

    def validate(self, attrs):
        start = attrs.get("sitting_date")
        end = attrs.get("sitting_end_date")
        if start and end and end < start:
            raise serializers.ValidationError(
                "sitting_end_date must be on or after sitting_date."
            )
        return attrs


class SittingUpdateSerializer(serializers.ModelSerializer):
    """Used for pre-lock draft edits (PATCH /sittings/{ref})."""

    class Meta:
        model = Sitting
        fields = [
            "sitting_date",
            "sitting_end_date",
            "pass_mark",
            "pass_band_min",
            "pass_band_max",
            "pass_rule",
            "compensated_min_per_paper",
            "compensated_aggregate_floor",
            "normalisation_method",
            "centres",
        ]


class SittingApproveSerializer(serializers.Serializer):
    meeting_id = serializers.UUIDField(
        help_text="Phase 2 Meeting that approved this configuration."
    )


class SittingAmendNonCriticalSerializer(serializers.Serializer):
    changes = serializers.DictField(
        child=serializers.JSONField(),
        help_text="Map of {field: new_value}. Critical fields are rejected.",
    )
    justification = serializers.CharField(
        min_length=10,
        help_text="Audit-logged justification (min 10 chars).",
    )


class SittingAmendCriticalSerializer(serializers.Serializer):
    changes = serializers.DictField(child=serializers.JSONField())
    resolution_ref = serializers.CharField(
        max_length=100,
        help_text="NBEC resolution / Minutes reference authorising the change.",
    )
    justification = serializers.CharField(min_length=30)


# ── SubjectPaper ───────────────────────────────────────────────────────────


class SubjectPaperSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubjectPaper
        fields = [
            "id", "sitting", "subject_code", "subject_name", "mode",
            "total_marks", "pass_mark", "duration_minutes", "sections",
            "normalisation_method", "normalisation_params",
            "blueprint_version",
        ]
        read_only_fields = ["id", "sitting"]


class SubjectPaperUpsertSerializer(serializers.ModelSerializer):
    """Used for POST /sittings/{ref}/papers (add or update by subject_code)."""

    class Meta:
        model = SubjectPaper
        fields = [
            "subject_code", "subject_name", "mode", "total_marks", "pass_mark",
            "duration_minutes", "sections", "normalisation_method",
            "normalisation_params", "blueprint_version",
        ]

    def validate_sections(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("sections must be a list.")
        for entry in value:
            if not isinstance(entry, dict) or "marks" not in entry:
                raise serializers.ValidationError(
                    "Each section must be an object with a 'marks' field."
                )
        return value


# ── BlueprintVersion ───────────────────────────────────────────────────────


class BlueprintVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlueprintVersion
        fields = [
            "id", "subject_code", "version_no",
            "topic_coverage", "cognitive_distribution",
            "difficulty_distribution", "sections", "total_marks",
            "tolerance", "description",
            "published_at", "published_by_id", "created_at",
        ]
        read_only_fields = [
            "id", "version_no", "published_at", "published_by_id", "created_at",
        ]


class BlueprintVersionPublishSerializer(serializers.Serializer):
    topic_coverage = serializers.DictField(child=serializers.FloatField())
    cognitive_distribution = serializers.DictField(child=serializers.FloatField())
    difficulty_distribution = serializers.DictField(child=serializers.FloatField())
    sections = serializers.ListField(
        child=serializers.DictField(), required=False, default=list,
    )
    total_marks = serializers.IntegerField(min_value=1, default=100)
    tolerance = serializers.DecimalField(
        max_digits=4, decimal_places=3, required=False, default="0.050",
    )
    description = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        for field in ("topic_coverage", "cognitive_distribution", "difficulty_distribution"):
            values = attrs.get(field, {})
            total = sum(float(v) for v in values.values())
            if values and abs(total - 1.0) > 0.01:
                raise serializers.ValidationError(
                    {field: f"Weights must sum to 1.0 (got {total:.3f})."}
                )
        return attrs


# ── Variant ────────────────────────────────────────────────────────────────


class VariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Variant
        fields = [
            "id", "paper", "variant_no", "seed", "items", "item_order",
            "coverage_report", "failed_constraints", "validation_status",
            "generated_by_id", "generated_at",
        ]
        read_only_fields = fields  # variants are read-only over the wire


class VariantGenerateSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=1, max_value=20, default=4)
    seeds = serializers.ListField(
        child=serializers.IntegerField(),
        required=False, allow_empty=False,
        help_text="Optional explicit seeds (length must equal count).",
    )

    def validate(self, attrs):
        # Enforce the contract at the API boundary so the caller gets a
        # 400 with a clear field-level error instead of a service-layer
        # ValueError bubbling up as a 400 with a generic message.
        seeds = attrs.get("seeds")
        if seeds is not None and len(seeds) != attrs["count"]:
            raise serializers.ValidationError(
                {"seeds": (
                    f"Length must equal count ({attrs['count']}); got {len(seeds)}."
                )}
            )
        return attrs


# ── Lock events ────────────────────────────────────────────────────────────


class SittingLockEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = SittingLockEvent
        fields = [
            "id", "sitting", "kind", "actor_id", "justification",
            "resolution_ref", "affected_fields", "before_snapshot",
            "after_snapshot", "occurred_at",
        ]
        read_only_fields = fields


# ── Sitting snapshot ──────────────────────────────────────────────────────-


class SittingSnapshotSerializer(serializers.Serializer):
    """Pass-through serializer for the frozen read-only snapshot dict.

    ``services.get_sitting_snapshot`` already returns JSON-ready primitives,
    so this is mostly a typed placeholder for OpenAPI generation.
    """

    ref = serializers.CharField()
    status = serializers.CharField()
    sitting_date = serializers.CharField()
    sitting_end_date = serializers.CharField()
    pass_rule = serializers.CharField()
    pass_mark = serializers.CharField()
    pass_band = serializers.DictField()
    compensation = serializers.DictField()
    normalisation_method = serializers.CharField()
    centres = serializers.ListField(child=serializers.CharField())
    locked_at = serializers.CharField(allow_null=True)
    approved_at = serializers.CharField(allow_null=True)
    approved_via_meeting_id = serializers.CharField(allow_null=True)
    papers = serializers.ListField(child=serializers.DictField())
