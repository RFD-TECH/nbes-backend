"""apps/audit/serializers.py — Read-only audit API serializers."""
from rest_framework import serializers

from .models import AuditEvent, DailyHashAnchor


class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = [
            "event_id",
            "actor_id",
            "action",
            "entity_type",
            "entity_id",
            "old_state",
            "new_state",
            "ip_address",
            "user_agent",
            "request_id",
            "source_system",
            "chain_hash",
            "created_at",
        ]
        read_only_fields = fields


class DailyHashAnchorSerializer(serializers.ModelSerializer):
    verifiable = serializers.SerializerMethodField()

    class Meta:
        model = DailyHashAnchor
        fields = [
            "date",
            "head_event_id",
            "head_hash",
            "event_count",
            "exported_to_s22_at",
            "anchor_ref",
            "verifiable",
            "created_at",
        ]
        read_only_fields = fields

    def get_verifiable(self, obj) -> bool:
        return bool(obj.exported_to_s22_at and obj.anchor_ref)
