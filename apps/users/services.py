"""Business logic for user profile and role management.

Views delegate to these functions so the logic is independently testable and
reusable (e.g. by bulk-import, management commands, Celery tasks).

Raised exceptions carry a ``code`` attribute that views convert to HTTP error
responses without needing to know about the underlying cause.
"""
from __future__ import annotations

import uuid
import logging
from datetime import date

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Custom exceptions ────────────────────────────────────────────────────────

class ServiceError(Exception):
    """Base exception for all service-layer errors."""
    def __init__(self, message: str, code: str = "SERVICE_ERROR"):
        super().__init__(message)
        self.code = code


class RoleApprovalPending(Exception):
    """Raised when a high-privilege role assignment is queued for approval.

    Not an error — callers should return 202 Accepted with the approval ID.
    """
    def __init__(self, approval):
        super().__init__("Role assignment pending second-administrator approval.")
        self.approval = approval


# ── User provisioning ────────────────────────────────────────────────────────

@transaction.atomic
def create_user(
    *,
    email: str,
    first_name: str,
    last_name: str,
    roles: list[str],
    effective_date: date | None = None,
    metadata: dict | None = None,
    created_by=None,
    actor_id: str | None = None,
    request_id=None,
    ip_address: str | None = None,
):
    """Provision a new NBES user: IAM account + local profile + role assignments.

    Returns the created ``UserProfile`` instance.
    Raises ``ServiceError`` on IAM failure or validation errors.
    """
    from apps.users.models import UserProfile, Role
    from apps.audit.models import AuditEvent
    from shared import keycloak_admin
    from shared.events import publish

    effective_date = effective_date or timezone.now().date()
    metadata = metadata or {}

    # Validate all roles exist before touching IAM
    role_objects: list = []
    for role_name in roles:
        try:
            role_objects.append(Role.objects.get(name=role_name, is_active=True))
        except Role.DoesNotExist:
            raise ServiceError(
                f"Role '{role_name}' does not exist or is inactive.",
                code="ROLE_NOT_FOUND",
            )

    # IAM provisioning
    try:
        keycloak_uuid = keycloak_admin.create_user(
            email=email,
            first_name=first_name,
            last_name=last_name,
            roles=roles,
            send_invite=True,
        )
    except keycloak_admin.IntegrationError as exc:
        raise ServiceError(
            f"Failed to provision user in IAM: {exc}",
            code="IAM_PROVISIONING_FAILED",
        ) from exc

    keycloak_sub_val = None
    if keycloak_uuid:
        try:
            keycloak_sub_val = uuid.UUID(keycloak_uuid)
        except ValueError:
            pass

    # Create local profile
    user = UserProfile.objects.create(
        keycloak_sub=keycloak_sub_val,
        email=email,
        first_name=first_name,
        last_name=last_name,
        status="pending_invite",
        metadata=metadata,
        created_by=created_by,
    )

    # Assign roles through assign_role() so mutual-exclusion and approval
    # checks run for every role, including initial provisioning grants.
    for role_name in roles:
        try:
            assign_role(
                user=user,
                role_name=role_name,
                effective_from=effective_date,
                assigned_by=created_by,
                reason="Admin provisioning",
                actor_id=actor_id,
                request_id=request_id,
                ip_address=ip_address,
            )
        except RoleApprovalPending as exc:
            # High-privilege role queued for two-admin approval; user is still
            # created and the approval row is committed.
            logger.info(
                "create_user: role=%s for user=%s queued for approval=%s",
                role_name, user.id, exc.approval.id,
            )
        except ServiceError:
            raise

    AuditEvent.record(
        actor_id=actor_id,
        action="USER_CREATED",
        entity_type="user",
        entity_id=user.id,
        new_state={
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "roles": roles,
            "status": "pending_invite",
        },
        ip_address=ip_address,
        request_id=request_id,
    )

    publish(
        "UserCreated",
        {
            "user_id": str(user.id),
            "keycloak_sub": str(user.keycloak_sub) if user.keycloak_sub else None,
            "email": email,
            "roles": roles,
        },
    )

    # Provisioning confirmation email — non-fatal (IAM invite is primary)
    try:
        from apps.notifications.services import send_profile_ready
        send_profile_ready(user_id=str(user.id), email=email, first_name=first_name)
    except Exception:
        logger.exception("notifications.send_profile_ready failed for user=%s", user.id)

    return user


# ── Role assignment / revocation ─────────────────────────────────────────────

