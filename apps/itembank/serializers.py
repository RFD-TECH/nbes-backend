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
        """Return the instance unchanged for compatibility with serializer APIs."""
        return instance
