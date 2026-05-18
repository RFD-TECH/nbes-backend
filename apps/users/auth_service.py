"""apps/users/auth_service.py — Authentication, session issuance, refresh, logout.

Phase 1 / Sprint 1.1 — SRS §1.2.3 / §1.2.6 / §1.4.

Dev/interim mode: HS256 JWTs signed with settings.JWT_SECRET_KEY.
Production: Keycloak issues the JWT and this module proxies the session-tracking
side only. See memory/auth_mode.md.
"""
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

import jwt
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.users import throttle
from apps.users.models import LoginAttempt, MFAEnrolment, Session, UserProfile


# ── Internal roles that MUST satisfy MFA before the session is fully issued ──
# Candidate role is the only one for which MFA is optional. SRS §1.2.3.
INTERNAL_ROLES = {
    "nbec-member", "nbec-secretariat", "item-writer", "moderator",
    "examiner", "clet-registrar", "invigilator", "centre-coordinator",
    "remote-proctor", "dti-operations", "auditor", "system-administrator",
    "director-general", "service-desk-agent",
}


# ── Exceptions raised by this service ────────────────────────────────────────

class AuthError(Exception):
    """Base — has an SRS-aligned error code."""
    code = "AUTH_FAILED"
    status_code = 401


class InvalidCredentials(AuthError):
    code = "INVALID_CREDENTIALS"


class AccountLocked(AuthError):
    code = "ACCOUNT_LOCKED"
    status_code = 423


class AccountInactive(AuthError):
    code = "ACCOUNT_INACTIVE"
    status_code = 403


class IPThrottled(AuthError):
    code = "IP_THROTTLED"
    status_code = 429


class MFARequired(AuthError):
    """Raised after password OK; client must call /auth/mfa with the challenge token."""
    code = "MFA_REQUIRED"
    status_code = 200  # Not an error to the API consumer — a flow step.


class MFAInvalid(AuthError):
    code = "MFA_INVALID"


class InvalidRefreshToken(AuthError):
    code = "INVALID_REFRESH_TOKEN"


# ── Return shapes ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    session_id: str


@dataclass(frozen=True)
class MFAChallenge:
    """Returned when password auth succeeded but MFA is still required.

    `challenge_token` is a short-lived JWT identifying the pending session.
    Client passes it back to /auth/mfa along with the OTP code.
    """
    challenge_token: str
    factors: list[str]


# ── Token helpers ────────────────────────────────────────────────────────────

def _hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> timezone.datetime:
    return timezone.now()


def _make_access_token(user: UserProfile, jti: str) -> tuple[str, int]:
    lifetime = settings.ACCESS_TOKEN_LIFETIME_MINUTES * 60
    now = _now()
    payload = {
        "sub": str(user.keycloak_sub or user.id),
        "user_id": str(user.id),
        "email": user.email,
        "role": user.role,
        "roles": [user.role] if user.role else [],
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=lifetime)).timestamp()),
        "type": "access",
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, lifetime


def _make_refresh_token(jti: str) -> tuple[str, int]:
    lifetime = settings.REFRESH_TOKEN_LIFETIME_DAYS * 24 * 60 * 60
    now = _now()
    raw = secrets.token_urlsafe(48)
    payload = {
        "jti": jti,
        "token": raw,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=lifetime)).timestamp()),
        "type": "refresh",
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, lifetime