def assign_role(
    *,
    user,
    role_name: str,
    effective_from: date | None = None,
    effective_to: date | None = None,
    assigned_by=None,
    reason: str = "",
    actor_id: str | None = None,
    request_id=None,
    ip_address: str | None = None,
):
    """Assign ``role_name`` to ``user``.

    Returns the created ``UserRole`` on direct assignment.
    Raises ``RoleApprovalPending`` when the role requires two-admin approval.
    Raises ``ServiceError`` for validation failures (role not found, mutual
    exclusion conflict, duplicate assignment).
    """
    from apps.users.models import (
        Role, UserRole, RoleChangeEvent, RoleMutualExclusion,
        RoleAssignmentApproval, HIGH_PRIVILEGE_ROLES,
    )
    from apps.audit.models import AuditEvent
    from shared import keycloak_admin, rbac
    from shared.events import publish

    effective_from = effective_from or timezone.now().date()

    try:
        role = Role.objects.get(name=role_name, is_active=True)
    except Role.DoesNotExist:
        raise ServiceError(
            f"Role '{role_name}' does not exist or is inactive.",
            code="ROLE_NOT_FOUND",
        )

    # Mutual exclusion check
    conflict = RoleMutualExclusion.check_conflict(user, role)
    if conflict:
        conflicting = (
            conflict.role_a.name if conflict.role_b == role else conflict.role_b.name
        )
        raise ServiceError(
            f"Cannot assign '{role_name}': conflicts with '{conflicting}' "
            f"(mutual exclusion rule).",
            code="ROLE_CONFLICT",
        )

    # Duplicate check
    if UserRole.objects.filter(
        user=user, role=role, revoked_at__isnull=True
    ).exists():
        raise ServiceError(
            f"User already holds the '{role_name}' role.",
            code="ROLE_ALREADY_ASSIGNED",
        )

    # High-privilege roles require two-administrator approval.
    # Create the approval in its own savepoint so it is committed before
    # RoleApprovalPending propagates (the outer @transaction.atomic would
    # otherwise roll it back).
    if role_name in HIGH_PRIVILEGE_ROLES:
        from datetime import timedelta
        expires_at = timezone.now() + timedelta(hours=48)
        with transaction.atomic():
            approval = RoleAssignmentApproval.objects.create(
                target_user=user,
                role=role,
                requested_by=assigned_by,
                status="pending",
                effective_from=effective_from,
                effective_to=effective_to,
                expires_at=expires_at,
            )
            AuditEvent.record(
                actor_id=actor_id,
                action="ROLE_APPROVAL_REQUESTED",
                entity_type="rbac",
                entity_id=user.id,
                new_state={
                    "role": role.name,
                    "approval_id": str(approval.id),
                    "expires_at": expires_at.isoformat(),
                    "effective_to": str(effective_to) if effective_to else None,
                },
                ip_address=ip_address,
                request_id=request_id,
            )
        raise RoleApprovalPending(approval)

    # Direct assignment
    with transaction.atomic():
        user_role = UserRole.objects.create(
            user=user,
            role=role,
            effective_from=effective_from,
            effective_to=effective_to,
            assigned_by=assigned_by,
        )
        RoleChangeEvent.objects.create(
            user=user,
            role=role,
            change_type="assign",
            actor=assigned_by,
            reason=reason,
        )

        if user.keycloak_sub:
            try:
                keycloak_admin.assign_client_role(str(user.keycloak_sub), role_name)
            except Exception:
                logger.warning(
                    "keycloak_admin.assign_client_role failed user=%s role=%s",
                    user.id, role_name,
                )

        rbac.invalidate_role(role_name)
        if user.keycloak_sub:
            rbac.invalidate_user(str(user.keycloak_sub))

        AuditEvent.record(
            actor_id=actor_id,
            action="ROLE_ASSIGNED",
            entity_type="rbac",
            entity_id=user.id,
            old_state={"role": role_name, "status": "not_held"},
            new_state={
                "role": role_name,
                "effective_from": str(effective_from),
                "effective_to": str(effective_to) if effective_to else None,
            },
            ip_address=ip_address,
            request_id=request_id,
        )
        publish(
            "UserRoleChanged",
            {
                "user_id": str(user.id),
                "change_type": "assign",
                "role": role_name,
                "effective_from": str(effective_from),
                "effective_to": str(effective_to) if effective_to else None,
            },
        )
    return user_role


