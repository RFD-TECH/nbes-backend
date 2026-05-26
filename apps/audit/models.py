"""Append-only audit trail with SHA-256 chain hash."""

import logging
import uuid
import hashlib
import json
from django.db import models, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Actions that mutate existing state — callers must supply old_state.
_STATE_CHANGE_ACTIONS = frozenset({
    "USER_UPDATED", "USER_DEACTIVATED", "ROLE_ASSIGNED", "ROLE_REVOKED",
    "ROLE_PERMISSIONS_UPDATED", "ROLE_ASSIGNMENT_APPROVED",
    "ROLE_ASSIGNMENT_REJECTED", "DASHBOARD_PANEL_UPDATED",
    "BULK_IMPORT", "PROFILE_UPDATED",
})


class AuditEvent(models.Model):
    """
    Append-only audit event store.
    DB trigger (added via migration RunSQL) prevents UPDATE/DELETE.
    chain_hash: SHA-256(previous_chain_hash + this_event_payload) — tamper evidence.
    All records forwarded to System 22 via Kafka (OutboxEvent).

    CRITICAL COMPLIANCE NOTICE: DO NOT DELETE or truncate this table.
    15-year statutory retention lifecycle plan (ADR-002) is required.
    """

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(db_index=True, default=uuid.uuid4)
    actor_id = models.UUIDField(
        null=True, blank=True
    )  # Keycloak sub; NULL for system events
    action = models.CharField(max_length=100)  # e.g. ITEM_APPROVED, VAULT_READ
    entity_type = models.CharField(max_length=100, blank=True, default="")
    # Accepts any entity primary key — UUID (most apps), short-string PK
    # (Sitting.ref BAR-YYYY-MM, candidate index BAR-YYYY-CCCCC), or null
    # for system-wide events. UUIDs continue to serialize losslessly via str().
    entity_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
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
        if action in _STATE_CHANGE_ACTIONS and kwargs.get("old_state") is None:
            from django.conf import settings
            msg = (
                f"audit.missing_old_state action={action} — callers must supply old_state "
                "for state-change actions (blueprint §1.2.7)"
            )
            if getattr(settings, "DEBUG", False):
                raise ValueError(msg)
            else:
                logger.warning(msg)
        with transaction.atomic():
            return cls._record_atomic(action=action, **kwargs)

    @classmethod
    def _record_atomic(cls, *, action: str, **kwargs) -> "AuditEvent":
        last = (
            cls.objects.select_for_update().order_by("-id").values("chain_hash").first()
        )
        previous_hash = last["chain_hash"] if last else "0" * 64

        event_id = kwargs.pop("event_id", uuid.uuid4())
        created_at = kwargs.pop("created_at", timezone.now())
        payload = json.dumps(
            {
                "event_id": str(event_id),
                "actor_id": str(kwargs.get("actor_id", "")),
                "action": action,
                "entity_type": kwargs.get("entity_type", ""),
                "entity_id": str(kwargs.get("entity_id", "")),
                "old_state": kwargs.get("old_state", {}),
                "new_state": kwargs.get("new_state", {}),
                "created_at": created_at.isoformat(),
            },
            sort_keys=True,
        )

        chain_hash = hashlib.sha256(f"{previous_hash}{payload}".encode()).hexdigest()

        event = cls.objects.create(
            event_id=event_id,
            action=action,
            chain_hash=chain_hash,
            created_at=created_at,
            **kwargs,
        )

        # Forward to System 22 via outbox
        from shared.events import publish

        publish(
            "AuditEventRecorded",
            {
                "event_id": str(event.event_id),
                "chain_hash": chain_hash,
            },
            topic="nbes.audit",
        )

        return event