def _make_mfa_challenge(user: UserProfile) -> str:
    """Short-lived JWT that proves the user passed password but hasn't done MFA yet."""
    now = _now()
    payload = {
        "user_id": str(user.id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "type": "mfa_challenge",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _decode_mfa_challenge(token: str) -> UserProfile:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError as e:
        raise MFAInvalid(f"Invalid challenge token: {e}")
    if payload.get("type") != "mfa_challenge":
        raise MFAInvalid("Wrong token type.")
    user_id = payload.get("user_id")
    try:
        return UserProfile.objects.get(id=user_id)
    except UserProfile.DoesNotExist:
        raise MFAInvalid("User not found.")


# ── Session issuance ─────────────────────────────────────────────────────────

def _issue_session(
    user: UserProfile,
    *,
    ip: str | None,
    user_agent: str,
    mfa_verified: bool,
) -> IssuedTokens:
    jti = uuid.uuid4().hex
    access_token, access_ttl = _make_access_token(user, jti)
    refresh_token, refresh_ttl = _make_refresh_token(jti)

    session = Session.objects.create(
        user=user,
        jti=jti,
        refresh_token_hash=_hash_refresh(refresh_token),
        expires_at=_now() + timedelta(seconds=refresh_ttl),
        mfa_verified_at=_now() if mfa_verified else None,
        ip=ip,
        user_agent=user_agent or "",
    )

    user.last_login_at = _now()
    user.failed_login_count = 0
    user.locked_until = None
    user.save(update_fields=["last_login_at", "failed_login_count", "locked_until", "updated_at"])

    return IssuedTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=access_ttl,
        refresh_expires_in=refresh_ttl,
        session_id=str(session.id),
    )


# ── Login ────────────────────────────────────────────────────────────────────

def _record_attempt(user, email, ip, user_agent, outcome) -> None:
    LoginAttempt.objects.create(
        user=user,
        email_attempted=email,
        ip=ip or "0.0.0.0",
        user_agent=user_agent or "",
        outcome=outcome,
    )


def authenticate(
    *,
    email: str,
    password: str,
    ip: str | None,
    user_agent: str = "",
) -> IssuedTokens | MFAChallenge:
    """Credential check + MFA gate + session issuance.

    Returns IssuedTokens for users without MFA requirement, or MFAChallenge
    for internal roles. Raises AuthError subclasses on failure.
    """
    if ip and throttle.is_blocked(ip):
        _record_attempt(None, email, ip, user_agent, LoginAttempt.Outcome.IP_THROTTLED)
        raise IPThrottled("Too many failed attempts from this address.")

    try:
        user = UserProfile.objects.get(email__iexact=email)
    except UserProfile.DoesNotExist:
        if ip:
            throttle.record_failure(ip)
        _record_attempt(None, email, ip, user_agent, LoginAttempt.Outcome.UNKNOWN_USER)
        raise InvalidCredentials("Invalid email or password.")

    if user.status == UserProfile.Status.DEACTIVATED:
        _record_attempt(user, email, ip, user_agent, LoginAttempt.Outcome.ACCOUNT_LOCKED)
        raise AccountInactive("Account has been deactivated.")

    if user.is_locked:
        _record_attempt(user, email, ip, user_agent, LoginAttempt.Outcome.ACCOUNT_LOCKED)
        raise AccountLocked("Account is temporarily locked. Try again later.")

    if not user.check_password(password):
        with transaction.atomic():
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= settings.MAX_FAILED_LOGINS:
                user.locked_until = _now() + timedelta(minutes=settings.ACCOUNT_LOCKOUT_MINUTES)
            user.save(update_fields=["failed_login_count", "locked_until", "updated_at"])
        if ip:
            throttle.record_failure(ip)
        _record_attempt(user, email, ip, user_agent, LoginAttempt.Outcome.BAD_CREDENTIAL)
        raise InvalidCredentials("Invalid email or password.")

    # Password check passed.
    if ip:
        throttle.record_success(ip)

    needs_mfa = (user.role in INTERNAL_ROLES) and user.mfa_enrolled
    if needs_mfa:
        factors = list(
            user.mfa_enrolments
            .filter(confirmed_at__isnull=False)
            .values_list("factor_type", flat=True)
        )
        _record_attempt(user, email, ip, user_agent, LoginAttempt.Outcome.SUCCESS)
        return MFAChallenge(challenge_token=_make_mfa_challenge(user), factors=factors)

    _record_attempt(user, email, ip, user_agent, LoginAttempt.Outcome.SUCCESS)
    tokens = _issue_session(user, ip=ip, user_agent=user_agent, mfa_verified=False)
    AuditEvent.record(
        actor_id=user.id,
        action="LOGIN_SUCCESS",
        entity_type="user",
        entity_id=user.id,
        new_state={"mfa": False},
        ip_address=ip,
    )
    return tokens


# ── MFA verify (TOTP) ────────────────────────────────────────────────────────

def verify_mfa_totp(
    *,
    challenge_token: str,
    code: str,
    ip: str | None,
    user_agent: str = "",
) -> IssuedTokens:
    import pyotp

    user = _decode_mfa_challenge(challenge_token)

    enrolments = list(
        user.mfa_enrolments.filter(
            factor_type=MFAEnrolment.FactorType.TOTP,
            confirmed_at__isnull=False,
        )
    )
    if not enrolments:
        raise MFAInvalid("No confirmed TOTP enrolment.")

    for enrolment in enrolments:
        totp = pyotp.TOTP(enrolment.credential_ref)
        if totp.verify(code, valid_window=1):
            enrolment.last_used_at = _now()
            enrolment.save(update_fields=["last_used_at"])
            _record_attempt(user, user.email, ip, user_agent, LoginAttempt.Outcome.SUCCESS)
            tokens = _issue_session(user, ip=ip, user_agent=user_agent, mfa_verified=True)
            AuditEvent.record(
                actor_id=user.id,
                action="LOGIN_SUCCESS",
                entity_type="user",
                entity_id=user.id,
                new_state={"mfa": True, "factor": "totp"},
                ip_address=ip,
            )
            return tokens

    if ip:
        throttle.record_failure(ip)
    _record_attempt(user, user.email, ip, user_agent, LoginAttempt.Outcome.MFA_FAIL)
    raise MFAInvalid("Invalid MFA code.")


# ── Refresh ──────────────────────────────────────────────────────────────────

def refresh_session(*, refresh_token: str, ip: str | None, user_agent: str = "") -> IssuedTokens:
    try:
        payload = jwt.decode(
            refresh_token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.PyJWTError as e:
        raise InvalidRefreshToken(f"Invalid refresh token: {e}")

    if payload.get("type") != "refresh":
        raise InvalidRefreshToken("Wrong token type.")

    jti = payload.get("jti")
    if not jti:
        raise InvalidRefreshToken("Missing jti.")

    try:
        session = Session.objects.select_related("user").get(jti=jti)
    except Session.DoesNotExist:
        raise InvalidRefreshToken("Unknown session.")

    if not session.is_active:
        raise InvalidRefreshToken("Session no longer active.")

    if session.refresh_token_hash != _hash_refresh(refresh_token):
        # Token reuse attack — revoke the whole session.
        session.revoke(reason="refresh_token_reuse")
        AuditEvent.record(
            actor_id=session.user_id,
            action="REFRESH_TOKEN_REUSE",
            entity_type="session",
            entity_id=session.id,
            ip_address=ip,
        )
        raise InvalidRefreshToken("Refresh token mismatch.")

    if session.user.status == UserProfile.Status.DEACTIVATED:
        session.revoke(reason="user_deactivated")
        raise AccountInactive("Account has been deactivated.")

    # Rotate: revoke old session, issue new one.
    session.revoke(reason="rotated")
    return _issue_session(
        session.user, ip=ip, user_agent=user_agent,
        mfa_verified=session.mfa_verified_at is not None,
    )


# ── Logout ───────────────────────────────────────────────────────────────────

def logout(*, jti: str, actor_id, ip: str | None) -> None:
    try:
        session = Session.objects.get(jti=jti)
    except Session.DoesNotExist:
        return
    if session.revoked_at is None:
        session.revoke(reason="user_logout")
        AuditEvent.record(
            actor_id=actor_id,
            action="LOGOUT",
            entity_type="session",
            entity_id=session.id,
            ip_address=ip,
        )


def revoke_all_user_sessions(user: UserProfile, *, reason: str, actor_id=None) -> int:
    """Used on deactivation and on high-impact role changes (SRS §1.2.2 — 60-second propagation)."""
    sessions = Session.objects.filter(user=user, revoked_at__isnull=True)
    count = 0
    for s in sessions:
        s.revoke(reason=reason)
        count += 1
    if count:
        AuditEvent.record(
            actor_id=actor_id or user.id,
            action="SESSIONS_REVOKED",
            entity_type="user",
            entity_id=user.id,
            new_state={"sessions_revoked": count, "reason": reason},
        )
    return count
