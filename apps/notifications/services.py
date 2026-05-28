"""Notification dispatch service.

Queues a Notification record and renders it against the matching
NotificationTemplate.  Delivery to System 21 is handled asynchronously
by ``tasks.deliver_notification``.

Blueprint §1.5 (notification-bridge light):
  "sends emails for provisioning confirmations ('your NBES profile is
  ready') via System 21. All password/verification emails come from IAM."
"""
from __future__ import annotations

import logging
from django.template import Context, Template

from .models import Notification, NotificationTemplate

logger = logging.getLogger(__name__)


def queue_notification(
    event_name: str,
    recipient_id: str,
    recipient_email: str = "",
    recipient_phone: str = "",
    context: dict | None = None,
) -> "Notification | None":
    """Create a queued Notification for *event_name*.

    Looks up the matching active ``NotificationTemplate``. If no template
    exists the call is a no-op (returns ``None``) — missing templates are
    logged but never raise, so missing configs don't break provisioning.
    """
    try:
        template = NotificationTemplate.objects.get(
            event_name=event_name, is_active=True
        )
    except NotificationTemplate.DoesNotExist:
        logger.info(
            "notifications: no active template for event=%s — skipping",
            event_name,
        )
        return None

    ctx = context or {}
    try:
        rendered_subject = Template(template.subject).render(Context(ctx))
        rendered_body = Template(template.body_template).render(Context(ctx))
    except Exception:
        logger.exception(
            "notifications: template render failed for event=%s", event_name
        )
        rendered_subject = template.subject
        rendered_body = ""

    notification = Notification.objects.create(
        template=template,
        recipient_id=recipient_id,
        recipient_email=recipient_email,
        recipient_phone=recipient_phone,
        event_name=event_name,
        context=ctx,
        rendered_subject=rendered_subject,
        rendered_body=rendered_body,
        status=Notification.Status.QUEUED,
    )

    from django.db import transaction as _tx
    from .tasks import deliver_notification
    _tx.on_commit(lambda: deliver_notification.delay(str(notification.id)))

    logger.info(
        "notifications: queued id=%s event=%s recipient=%s",
        notification.id, event_name, recipient_email,
    )
    return notification


def send_profile_ready(
    user_id: str,
    email: str,
    first_name: str,
) -> "Notification | None":
    """Send the 'your NBES profile is ready' provisioning confirmation.

    IAM sends the separate credential-setup invite; this confirms the
    NBES-side profile has been created.  Called after POST /admin/users/
    or bulk import succeeds.
    """
    return queue_notification(
        event_name="user.profile_ready",
        recipient_id=user_id,
        recipient_email=email,
        context={"first_name": first_name, "email": email},
    )