@transaction.atomic
def revoke_role(
    *,
    user,
    role_name: str,
    reason: str = "",
    actor=None,
    actor_id: str | None = None,
    request_id=None,
    ip_address: str | None = None,
):
    """Revoke ``role_name`` from ``user``.

    Returns the updated ``UserRole``.
    Raises ``ServiceError`` when the user does not hold the role.
    """
    from apps.users.models import Role, UserRole, RoleChangeEvent
    from apps.audit.models import AuditEvent
    from shared import keycloak_admin, rbac
    from shared.events import publish

    try:
        role = Role.objects.get(name=role_name)
    except Role.DoesNotExist:
        raise ServiceError(f"Role '{role_name}' not found.", code="ROLE_NOT_FOUND")

    try:
        user_role = UserRole.objects.select_for_update().get(
            user=user, role=role, revoked_at__isnull=True
        )
    except UserRole.DoesNotExist:
        raise ServiceError(
            f"User does not hold an active '{role_name}' assignment.",
            code="ROLE_NOT_ACTIVE",
        )

    user_role.revoked_at = timezone.now()
    user_role.revoke_reason = reason
    user_role.save(update_fields=["revoked_at", "revoke_reason"])

    RoleChangeEvent.objects.create(
        user=user,
        role=role,
        change_type="revoke",
        actor=actor,
        reason=reason,
    )

    if user.keycloak_sub:
        try:
            keycloak_admin.remove_client_role(str(user.keycloak_sub), role_name)
        except Exception:
            logger.warning(
                "keycloak_admin.remove_client_role failed user=%s role=%s",
                user.id, role_name,
            )

    rbac.invalidate_role(role_name)
    if user.keycloak_sub:
        rbac.invalidate_user(str(user.keycloak_sub))

    AuditEvent.record(
        actor_id=actor_id,
        action="ROLE_REVOKED",
        entity_type="rbac",
        entity_id=user.id,
        old_state={"role": role_name},
        new_state={"revoked_at": str(user_role.revoked_at), "reason": reason},
        ip_address=ip_address,
        request_id=request_id,
    )
    publish(
        "UserRoleChanged",
        {
            "user_id": str(user.id),
            "change_type": "revoke",
            "role": role_name,
        },
    )
    return user_role


# ── Pluggable Active Assignment Checks ──────────────────────────────
from typing import Callable  # noqa: E402

_ACTIVE_ASSIGNMENT_CHECKS: list[Callable] = []


def register_assignment_check(fn: Callable) -> None:
    """Register a callback function to check for active assignments for a user.

    The function must accept a ``UserProfile`` and return a string describing
    the active assignments if blocked, or ``None`` if not blocked.
    """
    if fn not in _ACTIVE_ASSIGNMENT_CHECKS:
        _ACTIVE_ASSIGNMENT_CHECKS.append(fn)


def has_active_assignments(user) -> list[str]:
    """Call all registered checks and return a list of blocking reasons."""
    reasons = []
    for check_fn in _ACTIVE_ASSIGNMENT_CHECKS:
        try:
            res = check_fn(user)
            if res:
                reasons.append(res)
        except Exception:
            logger.exception("Active assignment check failed.")
    return reasons


def _check_marking_assignments(user) -> str | None:
    try:
        from apps.marking.models import MarkingAssignment
        if MarkingAssignment.objects.filter(
            examiner_id=user.id, status__in=["Assigned", "InProgress"]
        ).exists():
            return "active marking assignments"
    except ImportError:
        pass
    return None


def _check_itembank_assignments(user) -> str | None:
    try:
        from apps.itembank.models import Item
        active_review_statuses = ["In Review", "Moderation Panel"]
        if Item.objects.filter(
            assigned_reviewer_id=user.id, status__in=active_review_statuses
        ).exists():
            return "items in active review queue"
    except ImportError:
        pass
    return None


def _check_committee_assignments(user) -> str | None:
    try:
        from apps.committee.models import NBECMember, ConflictDeclaration
        if user.keycloak_sub:
            member_ids = NBECMember.objects.filter(
                keycloak_sub=user.keycloak_sub
            ).values_list("id", flat=True)
            if (
                member_ids
                and ConflictDeclaration.objects.filter(
                    member_id__in=member_ids,
                    status=ConflictDeclaration.Status.APPROVED,
                ).exists()
            ):
                return "active conflict-of-interest declarations"
    except ImportError:
        return None
    except Exception:
        logger.exception(
            "_check_committee_assignments: unexpected error for user=%s",
            getattr(user, "keycloak_sub", None),
        )
        return "committee-check-failed"
    return None


_ACTIVE_ASSIGNMENT_CHECKS.extend([
    _check_marking_assignments,
    _check_itembank_assignments,
    _check_committee_assignments,
])