class OutboxEvent(models.Model):
    """
    Transactional outbox for Kafka event delivery.
    Written in same DB transaction as the domain state change.
    Polled every 5 seconds by apps.audit.tasks.poll_outbox Celery task.
    """

    id = models.BigAutoField(primary_key=True)
    correlation_id = models.UUIDField(unique=True, default=uuid.uuid4)
    request_id = models.UUIDField(null=True, blank=True, db_index=True)
    traceparent = models.CharField(max_length=255, null=True, blank=True)
    tracestate = models.TextField(null=True, blank=True)
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


class SecurityEvent(models.Model):
    """NBES-side security observation.

    Recorded whenever the service rejects a request for security reasons:
    bad signature, expired token, audience mismatch, AUTHZ_DENIED, edge
    throttle applied, IP block triggered.

    Categories are aligned with the System 22 SIEM schema (severity,
    category, indicators) per blueprint §1.2.6. Every row also lands in
    the outbox so System 22 sees the same view in near-real-time.

    Retention: last 90 days hot in this table (via ``cleanup_security_events``).
    Cold storage is System 22's responsibility.
    """

    CATEGORY_CHOICES = [
        ("auth_token_invalid", "auth_token_invalid"),
        ("auth_token_expired", "auth_token_expired"),
        ("auth_audience_mismatch", "auth_audience_mismatch"),
        ("authz_denied", "authz_denied"),
        ("step_up_denied", "step_up_denied"),
        ("throttle_applied", "throttle_applied"),
        ("ip_blocked", "ip_blocked"),
        ("anomaly_detected", "anomaly_detected"),
    ]

    SEVERITY_CHOICES = [
        ("info", "info"),
        ("warning", "warning"),
        ("high", "high"),
    ]

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(unique=True, default=uuid.uuid4)
    category = models.CharField(max_length=40, choices=CATEGORY_CHOICES, db_index=True)
    severity = models.CharField(
        max_length=10, choices=SEVERITY_CHOICES, default="warning"
    )
    indicators = models.JSONField(
        default=dict,
        help_text="Free-form details: path, method, role names, reason, etc.",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    actor_id = models.UUIDField(null=True, blank=True, db_index=True)
    request_id = models.UUIDField(null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "audit_securityevent"
        ordering = ["-occurred_at"]
        verbose_name = "Security Event"
        indexes = [
            models.Index(fields=["category", "occurred_at"]),
            models.Index(fields=["ip_address", "occurred_at"]),
        ]

    def __str__(self):
        return (
            f"{self.category}@{self.ip_address or '-'} {self.occurred_at.isoformat()}"
        )


class DailyHashAnchor(models.Model):
    """
    Per-day anchor of the audit chain. Built by ``daily_hash_anchor`` Celery
    Beat task at 01:00 UTC: takes the last ``AuditEvent.chain_hash`` for the
    UTC day, records the anchor row, and emits ``AuditChainAnchorReady`` to
    the outbox for System 22 to pick up and notarise.

    Once ``exported_to_s22_at`` is set and ``anchor_ref`` is populated by
    System 22's webhook, the day is independently verifiable using System
    22's published public key.

    Blueprint references: §1.2.7, §1.4 (`GET /api/v1/audit/chain/{date}`),
    §1.6 ("Daily hash anchor must be exported to System 22 by 01:00 UTC;
    failure pages the on-call.").
    """

    date = models.DateField(unique=True, db_index=True)
    head_event_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="event_id of the last AuditEvent on this UTC day.",
    )
    head_hash = models.CharField(
        max_length=64,
        help_text="SHA-256 chain hash of the day's last event. The anchor.",
    )
    event_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of AuditEvent rows produced on this UTC day.",
    )
    exported_to_s22_at = models.DateTimeField(null=True, blank=True)
    anchor_ref = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Receipt id returned by System 22 once notarisation completes.",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "audit_dailyhashanchor"
        ordering = ["-date"]
        verbose_name = "Daily Hash Anchor"

    def __str__(self):
        return f"{self.date.isoformat()} → {self.head_hash[:12]}…"
