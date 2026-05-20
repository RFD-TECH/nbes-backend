"""apps/committee/serializers.py — NBEC Committee input/output serializers."""
from rest_framework import serializers

from .models import (
    ActionItem,
    Agenda,
    ConflictDeclaration,
    Meeting,
    Minutes,
    MinutesAddendum,
    NBECMember,
)


# ── NBECMember ────────────────────────────────────────────────────────────────

class NBECMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = NBECMember
        fields = [
            "id", "keycloak_sub", "full_name", "title", "post_nominals",
            "email", "role", "status", "instrument_ref", "appointment_date",
            "tenure_end_date", "photo_ref", "is_active", "is_voting_member",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]


class NBECMemberCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = NBECMember
        fields = [
            "keycloak_sub", "full_name", "title", "post_nominals",
            "email", "role", "instrument_ref", "appointment_date",
            "tenure_end_date", "photo_ref", "is_voting_member",
        ]


class NBECMemberAmendSerializer(serializers.ModelSerializer):
    class Meta:
        model = NBECMember
        fields = [
            "full_name", "title", "post_nominals", "email", "role",
            "instrument_ref", "appointment_date", "tenure_end_date",
            "photo_ref", "is_voting_member",
        ]


# ── ConflictDeclaration ───────────────────────────────────────────────────────

class ConflictDeclarationSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source="member.full_name", read_only=True)

    class Meta:
        model = ConflictDeclaration
        fields = [
            "id", "member", "member_name", "subject_type", "subject_description",
            "nature", "affected_entity_type", "affected_entity_id",
            "status", "effective_from", "review_date",
            "declared_at", "reviewed_at", "reviewed_by_id",
        ]
        read_only_fields = ["id", "status", "declared_at", "reviewed_at", "reviewed_by_id"]


class COIDeclareSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConflictDeclaration
        fields = [
            "member", "subject_type", "subject_description", "nature",
            "affected_entity_type", "affected_entity_id", "effective_from",
        ]


class COIReviewSerializer(serializers.Serializer):
    approved = serializers.BooleanField()
    review_date = serializers.DateField(required=False, allow_null=True)


# ── Agenda ────────────────────────────────────────────────────────────────────

class AgendaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Agenda
        fields = [
            "id", "meeting", "version", "items", "document_ref",
            "published_at", "created_by_id", "created_at",
        ]
        read_only_fields = ["id", "version", "published_at", "created_at"]


class AgendaPublishSerializer(serializers.Serializer):
    items = serializers.ListField(
        child=serializers.DictField(),
        help_text="[{order, title, description, presenter_id, duration_minutes}]",
    )
    document_ref = serializers.CharField(required=False, allow_blank=True, default="")


# ── Meeting ───────────────────────────────────────────────────────────────────

class MeetingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Meeting
        fields = [
            "id", "reference", "meeting_type", "scheduled_date", "venue",
            "status", "quorum_required", "attendees", "chair_id",
            "secretariat_id", "convened_at", "adjourned_at", "created_at",
        ]
        read_only_fields = ["id", "status", "convened_at", "adjourned_at", "created_at"]


class MeetingCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Meeting
        fields = [
            "reference", "meeting_type", "scheduled_date", "venue",
            "quorum_required", "chair_id", "secretariat_id",
        ]


class AttendanceSerializer(serializers.Serializer):
    attendee_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        help_text="List of keycloak_sub UUIDs of attending members.",
    )


# ── Minutes ───────────────────────────────────────────────────────────────────

class MinutesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Minutes
        fields = [
            "id", "meeting", "content", "approved", "approved_by_id",
            "approved_at", "document_ref", "immutable_at", "signature_ref",
            "archive_ref", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "approved", "approved_by_id", "approved_at",
            "immutable_at", "archive_ref", "created_at", "updated_at",
        ]


class MinutesSignSerializer(serializers.Serializer):
    signature_ref = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="Digital signature artefact reference (MinIO key or HSM ref).",
    )


class MinutesAddendumSerializer(serializers.ModelSerializer):
    class Meta:
        model = MinutesAddendum
        fields = ["id", "minutes", "content", "issued_by_id", "issued_at",
                  "document_ref", "created_at"]
        read_only_fields = ["id", "issued_by_id", "issued_at", "created_at"]


class AddendumCreateSerializer(serializers.Serializer):
    content = serializers.CharField(min_length=10)
    document_ref = serializers.CharField(required=False, allow_blank=True, default="")


# ── ActionItem ────────────────────────────────────────────────────────────────

class ActionItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActionItem
        fields = [
            "id", "meeting", "minutes", "description", "assigned_to_id",
            "due_date", "status", "completed_at", "last_escalated_at", "created_at",
        ]
        read_only_fields = ["id", "status", "completed_at", "last_escalated_at", "created_at"]


# ── COI Policy (internal) ──────────────────────────────────────────────────────

class COIPolicyResponseSerializer(serializers.Serializer):
    has_active_conflict = serializers.BooleanField()
    member_id = serializers.UUIDField()
    entity_type = serializers.CharField()
    entity_id = serializers.UUIDField(allow_null=True)
    conflict_ids = serializers.ListField(child=serializers.UUIDField())
