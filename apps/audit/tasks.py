"""apps/audit/tasks.py - Outbox poller."""
from celery import shared_task
from django.utils import timezone
import logging
logger = logging.getLogger(__name__)

@shared_task(name="apps.audit.tasks.poll_outbox", queue="outbox")
def poll_outbox():
    from django.conf import settings
    from apps.audit.models import OutboxEvent
    unpublished = OutboxEvent.objects.filter(published=False).order_by("created_at")[:100]
    for event in unpublished:
        try:
            if settings.KAFKA_ENABLED:
                raise NotImplementedError("Kafka not configured. Set KAFKA_ENABLED=False for dev.")
            event.published = True
            event.published_at = timezone.now()
            event.save(update_fields=["published", "published_at"])
        except Exception as exc:
            logger.error(f"OutboxEvent {event.id} failed: {exc}")