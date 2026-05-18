"""apps/users/models.py — Identity, MFA, sessions, login attempts.

Phase 1 / Sprint 1.1 — REQ-F000 identity substrate.

In dev mode the platform owns passwords (HS256 JWT, local credential check).
In production Keycloak owns auth — `keycloak_sub` is set from the JWT `sub`
claim and the local `password_hash`/`failed_login_count` fields are unused.
"""
import secrets
import uuid

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone


class UserProfile(models.Model):
    """Account record for every NBES/CBT user.

    SRS §1.2.1 — User Account Management.
    SRS §1.5.1 — user(id, email_unique, first_name, last_name, status,
                       mfa_enrolled, password_hash, password_changed_at,
                       created_by, created_at, deactivated_at).
    """

    class Status(models.TextChoices):
        INVITED = "invited", "Invited"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        DEACTIVATED = "deactivated", "Deactivated"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Identity
    keycloak_sub = models.UUIDField(null=True, blank=True, unique=True, db_index=True)
    email = models.EmailField(unique=True, db_index=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)

    # Role assignment — DB-backed Role table arrives in Sprint 1.2.
    # Until then `role` stays as a string and is read by shared/permissions.py.
    role = models.CharField(max_length=50, blank=True)

    # Lifecycle
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INVITED)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="created_users"
    )

    # Local credential (dev only — Keycloak owns auth in production)
    password_hash = models.CharField(max_length=255, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)

    # Brute-force defence (SRS §1.2.3)
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    # MFA (SRS §1.2.3)
    mfa_enrolled = models.BooleanField(default=False)

    # Invitation flow (SRS §1.2.1 — single-use first-time-login link, 7-day expiry)
    invite_token = models.CharField(max_length=64, blank=True, db_index=True)
    invite_expires_at = models.DateTimeField(null=True, blank=True)

    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users_userprofile"
        verbose_name = "User Profile"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["role"]),
        ]

    def __str__(self):
        return f"{self.email} ({self.role or 'no role'})"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email

    @property
    def is_authenticated(self):
        return True

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > timezone.now())

    def set_password(self, raw_password: str) -> None:
        self.password_hash = make_password(raw_password)
        self.password_changed_at = timezone.now()

    def check_password(self, raw_password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password(raw_password, self.password_hash)

    def issue_invite_token(self, lifetime_days: int = 7) -> str:
        from datetime import timedelta
        token = secrets.token_urlsafe(32)
        self.invite_token = token
        self.invite_expires_at = timezone.now() + timedelta(days=lifetime_days)
        self.save(update_fields=["invite_token", "invite_expires_at", "updated_at"])
        return token

    def clear_invite_token(self) -> None:
        self.invite_token = ""
        self.invite_expires_at = None
        self.save(update_fields=["invite_token", "invite_expires_at", "updated_at"])


class LoginAttempt(models.Model):
    """One row per credential attempt — success or failure.

    Fed to the brute-force counters in identity-service and to the
    Security Operations Console (SRS §1.2.6).
    """

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        BAD_CREDENTIAL = "bad_credential", "Bad Credential"
        ACCOUNT_LOCKED = "account_locked", "Account Locked"
        UNKNOWN_USER = "unknown_user", "Unknown User"
        MFA_FAIL = "mfa_fail", "MFA Failed"
        IP_THROTTLED = "ip_throttled", "IP Throttled"

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="login_attempts"
    )
    email_attempted = models.EmailField(blank=True)
    ip = models.GenericIPAddressField()
    user_agent = models.TextField(blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "users_loginattempt"
        indexes = [
            models.Index(fields=["ip", "occurred_at"]),
            models.Index(fields=["user", "occurred_at"]),
        ]


class Session(models.Model):
    """Active user session — backs refresh-token rotation and revocation.

    SRS §1.5.1 — session(id, user_id, issued_at, expires_at,
                         mfa_verified_at, revoked_at, ip).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="sessions")
    jti = models.CharField(max_length=64, unique=True, db_index=True)
    refresh_token_hash = models.CharField(max_length=128, db_index=True)
    issued_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    mfa_verified_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.CharField(max_length=100, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        db_table = "users_session"
        indexes = [
            models.Index(fields=["user", "revoked_at"]),
            models.Index(fields=["expires_at"]),
        ]

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at > timezone.now()

    def revoke(self, reason: str = "") -> None:
        self.revoked_at = timezone.now()
        self.revoke_reason = reason
        self.save(update_fields=["revoked_at", "revoke_reason"])


class MFAEnrolment(models.Model):
    """Per-factor MFA enrolment record.

    SRS §1.2.3 — TOTP (RFC 6238), WebAuthn / FIDO2, SMS fallback.
    `credential_ref` is the secret (TOTP) or the credential payload (WebAuthn);
    in production this column is encrypted at rest via shared.vault.
    """

    class FactorType(models.TextChoices):
        TOTP = "totp", "TOTP"
        WEBAUTHN = "webauthn", "WebAuthn / FIDO2"
        SMS = "sms", "SMS OTP"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="mfa_enrolments")
    factor_type = models.CharField(max_length=20, choices=FactorType.choices)
    label = models.CharField(max_length=100, blank=True)
    credential_ref = models.TextField()  # TOTP secret OR WebAuthn credential JSON
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_mfaenrolment"
        indexes = [models.Index(fields=["user", "factor_type"])]


class PasswordHistory(models.Model):
    """Last-N password hashes — SRS §1.2.3 blocks reuse of last 12."""
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="password_history")
    password_hash = models.CharField(max_length=255)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "users_passwordhistory"
        ordering = ["-created_at"]

