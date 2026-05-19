"""apps/committee/tasks.py — NBEC Committee Celery tasks."""
import logging
from datetime import date

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(queue="sla-monitor")
def monitor_tenure_expiry():
    """Daily: expire NBECMember records whose tenure_end_date has passed.

    Runs every day at 00:30 UTC via Celery Beat (config/celery.py).
    Members with tenure_end_date < today and status='active' are moved to Expired.
    """
    from apps.audit.models import AuditEvent
    from . import events as ev
    from .models import NBECMember

    today = date.today()
    due = NBECMember.objects.filter(
        status=NBECMember.Status.ACTIVE,
        tenure_end_date__lt=today,
    )
    expired_count = 0
    for member in due:
        try:
            member.expire()
            AuditEvent.record(
                actor_id=None,
                action=ev.MEMBER_EXPIRED,
                entity_type="committee_member",
                entity_id=member.id,
                old_state={"status": "active"},
                new_state={"status": "expired", "tenure_end_date": str(member.tenure_end_date)},
            )
            expired_count += 1
        except Exception:
            logger.exception("Failed to expire member %s", member.id)

    if expired_count:
        logger.info("monitor_tenure_expiry: expired %d member(s)", expired_count)
    return {"expired": expired_count}


@shared_task(queue="sla-monitor")
def escalate_overdue_actions():
    """Daily: mark ActionItems as Overdue when due_date < today and still open.

    Runs every day at 01:30 UTC via Celery Beat (config/celery.py).
    Emits an audit event and a domain event for each escalation so that
    System 21 (Notifications) can send a reminder to the assignee.
    """
    from apps.audit.models import AuditEvent
    from shared.events import publish
    from . import events as ev
    from .models import ActionItem

    today = date.today()
    overdue = ActionItem.objects.filter(
        status__in=[ActionItem.Status.OPEN, ActionItem.Status.IN_PROGRESS],
        due_date__lt=today,
    )
    escalated_count = 0
    for item in overdue:
        try:
            item.status = ActionItem.Status.OVERDUE
            item.last_escalated_at = timezone.now()
            item.save(update_fields=["status", "last_escalated_at"])
            AuditEvent.record(
                actor_id=None,
                action=ev.ACTION_ITEM_ESCALATED,
                entity_type="action_item",
                entity_id=item.id,
                new_state={
                    "due_date": str(item.due_date),
                    "assigned_to_id": str(item.assigned_to_id),
                },
            )
            publish("ActionItemEscalated", {
                "action_item_id": str(item.id),
                "assigned_to_id": str(item.assigned_to_id),
                "due_date": str(item.due_date),
            })
            escalated_count += 1
        except Exception:
            logger.exception("Failed to escalate action item %s", item.id)

    if escalated_count:
        logger.info("escalate_overdue_actions: escalated %d action item(s)", escalated_count)
    return {"escalated": escalated_count}
