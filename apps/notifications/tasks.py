"""Celery tasks for the notification bridge.

``deliver_notification`` fetches a queued Notification record and forwards
it to System 21 (Communications) via the canonical System 17 client.
On transient failure it retries with exponential backoff (max 3 attempts).
"""
from __future__ import annotations

import logging

from celery import shared_task

from .models import DeliveryLog, Notification

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def deliver_notification(self, notification_id: str) -> None:
    """Deliver a single queued Notification to System 21.

    System 21 is called via ``shared.integrations.call_system_17`` so all
    signed, replay-protected semantics apply automatically.
    """
    from shared.integrations import call_system_17

    try:
        notification = Notification.objects.get(pk=notification_id)
    except Notification.DoesNotExist:
        logger.warning("deliver_notification: id=%s not found — skipping", notification_id)
        return

    if notification.status == Notification.Status.DELIVERED:
        return

    attempt_num = notification.retry_count + 1
    idempotency_key = f"notif-{notification_id}"

    payload = {
        "notification_id": notification_id,
        "event_name": notification.event_name,
        "recipient_email": notification.recipient_email,
        "recipient_phone": notification.recipient_phone,
        "subject": notification.rendered_subject,
        "body": notification.rendered_body,
        "channel": notification.template.channel if notification.template else "email",
    }

    result = call_system_17(
        endpoint="/api/v1/communications/send",
        payload=payload,
        method="POST",
        idempotency_key=idempotency_key,
    )

    success = result.ok
    system_21_ref = (result.data or {}).get("message_id", "") if success else ""

    DeliveryLog.objects.create(
        notification=notification,
        attempt=attempt_num,
        system_21_ref=system_21_ref,
        success=success,
        error_message="" if success else result.message,
    )

    if success:
        notification.status = Notification.Status.DELIVERED
        notification.retry_count = attempt_num
        notification.save(update_fields=["status", "retry_count", "updated_at"])
        logger.info("deliver_notification: delivered id=%s ref=%s", notification_id, system_21_ref)
    else:
        notification.retry_count = attempt_num
        notification.save(update_fields=["retry_count", "updated_at"])
        logger.warning(
            "deliver_notification: failed id=%s attempt=%d: %s",
            notification_id, attempt_num, result.message,
        )
        if result.retryable:
            try:
                raise self.retry(
                    exc=RuntimeError(result.message),
                    countdown=60 * (2 ** (attempt_num - 1)),
                )
            except self.MaxRetriesExceededError:
                logger.error(
                    "deliver_notification: max retries exhausted id=%s", notification_id
                )
                notification.status = Notification.Status.FAILED
                notification.save(update_fields=["status", "updated_at"])
                return
        notification.status = Notification.Status.FAILED
        notification.save(update_fields=["status", "updated_at"])
