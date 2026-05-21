"""Serializers for itembank app.

This module provides a serializer used to validate and (de)serialize
an item draft payload. Fields are minimal and intended for incoming
rich-text question content and associated metadata used by the item
bank service.

Note: This serializer is non-model-backed and is used for request/response
validation only.
"""

import json

from rest_framework import serializers
from .models import (
    Item,
    ItemVersion,
    ItemComment,
    VaultExportRequest,
    PanelVote,
    SavedSearch,
)

from .services import create_or_update_item_draft


class ItemDraftSerializer(serializers.Serializer):
    """Serializer for validating an item draft payload.

    The serializer expects the main rich-text content of the question and an
    optional set of metadata fields used for categorization, filtering and
    scoring. This is a plain Serializer (not a ModelSerializer) so it is
    suitable for transient draft payloads or API requests.

    Fields
    - content: Required rich-text question content (HTML/JSON/Rich text).
    - subject/topic: Optional strings used to categorize the item.
    - cognitive_level/difficulty: Optional strings describing taxonomy.
    - time: Optional integer representing suggested time in seconds.
    - marks: Optional Decimal representing marks/weight for the item.
    - source/blueprint_ref: Optional reference strings.
    - asset_refs: Optional list of string IDs (e.g. UUIDs) referencing
      attached assets such as images, files or other media.
    """

    ITEM_TYPES = ["mcq", "essay", "short_answer", "practical", "multiple_response"]

    item_type = serializers.ChoiceField(choices=ITEM_TYPES, required=False)

    # The actual rich-text question content. Expected to contain markup,
    # embedded equations and other structured content produced by the
    # front-end rich text editor.
    content = serializers.CharField(
        help_text="Rich text content including equations, tables, lists",
        required=True,
    )

    # Metadata fields for categorization and filtering
    subject = serializers.CharField(max_length=255, required=False, allow_blank=True)
    topic = serializers.CharField(max_length=255, required=False, allow_blank=True)
    cognitive_level = serializers.CharField(
        max_length=50, required=False, allow_blank=True
    )
    difficulty = serializers.CharField(max_length=50, required=False, allow_blank=True)

    # Estimated time (in seconds) to answer the item. Nullable/optional.
    time = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    # Marks/weight allocated to the item. Uses Decimal to preserve precision.
    marks = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True, min_value=0.01
    )

    # Optional provenance and blueprint references
    source = serializers.CharField(max_length=255, required=False, allow_blank=True)
    blueprint_ref = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )

    # Attached assets are expressed as a list of string identifiers (for
    # example UUIDs). The front-end stores references to uploaded assets and
    # the item draft keeps an array of those references.
    # Do not provide a default here so omission of the field can be detected
    # by the service layer (preserve existing refs on autosave when omitted).
    asset_refs = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True
    )

    def validate(self, attrs):
        """MCQ items must have exactly one correct answer."""
        if attrs.get("item_type") == "mcq" and "content" in attrs:
            try:
                content_dict = json.loads(attrs["content"])
                options = content_dict.get("options", [])
                correct_count = sum(
                    1
                    for option in options
                    if isinstance(option, dict)
                    and (option.get("is_correct") or option.get("correct"))
                )
                if correct_count != 1:
                    raise serializers.ValidationError(
                        {"content": "MCQ must have exactly one correct answer."}
                    )
            except json.JSONDecodeError:
                # Unparseable JSON will be rejected later by the workflow guard.
                pass
        return attrs

    def create(self, validated_data):
        """Create a new draft item through the service layer."""
        request = self.context.get("request")
        return create_or_update_item_draft(validated_data, request.auth)

    def update(self, instance, validated_data):
        """Auto-save an existing draft item through the service layer."""
        request = self.context.get("request")
        return create_or_update_item_draft(
            validated_data, request.auth, item_id=instance.id
        )


