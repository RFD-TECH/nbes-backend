"""apps/audit/tasks.py — Outbox poller + daily hash anchor.

Scheduled by ``config/celery.py``:

* ``poll_outbox``        — every 5s; relays unpublished OutboxEvent rows.
* ``daily_hash_anchor``  — 01:00 UTC; takes yesterday's last
  ``AuditEvent.chain_hash`` and writes a ``DailyHashAnchor`` row, then
  emits ``AuditChainAnchorReady`` to the outbox so System 22 can notarise
  it. Failure of this task is on-call paging by design — the blueprint's
  acceptance criterion F000-07 hinges on the export landing by 01:00 UTC.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone as py_timezone

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


GENESIS_HASH = "0" * 64


@shared_task(name="apps.audit.tasks.poll_outbox", queue="outbox")
def poll_outbox():
    """Relay unpublished OutboxEvent rows to the event bus.

    In dev (``KAFKA_ENABLED=False``) the relay marks rows as published
    in-place so the outbox doesn't grow forever. In prod the System 17
    HTTP path (Phase 2) takes over.
    """
    from django.conf import settings

    from apps.audit.models import OutboxEvent

    unpublished = OutboxEvent.objects.filter(published=False).order_by("created_at")[:100]
    sent = 0
    for event in unpublished:
        try:
            if settings.KAFKA_ENABLED:
                _publish_via_system_17(event)
            else:
                _publish_local_dev(event)
            event.published = True
            event.published_at = timezone.now()
            event.save(update_fields=["published", "published_at"])
            sent += 1
        except Exception as exc:
            logger.error("OutboxEvent %s failed: %s", event.id, exc)
    if sent:
        logger.info("outbox: relayed %d event(s)", sent)


def _publish_local_dev(event) -> None:
    """Dev fallback: no real bus, just log the payload."""
    logger.debug("dev outbox publish: %s → %s", event.topic, event.event_name)


def _publish_via_system_17(event) -> None:
    """Production publish via System 17.

    The blueprint § 1.2.8 mandates that *all* inter-system calls go through
    System 17. The outbox row's ``correlation_id`` doubles as the
    idempotency key so retries on the same OutboxEvent never produce
    duplicate downstream events.
    """
    from shared.integrations import call_system_17

    response = call_system_17(
        endpoint=f"/v1/events/{event.topic}",
        payload={
            "event_name": event.event_name,
            "topic": event.topic,
            "payload": event.payload,
            "correlation_id": str(event.correlation_id),
            "occurred_at": event.created_at.isoformat(),
        },
        idempotency_key=str(event.correlation_id),
        correlation_id=str(event.correlation_id),
    )
    if not response.ok:
        # Raise so poll_outbox keeps the row pending and retries on the
        # next tick. call_system_17 already retried inline with backoff.
        raise RuntimeError(
            f"System 17 publish failed: code={response.code} "
            f"status={response.status_code} retryable={response.retryable}"
        )


# ──────────────────────────────────────────────────────────────────────────
# Daily hash anchor — blueprint §1.2.7
# ──────────────────────────────────────────────────────────────────────────

@shared_task(name="apps.audit.tasks.daily_hash_anchor", queue="outbox", bind=True)
def daily_hash_anchor(self, target_date: str | None = None):
    """Anchor a UTC day's audit chain.

    Looks at the AuditEvent rows whose ``created_at`` falls on the target
    UTC day (defaults to *yesterday* relative to now). Writes a
    DailyHashAnchor row with the day's last chain_hash and emits
    ``AuditChainAnchorReady`` to the outbox.

    Idempotent: re-running the task for the same date updates the row
    in-place. If a day has zero events, we still record the row (with the
    genesis hash) so verification can be uniform.
    """
    from apps.audit.models import AuditEvent, DailyHashAnchor
    from shared.events import publish

    target = _resolve_target_date(target_date)
    day_start = datetime.combine(target, time.min, tzinfo=py_timezone.utc)
    day_end = day_start + timedelta(days=1)

    queryset = AuditEvent.objects.filter(
        created_at__gte=day_start,
        created_at__lt=day_end,
    ).order_by("id")

    head = queryset.order_by("-id").values("event_id", "chain_hash").first()
    count = queryset.count()

    head_hash = head["chain_hash"] if head else GENESIS_HASH
    head_event_id = head["event_id"] if head else None

    with transaction.atomic():
        anchor, _ = DailyHashAnchor.objects.update_or_create(
            date=target,
            defaults={
                "head_event_id": head_event_id,
                "head_hash": head_hash,
                "event_count": count,
            },
        )
        publish(
            "AuditChainAnchorReady",
            {
                "date": target.isoformat(),
                "head_event_id": str(head_event_id) if head_event_id else None,
                "head_hash": head_hash,
                "event_count": count,
                "source_system": "nbes",
            },
            topic="nbes.audit",
        )

    logger.info(
        "daily_hash_anchor: %s anchored head=%s count=%d",
        target.isoformat(),
        head_hash[:12],
        count,
    )
    return {"date": target.isoformat(), "head_hash": head_hash, "event_count": count}


# ──────────────────────────────────────────────────────────────────────────
# Security-Ops housekeeping
# ──────────────────────────────────────────────────────────────────────────

@shared_task(name="apps.audit.tasks.cleanup_security_events", queue="sla-monitor")
def cleanup_security_events():
    """Delete SecurityEvent rows older than the configured retention window.

    Cold storage is System 22's job (the events are already in the outbox).
    We just keep the hot table small so dashboards stay fast.
    """
    from django.conf import settings

    from apps.audit.models import SecurityEvent

    days = getattr(settings, "EDGE_SECURITY_EVENT_RETENTION_DAYS", 90)
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = SecurityEvent.objects.filter(occurred_at__lt=cutoff).delete()
    if deleted:
        logger.info("cleanup_security_events: deleted %d row(s) < %s", deleted, cutoff)
    return deleted


@shared_task(name="apps.audit.tasks.daily_security_summary", queue="sla-monitor")
def daily_security_summary(target_date: str | None = None):
    """Aggregate yesterday's SecurityEvent rows and publish a summary.

    Used by the Security Operations Console's "daily summary" endpoint and
    by the notification bridge to email the Security Officer at 06:00 UTC.
    Output is shape-stable so dashboards can rely on it.
    """
    from django.db.models import Count

    from apps.audit.models import SecurityEvent
    from shared.events import publish

    target = _resolve_target_date(target_date)
    day_start = datetime.combine(target, time.min, tzinfo=py_timezone.utc)
    day_end = day_start + timedelta(days=1)

    queryset = SecurityEvent.objects.filter(
        occurred_at__gte=day_start, occurred_at__lt=day_end,
    )
    by_category = dict(
        queryset.values_list("category").annotate(c=Count("id")).values_list("category", "c")
    )
    by_severity = dict(
        queryset.values_list("severity").annotate(c=Count("id")).values_list("severity", "c")
    )
    top_ips = list(
        queryset.exclude(ip_address__isnull=True)
        .values("ip_address").annotate(c=Count("id")).order_by("-c")[:10]
    )

    summary = {
        "date": target.isoformat(),
        "total": queryset.count(),
        "by_category": by_category,
        "by_severity": by_severity,
        "top_ips": [{"ip": row["ip_address"], "count": row["c"]} for row in top_ips],
    }

    publish("SecurityDailySummary", summary, topic="nbes.secops")
    logger.info(
        "daily_security_summary: %s total=%d",
        target.isoformat(), summary["total"],
    )
    return summary


def _resolve_target_date(target_date):
    """``target_date`` may be a string (YYYY-MM-DD), a date, or None.
    None means *yesterday* — the canonical scheduled call at 01:00 UTC
    closes out the day that just ended."""
    if target_date is None:
        return (timezone.now().astimezone(py_timezone.utc) - timedelta(days=1)).date()
    if hasattr(target_date, "isoformat") and not isinstance(target_date, str):
        return target_date
    return datetime.fromisoformat(target_date).date()
