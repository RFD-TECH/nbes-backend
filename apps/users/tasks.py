"""Periodic tasks for user profile management.

Currently implements:
  * ``expire_pending_role_approvals`` — marks RoleAssignmentApproval records
    that have passed their ``expires_at`` timestamp as ``expired``.  Runs
    every hour via Celery Beat.  Blueprint §1.2.2 specifies 48-hour expiry.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="apps.users.tasks.expire_pending_role_approvals")
def expire_pending_role_approvals():
    """Mark stale pending RoleAssignmentApproval records as expired.

    A pending record is considered expired once ``expires_at < now()``.
    Returns the count of records that were transitioned to ``expired``.
    """
    from django.db import transaction, connection
    from django.utils import timezone
    from apps.users.models import RoleAssignmentApproval
    from apps.audit.models import AuditEvent

    now = timezone.now()
    expired_count = 0

    with transaction.atomic():
        qs = RoleAssignmentApproval.objects.filter(
            status=RoleAssignmentApproval.STATUS_PENDING,
            expires_at__lt=now,
        )
        if connection.features.has_select_for_update_skip_locked:
            qs = qs.select_for_update(skip_locked=True)
        for approval in qs:
            try:
                with transaction.atomic():
                    approval.status = RoleAssignmentApproval.STATUS_EXPIRED
                    approval.save(update_fields=["status", "updated_at"])
                    AuditEvent.record(
                        actor_id=None,
                        action="ROLE_APPROVAL_EXPIRED",
                        entity_type="rbac",
                        entity_id=approval.target_user_id,
                        old_state={
                            "role": approval.role.name,
                            "approval_id": str(approval.id),
                            "status": "pending",
                        },
                        new_state={
                            "status": "expired",
                            "expired_at": now.isoformat(),
                        },
                    )
                    expired_count += 1
            except Exception:
                logger.exception(
                    "expire_pending_role_approvals: failed for approval=%s", approval.id
                )

    return {"expired": expired_count, "at": now.isoformat()}
