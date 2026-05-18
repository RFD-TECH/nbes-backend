"""apps/users/services.py — Admin user management.

SRS §1.2.1 (User Account Management) + §1.2.3 (password policy).
All business logic for the Admin User Console lives here. Views call these
functions; they never touch the model directly.
"""
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.users.auth_service import revoke_all_user_sessions
from apps.users.models import PasswordHistory, UserProfile
from apps.users.password_policy import (
    PASSWORD_HISTORY_DEPTH,
    validate_password,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _send_invite_email(user: UserProfile, token: str) -> None:
    """Console backend in dev — emails print to terminal. Real SMTP in prod via System 21."""
    accept_url = f"{getattr(settings, 'INVITE_ACCEPT_URL_BASE', '')}/invite?token={token}"
    body = (
        f"Hello {user.full_name},\n\n"
        f"You have been invited to the NBES platform with the role: {user.role}.\n"
        f"Set your password and configure MFA at:\n  {accept_url}\n\n"
        f"This link expires in {settings.INVITE_TOKEN_LIFETIME_DAYS} days.\n"
    )
    send_mail(
        subject="Your NBES account invitation",
        message=body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@nbes.local"),
        recipient_list=[user.email],
        fail_silently=True,
    )


def _record_password(user: UserProfile, raw_password: str) -> None:
    user.set_password(raw_password)
    user.save(update_fields=["password_hash", "password_changed_at", "updated_at"])
    PasswordHistory.objects.create(user=user, password_hash=user.password_hash)
    # Prune beyond depth so the table doesn't grow unbounded.
    keep_ids = list(
        user.password_history.order_by("-created_at")
        .values_list("id", flat=True)[:PASSWORD_HISTORY_DEPTH]
    )
    user.password_history.exclude(id__in=keep_ids).delete()


# ── Admin operations ─────────────────────────────────────────────────────────

@transaction.atomic
def create_user(
    *,
    email: str,
    first_name: str,
    last_name: str,
    role: str,
    actor: UserProfile | None,
    actor_ip: str | None = None,
) -> UserProfile:
    """Create a new user in Invited state and dispatch the invite email."""
    if UserProfile.objects.filter(email__iexact=email).exists():
        raise ValueError(f"A user with email {email} already exists.")

    user = UserProfile.objects.create(
        email=email,
        first_name=first_name,
        last_name=last_name,
        role=role,
        status=UserProfile.Status.INVITED,
        created_by=actor,
    )
    token = user.issue_invite_token(lifetime_days=settings.INVITE_TOKEN_LIFETIME_DAYS)

    AuditEvent.record(
        actor_id=actor.id if actor else None,
        action="USER_CREATED",
        entity_type="user",
        entity_id=user.id,
        new_state={"email": email, "role": role, "status": user.status},
        ip_address=actor_ip,
    )
    _send_invite_email(user, token)
    return user


@transaction.atomic
def accept_invite(*, token: str, password: str, ip: str | None = None) -> UserProfile:
    """User follows the invite link, sets a password, activates the account."""
    try:
        user = UserProfile.objects.get(invite_token=token)
    except UserProfile.DoesNotExist:
        raise ValueError("Invalid or expired invite token.")

    if not user.invite_expires_at or user.invite_expires_at < timezone.now():
        raise ValueError("Invite token has expired.")

    validate_password(user, password)
    _record_password(user, password)

    user.status = UserProfile.Status.ACTIVE
    user.clear_invite_token()
    user.save(update_fields=["status", "updated_at"])

    AuditEvent.record(
        actor_id=user.id,
        action="USER_INVITE_ACCEPTED",
        entity_type="user",
        entity_id=user.id,
        new_state={"status": user.status},
        ip_address=ip,
    )
    return user


@transaction.atomic
def edit_user(
    *,
    user_id,
    actor: UserProfile,
    actor_ip: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    role: str | None = None,
    status: str | None = None,
) -> UserProfile:
    user = UserProfile.objects.select_for_update().get(id=user_id)
    before = {
        "first_name": user.first_name,
        "last_name": user.last_name,
        "role": user.role,
        "status": user.status,
    }
    role_changed = False

    if first_name is not None:
        user.first_name = first_name
    if last_name is not None:
        user.last_name = last_name
    if role is not None and role != user.role:
        user.role = role
        role_changed = True
    if status is not None and status != user.status:
        user.status = status

    user.save()

    AuditEvent.record(
        actor_id=actor.id,
        action="USER_EDITED",
        entity_type="user",
        entity_id=user.id,
        old_state=before,
        new_state={
            "first_name": user.first_name,
            "last_name": user.last_name,
            "role": user.role,
            "status": user.status,
        },
        ip_address=actor_ip,
    )

    # Role change → revoke active sessions so the user re-acquires permissions on next login
    # (SRS §1.2.2 — propagation within 60 seconds).
    if role_changed:
        revoke_all_user_sessions(user, reason="role_changed", actor_id=actor.id)

    return user


@transaction.atomic
def deactivate_user(
    *,
    user_id,
    actor: UserProfile,
    actor_ip: str | None = None,
    reason: str = "",
) -> UserProfile:
    """Deactivate a user. Active sessions are revoked immediately
    (SRS §1.2.1 — sessions terminated within 60 s).
    """
    user = UserProfile.objects.select_for_update().get(id=user_id)
    if not _can_deactivate(user):
        raise ValueError("Cannot deactivate a user with open active assignments.")

    user.status = UserProfile.Status.DEACTIVATED
    user.deactivated_at = timezone.now()
    user.save(update_fields=["status", "deactivated_at", "updated_at"])

    revoke_all_user_sessions(user, reason="user_deactivated", actor_id=actor.id)

    AuditEvent.record(
        actor_id=actor.id,
        action="USER_DEACTIVATED",
        entity_type="user",
        entity_id=user.id,
        new_state={"status": user.status, "reason": reason},
        ip_address=actor_ip,
    )
    return user


def _can_deactivate(user: UserProfile) -> bool:
    """SRS §1.2.1 — 'Cannot delete an account with open active assignments
    (e.g. an Examiner with scripts in their queue).'

    Wire each app's open-assignment check here as those apps' services land.
    Phase 1 ships with the stub — Phase 9 (marking) wires the Examiner check,
    Phase 3 (itembank) wires the Item Writer check, and so on.
    """
    return True


@transaction.atomic
def reset_mfa(*, user_id, actor: UserProfile, actor_ip: str | None = None) -> UserProfile:
    """Admin-mediated MFA reset — clears all confirmed factors and forces re-enrolment."""
    user = UserProfile.objects.select_for_update().get(id=user_id)
    user.mfa_enrolments.all().delete()
    user.mfa_enrolled = False
    user.save(update_fields=["mfa_enrolled", "updated_at"])

    revoke_all_user_sessions(user, reason="mfa_reset", actor_id=actor.id)

    AuditEvent.record(
        actor_id=actor.id,
        action="USER_MFA_RESET",
        entity_type="user",
        entity_id=user.id,
        ip_address=actor_ip,
    )
    return user