class AssetUploadSerializer(serializers.Serializer):
    """Handles multipart file uploads for inline item media."""

    file = serializers.FileField(
        help_text="Inline media (image, PDF, audio). Max size 25MB."
    )

    def validate_file(self, value):
        """Validate uploaded file size.

        Ensures the uploaded file does not exceed the configured maximum
        size (25 MB). Raises a ValidationError when the file is too large.

        Args:
            value: Uploaded file object with a .size attribute (in bytes).

        Returns:
            The original file value when validation passes.
        """
        max_size = 25 * 1024 * 1024  # 25 MB in bytes
        if value.size > max_size:
            raise serializers.ValidationError("File size must not exceed 25 MB.")
        return value

    def create(self, validated_data):
        """Return validated upload payload for compatibility with serializer APIs."""
        return validated_data

    def update(self, instance, validated_data):
        """Update method stub - not used as this is a non-model serializer."""
        raise NotImplementedError(
            "update() is not implemented for AssetUploadSerializer"
        )


class ItemVersionSerializer(serializers.ModelSerializer):
    """Serializes a specific forensic snapshot of an item.

    Used for version history and side-by-side diffing.
    """

    class Meta:
        """Meta configuration for ItemVersionSerializer."""

        model = ItemVersion
        fields = [
            "id",
            "version_no",
            "content",
            "metadata_snapshot",
            "asset_refs",
            "saved_by",
            "saved_at",
        ]


class ItemCommentSerializer(serializers.ModelSerializer):
    """Handles creating and listing annotations on specific parts of an item."""

    class Meta:
        model = ItemComment
        fields = [
            "id",
            "item_version_id",
            "anchor_path",
            "body",
            "status",
            "created_by",
        ]
        read_only_fields = ["id", "status", "created_by"]


class SuggestionDecisionSerializer(serializers.Serializer):
    """Handles the Accept/Decline payload.

    This serializer validates suggestion decision payloads containing a choice
    (accept or decline) and an optional rationale. Declined suggestions require
    a documented rationale.
    """

    DECISION_CHOICES = [("accept", "Accept"), ("decline", "Decline")]

    decision = serializers.ChoiceField(choices=DECISION_CHOICES)
    rationale = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        # Declined suggestions must be preserved alongside a documented rationale.
        rationale = attrs.get("rationale")
        if attrs["decision"] == "decline" and (not rationale or not rationale.strip()):
            raise serializers.ValidationError(
                {"rationale": "A rationale is required when declining a suggestion."}
            )
        return attrs

    def create(self, validated_data):
        """Create is not supported for this transient payload serializer."""
        raise NotImplementedError(
            "create() is not implemented for SuggestionDecisionSerializer"
        )

    def update(self, instance, validated_data):
        """Update is not supported for this transient payload serializer."""
        raise NotImplementedError(
            "update() is not implemented for SuggestionDecisionSerializer"
        )


class PanelVoteSerializer(serializers.ModelSerializer):
    """Serializes panel votes on items during the review workflow.

    Used to capture panellist decisions, including vote choice and supporting
    justification. Timestamps are automatically managed by the model.
    """

    class Meta:
        """Meta configuration for PanelVoteSerializer."""

        model = PanelVote
        fields = ["id", "item_id", "panellist_id", "vote", "justification", "voted_at"]
        read_only_fields = ["id", "item_id", "panellist_id", "voted_at"]


class VaultExportSerializer(serializers.ModelSerializer):
    """Serializes vault export requests for archival and retrieval workflows.

    Used to manage the lifecycle of data export requests, including scope,
    purpose, and approval tracking. Timestamps and status are automatically
    managed by the model.
    """

    class Meta:
        """Meta configuration for VaultExportSerializer."""

        model = VaultExportRequest
        fields = [
            "id",
            "scope",
            "purpose",
            "requester_id",
            "cosigner_id",
            "status",
            "expires_at",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "requester_id",
            "status",
            "expires_at",
            "created_at",
            "cosigner_id",
        ]


