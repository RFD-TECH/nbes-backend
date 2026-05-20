"""apps/audit/serializers.py — Audit event read serializers."""
from rest_framework import serializers

from .models import AuditEvent


class AuditEventSerializer(serializers.ModelSerializer):
    event_id = serializers.UUIDField()
    actor_id = serializers.UUIDField(allow_null=True)
    entity_id = serializers.UUIDField(allow_null=True)
    request_id = serializers.UUIDField(allow_null=True)

    class Meta:
        model = AuditEvent
        fields = [
            "id",
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
