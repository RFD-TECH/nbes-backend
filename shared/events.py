"""
shared/events.py — Domain Event Publishing (Transactional Outbox)
=================================================================

publish() writes a domain event to the OutboxEvent table within the
current DB transaction. A Celery Beat task (apps.audit.tasks.poll_outbox)
polls every 5 seconds and delivers unpublished events to Kafka.

This guarantees at-least-once delivery — Kafka consumers must be
idempotent and check correlation_id before processing.

In dev (KAFKA_ENABLED=False), events are stored in the DB only.
In production (KAFKA_ENABLED=True), the outbox task publishes to Kafka.

Reference: NBES System Architecture §6.1 — Transactional outbox pattern
"""

import uuid
import json
from django.utils import timezone


def publish(event_name: str, payload: dict, *, topic: str | None = None) -> None:
    """
    Write a domain event to the OutboxEvent table.

    Args:
        event_name: e.g. "ItemApproved", "CandidateRegistered"
        payload:    Event data dict — must be JSON-serialisable.
        topic:      Kafka topic override. Defaults to nbes.<app_label>
                    derived from the first key in the payload.

    The OutboxEvent is written in the same DB transaction as the caller.
    Do NOT call this outside a transaction — wrap in atomic() if needed.
    """
    from apps.audit.models import OutboxEvent

    # Derive topic from event name prefix if not provided
    if topic is None:
        topic = _infer_topic(event_name)

    OutboxEvent.objects.create(
        correlation_id=uuid.uuid4(),
        topic=topic,
        event_name=event_name,
        payload=payload,
        published=False,
        created_at=timezone.now(),
    )


def _infer_topic(event_name: str) -> str:
    """
    Map event name to Kafka topic.
    e.g. "ItemApproved" → "nbes.itembank"
         "CandidateRegistered" → "nbes.registration"
    """
    prefix_map = {
        "Item": "nbes.itembank",
        "Paper": "nbes.itembank",
        "Vault": "nbes.itembank",
        "Member": "nbes.committee",
        "Meeting": "nbes.committee",
        "Minutes": "nbes.committee",
        "Conflict": "nbes.committee",
        "Action": "nbes.committee",
        "Sitting": "nbes.sitting",
        "Manifest": "nbes.sitting",
        "Candidate": "nbes.registration",
        "Eligibility": "nbes.registration",
        "Registration": "nbes.registration",
        "Index": "nbes.registration",
        "Slip": "nbes.registration",
        "Script": "nbes.marking",
        "AI": "nbes.marking",
        "Borderline": "nbes.marking",
        "Moderation": "nbes.marking",
        "Reconciliation": "nbes.marking",
        "Hash": "nbes.marking",
        "FinalMark": "nbes.marking",
        "Results": "nbes.results",
        "Board": "nbes.results",
        "Publication": "nbes.results",
        "Remark": "nbes.results",
        "Attempt": "nbes.resit",
        "Resit": "nbes.resit",
        "Exception": "nbes.resit",
        "Cert": "nbes.cert_trigger",
        "Pass": "nbes.cert_trigger",
        "SLA": "nbes.sla",
        "Audit": "nbes.audit",
    }
    for prefix, topic in prefix_map.items():
        if event_name.startswith(prefix):
            return topic
    return "nbes.general"