class ItemListSerializer(serializers.ModelSerializer):
    """Summary serializer for item search results.

    SRS-NBE-F02-10 result list shows item ID, status, last-modified date,
    usage count, and quality indicators. ``usage_count``,
    ``latest_facility_index``, ``latest_discrimination_index`` and
    ``last_modified`` are computed from related rows.
    """

    last_modified = serializers.DateTimeField(source="updated_at", read_only=True)
    usage_count = serializers.SerializerMethodField()
    latest_facility_index = serializers.SerializerMethodField()
    latest_discrimination_index = serializers.SerializerMethodField()
    author_id = serializers.UUIDField(source="author_id_id", read_only=True)

    class Meta:
        model = Item
        fields = [
            "id",
            "item_type",
            "status",
            "subject",
            "topic",
            "difficulty",
            "cognitive_level",
            "marks",
            "time",
            "blueprint_ref",
            "quality_flagged",
            "current_version_id",
            "author_id",
            "last_modified",
            "usage_count",
            "latest_facility_index",
            "latest_discrimination_index",
        ]
        read_only_fields = fields

    def _latest_usage(self, obj):
        if "_latest_usage_cache" not in getattr(obj, "__dict__", {}):
            obj.__dict__["_latest_usage_cache"] = (
                obj.usage_history.order_by("-recorded_at").first()
            )
        return obj.__dict__["_latest_usage_cache"]

    def get_usage_count(self, obj) -> int:
        return obj.usage_history.count()

    def get_latest_facility_index(self, obj):
        usage = self._latest_usage(obj)
        return str(usage.facility_index) if usage and usage.facility_index is not None else None

    def get_latest_discrimination_index(self, obj):
        usage = self._latest_usage(obj)
        return (
            str(usage.discrimination_index)
            if usage and usage.discrimination_index is not None
            else None
        )


class SavedSearchSerializer(serializers.ModelSerializer):
    """CRUD serializer for ``SavedSearch`` records (NBE-F02-10)."""

    class Meta:
        model = SavedSearch
        fields = [
            "id",
            "name",
            "query",
            "shared_with_secretariat",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_query(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                "query must be a JSON object mapping filter keys to values."
            )
        return value


class PaperSectionSerializer(serializers.Serializer):
    """Inline serializer for a single paper section (SRS-NBE-F02-08)."""

    name = serializers.CharField(max_length=100)
    item_ids = serializers.ListField(
        child=serializers.UUIDField(), allow_empty=False
    )
    marks = serializers.DecimalField(
        max_digits=6, decimal_places=2, required=False
    )
    time = serializers.IntegerField(required=False, min_value=1)


class ManualPaperSerializer(serializers.Serializer):
    """Validates payload for manual paper construction (NBE-F02-08)."""

    sitting_ref = serializers.CharField(max_length=255)
    subject = serializers.CharField(max_length=255)
    mode = serializers.CharField(max_length=50)
    total_marks = serializers.DecimalField(max_digits=6, decimal_places=2)
    time_limit = serializers.IntegerField(min_value=1)
    item_ids = serializers.ListField(
        child=serializers.UUIDField(), allow_empty=False
    )
    sections = serializers.ListField(
        child=PaperSectionSerializer(), required=False, allow_empty=True
    )
    blueprint_ref = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )

    def create(self, validated_data):
        return validated_data

    def update(self, instance, validated_data):
        raise NotImplementedError(
            "update() is not implemented for ManualPaperSerializer"
        )


class RuleBasedPaperSerializer(serializers.Serializer):
    sitting_ref = serializers.CharField()
    subject = serializers.CharField()
    mode = serializers.CharField()
    total_marks = serializers.DecimalField(max_digits=6, decimal_places=2)
    time_limit = serializers.IntegerField()
    difficulty_distribution = serializers.DictField(child=serializers.IntegerField())
    topic_coverage = serializers.DictField(child=serializers.IntegerField())
    blueprint_ref = serializers.CharField(required=False, allow_blank=True)
    variants_count = serializers.IntegerField(required=False, min_value=1, default=1)

    def validate(self, attrs):
        # Validate that all percentages sum to 100
        diff_dist = attrs.get("difficulty_distribution", {})
        if sum(diff_dist.values()) != 100:
            raise serializers.ValidationError(
                {"difficulty_distribution": "Percentages must sum to 100."}
            )
        topic_dist = attrs.get("topic_coverage", {})
        if sum(topic_dist.values()) != 100:
            raise serializers.ValidationError(
                {"topic_coverage": "Percentages must sum to 100."}
            )
        return attrs

    def create(self, validated_data):
        return validated_data

    def update(self, instance, validated_data):
        instance.update(validated_data)
        return instance
