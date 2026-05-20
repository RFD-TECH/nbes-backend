"""apps/audit/tasks.py — Outbox poller + daily hash-anchor export."""
import datetime
import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="apps.audit.tasks.poll_outbox", queue="outbox")
def poll_outbox():
    """Relay unpublished OutboxEvents to Kafka (or mark as published in dev)."""
    from django.conf import settings
    from apps.audit.models import OutboxEvent

    if settings.KAFKA_ENABLED:
        raise RuntimeError("KAFKA_ENABLED is True but Kafka producer is not configured.")

    unpublished = OutboxEvent.objects.filter(published=False).order_by("created_at")[:100]
    for event in unpublished:
        try:
            event.published = True
            event.published_at = timezone.now()
            event.save(update_fields=["published", "published_at"])
        except Exception as exc:
            logger.error("OutboxEvent %s failed: %s", event.id, exc)


@shared_task(name="apps.audit.tasks.export_daily_audit_anchor", queue="marking-high")
def export_daily_audit_anchor():
    """
    Export yesterday's audit chain head-hash to System 22 by 01:00 UTC.
    Creates or updates DailyHashAnchor. Idempotent — safe to retry.

    Beat schedule: daily at 01:00 UTC (see config/celery.py).
    On failure: logs to audit_export_failed.jsonl for manual replay.
    """
    import json
    from pathlib import Path
    from django.conf import settings
    from apps.audit.models import AuditEvent, DailyHashAnchor
    from shared.integrations.system22 import System22Client

    now_utc = timezone.now()
    yesterday = (now_utc - datetime.timedelta(days=1)).date()
    day_start = datetime.datetime(yesterday.year, yesterday.month, yesterday.day,
                                  tzinfo=datetime.timezone.utc)
    day_end = day_start + datetime.timedelta(days=1)

    anchor, _ = DailyHashAnchor.objects.get_or_create(
        date=yesterday,
        defaults={"head_hash": "0" * 64, "event_count": 0},
    )

    if anchor.exported_to_s22_at:
        logger.info("Audit anchor %s already exported — skipping.", yesterday)
        return

    qs = AuditEvent.objects.filter(
        created_at__gte=day_start, created_at__lt=day_end
    ).order_by("id")
    count = qs.count()

    if count == 0:
        head_hash = "0" * 64
        logger.info("No audit events for %s — exporting zero-event anchor.", yesterday)
    else:
        last = qs.values("chain_hash").last()
        head_hash = last["chain_hash"]

    anchor.head_hash = head_hash
    anchor.event_count = count
    anchor.save(update_fields=["head_hash", "event_count"])

    try:
        client = System22Client()
        ref = client.export_audit_anchor(
            date=str(yesterday),
            head_hash=head_hash,
            event_count=count,
        )
        anchor.exported_to_s22_at = timezone.now()
        anchor.anchor_ref = ref
        anchor.save(update_fields=["exported_to_s22_at", "anchor_ref"])
        logger.info("Audit anchor %s exported to System 22. ref=%s", yesterday, ref)
    except Exception as exc:
        logger.error("Audit anchor export failed for %s: %s", yesterday, exc)
        _write_fallback_log(yesterday, head_hash, count, str(exc))
        raise


def _write_fallback_log(date, head_hash, event_count, error):
    """Append to local fallback file when System 22 is unreachable."""
    import json
    from pathlib import Path
    from django.conf import settings

    log_dir = Path(settings.BASE_DIR) / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "audit_export_failed.jsonl"
    entry = json.dumps({
        "date": str(date),
        "head_hash": head_hash,
        "event_count": event_count,
        "error": error,
        "ts": timezone.now().isoformat(),
    })
    with open(log_file, "a") as f:
        f.write(entry + "\n")
