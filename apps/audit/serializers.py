"""apps/audit/serializers.py — Output shapes for the audit search API.

Read-only. Audit rows are never created or edited via the API; ``AuditEvent``
rows come from ``AuditEvent.record(...)`` only. ``DailyHashAnchor`` rows
come from the daily Celery task.
"""
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
    """Hash-chain proof for one UTC day.

    ``head_hash`` is the SHA-256 chain hash of the day's last event;
    ``anchor_ref`` (when present) is the receipt id System 22 returned after
    notarising the anchor. Independent verification: re-derive the chain
    for the day from the AuditEvent rows and confirm the final hash matches.
    """
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
