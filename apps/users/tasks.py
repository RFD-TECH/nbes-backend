"""Periodic tasks for user profile management.

Currently implements:
  * ``expire_pending_role_approvals`` — marks RoleAssignmentApproval records
    that have passed their ``expires_at`` timestamp as ``expired``.  Runs
    every hour via Celery Beat.  Blueprint §1.2.2 specifies 48-hour expiry.
"""
from celery import shared_task


@shared_task(name="apps.users.tasks.expire_pending_role_approvals")
def expire_pending_role_approvals():
    """Mark stale pending RoleAssignmentApproval records as expired.

    A pending record is considered expired once ``expires_at < now()``.
    Returns the count of records that were transitioned to ``expired``.
    """
    from django.utils import timezone
    from apps.users.models import RoleAssignmentApproval
    from apps.audit.models import AuditEvent

    now = timezone.now()
    pending_expired = RoleAssignmentApproval.objects.filter(
        status=RoleAssignmentApproval.STATUS_PENDING,
        expires_at__lt=now,
    )
    expired_count = 0
    for approval in pending_expired:
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
            }
        )
        expired_count += 1

    return {"expired": expired_count, "at": now.isoformat()}
