"""Outbox poller, daily anchors, and SecOps jobs."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone as py_timezone

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from shared.integrations import call_system_17

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64


@shared_task(name="apps.audit.tasks.poll_outbox", queue="outbox")
def poll_outbox():
    """Relay unpublished OutboxEvent rows to the event bus."""
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
    logger.debug("dev outbox publish: %s -> %s", event.topic, event.event_name)


def _publish_via_system_17(event) -> None:
    """Production publish via System 17."""
    correlation_id_val = str(event.request_id) if event.request_id else str(event.correlation_id)

    response = call_system_17(
        endpoint=f"/v1/events/{event.topic}",
        payload={
            "event_name": event.event_name,
            "topic": event.topic,
            "payload": event.payload,
            "correlation_id": correlation_id_val,
            "occurred_at": event.created_at.isoformat(),
        },
        idempotency_key=str(event.correlation_id),
        correlation_id=correlation_id_val,
        traceparent=getattr(event, "traceparent", "") or "",
        tracestate=getattr(event, "tracestate", "") or "",
    )
    if not response.ok:
        raise RuntimeError(
            f"System 17 publish failed: code={response.code} "
            f"status={response.status_code} retryable={response.retryable}"
        )


@shared_task(name="apps.audit.tasks.daily_hash_anchor", queue="outbox", bind=True)
def daily_hash_anchor(self, target_date: str | None = None):
    """Anchor a UTC day's audit chain.

    Empty days carry forward the last chain hash before the day so the
    global audit chain remains continuous across zero-event days.
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
    previous_head = (
        AuditEvent.objects
        .filter(created_at__lt=day_start)
        .order_by("-id")
        .values("chain_hash")
        .first()
    )

    head_hash = (
        head["chain_hash"]
        if head
        else previous_head["chain_hash"] if previous_head else GENESIS_HASH
    )
    head_event_id = head["event_id"] if head else None

    with transaction.atomic():
        DailyHashAnchor.objects.update_or_create(
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


@shared_task(name="apps.audit.tasks.cleanup_security_events", queue="sla-monitor")
def cleanup_security_events():
    """Delete SecurityEvent rows older than the configured hot-retention window."""
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
    """Aggregate yesterday's SecurityEvent rows and publish a summary."""
    from django.db.models import Count

    from apps.audit.models import SecurityEvent
    from shared.events import publish

    target = _resolve_target_date(target_date)
    day_start = datetime.combine(target, time.min, tzinfo=py_timezone.utc)
    day_end = day_start + timedelta(days=1)

    queryset = SecurityEvent.objects.filter(
        occurred_at__gte=day_start,
        occurred_at__lt=day_end,
    )
    by_category = dict(
        queryset.values_list("category").annotate(c=Count("id")).values_list("category", "c")
    )
    by_severity = dict(
        queryset.values_list("severity").annotate(c=Count("id")).values_list("severity", "c")
    )
    top_ips = list(
        queryset.exclude(ip_address__isnull=True)
        .values("ip_address")
        .annotate(c=Count("id"))
        .order_by("-c")[:10]
    )

    summary = {
        "date": target.isoformat(),
        "total": queryset.count(),
        "by_category": by_category,
        "by_severity": by_severity,
        "top_ips": [{"ip": row["ip_address"], "count": row["c"]} for row in top_ips],
    }

    publish("SecurityDailySummary", summary, topic="nbes.secops")
    logger.info("daily_security_summary: %s total=%d", target.isoformat(), summary["total"])
    return summary


def _resolve_target_date(target_date):
    if target_date is None:
        return (timezone.now().astimezone(py_timezone.utc) - timedelta(days=1)).date()
    if hasattr(target_date, "isoformat") and not isinstance(target_date, str):
        return target_date
    return datetime.fromisoformat(target_date).date()


@shared_task(name="apps.audit.tasks.precreate_audit_partitions", queue="outbox")
def precreate_audit_partitions():
    """
    Celery task that runs to precreate the PostgreSQL partition for the next calendar year.
    Only executes if the database is PostgreSQL.
    """
    from django.db import connection
    if connection.vendor != "postgresql":
        logger.info("precreate_audit_partitions: skipped on non-postgresql database")
        return {"status": "skipped", "reason": "not postgresql"}

    current_year = datetime.now(py_timezone.utc).year
    next_year = current_year + 1
    partition_name = f"audit_auditevent_y{next_year}"
    start_date = f"{next_year}-01-01 00:00:00+00"
    end_date = f"{next_year + 1}-01-01 00:00:00+00"

    sql = f"""
        CREATE TABLE IF NOT EXISTS {partition_name}
        PARTITION OF audit_auditevent
        FOR VALUES FROM ('{start_date}') TO ('{end_date}');
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)

    logger.info("precreate_audit_partitions: ensured partition %s exists", partition_name)
    return {"status": "success", "partition": partition_name}
