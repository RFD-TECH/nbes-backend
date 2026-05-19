"""apps/audit/models.py — Append-only audit trail with SHA-256 chain hash."""
import uuid
import hashlib
import json
from django.db import models
from django.utils import timezone


class AuditEvent(models.Model):
    """
    Append-only audit event store.
    DB trigger (added via migration RunSQL) prevents UPDATE/DELETE.
    chain_hash: SHA-256(previous_chain_hash + this_event_payload) — tamper evidence.
    All records forwarded to System 22 via Kafka (OutboxEvent).
    """
    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(unique=True, default=uuid.uuid4)
    actor_id = models.UUIDField(null=True, blank=True)   # Keycloak sub; NULL for system events
    action = models.CharField(max_length=100)             # e.g. ITEM_APPROVED, VAULT_READ
    entity_type = models.CharField(max_length=100, blank=True, default="")
    entity_id = models.UUIDField(null=True, blank=True)
    old_state = models.JSONField(null=True, blank=True)
    new_state = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    request_id = models.UUIDField(null=True, blank=True)
    source_system = models.CharField(max_length=20, default="nbes")
    chain_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "audit_auditevent"
        ordering = ["id"]
        verbose_name = "Audit Event"

    def __str__(self):
        return f"{self.action} on {self.entity_type}:{self.entity_id}"

    @classmethod
    def record(cls, *, action: str, **kwargs) -> "AuditEvent":
        """
        Create an audit event with chained SHA-256 hash.
        Call from services and FSM transition methods.

        Example:
            AuditEvent.record(
                actor_id=request.auth["sub"],
                action="ITEM_APPROVED",
                entity_type="item",
                entity_id=item.id,
                new_state={"status": item.status},
                request_id=getattr(request, "request_id", None),
            )
        """
        last = cls.objects.order_by("-id").values("chain_hash").first()
        previous_hash = last["chain_hash"] if last else "0" * 64

        event_id = kwargs.pop("event_id", uuid.uuid4())
        payload = json.dumps({
            "event_id": str(event_id),
            "actor_id": str(kwargs.get("actor_id", "")),
            "action": action,
            "entity_type": kwargs.get("entity_type", ""),
            "entity_id": str(kwargs.get("entity_id", "")),
            "new_state": kwargs.get("new_state", {}),
            "created_at": timezone.now().isoformat(),
        }, sort_keys=True)

        chain_hash = hashlib.sha256(
            f"{previous_hash}{payload}".encode()
        ).hexdigest()

        event = cls.objects.create(
            event_id=event_id,
            action=action,
            chain_hash=chain_hash,
            **kwargs,
        )

        # Forward to System 22 via outbox
        from shared.events import publish
        publish("AuditEventRecorded", {
            "event_id": str(event.event_id),
            "chain_hash": chain_hash,
        }, topic="nbes.audit")

        return event


class OutboxEvent(models.Model):
    """
    Transactional outbox for Kafka event delivery.
    Written in same DB transaction as the domain state change.
    Polled every 5 seconds by apps.audit.tasks.poll_outbox Celery task.
    """
    id = models.BigAutoField(primary_key=True)
    correlation_id = models.UUIDField(unique=True, default=uuid.uuid4)
    topic = models.CharField(max_length=100)
    event_name = models.CharField(max_length=100)
    payload = models.JSONField()
    published = models.BooleanField(default=False, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "audit_outboxevent"
        indexes = [
            models.Index(fields=["published", "created_at"]),
        ]

    def __str__(self):
        return f"{self.event_name} → {self.topic} ({'sent' if self.published else 'pending'})"
