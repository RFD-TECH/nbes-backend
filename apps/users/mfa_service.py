"""apps/users/mfa_service.py — TOTP and WebAuthn enrolment.

SRS §1.2.3:
- TOTP (RFC 6238) — primary fallback factor.
- WebAuthn / FIDO2 — preferred for internal roles.
- SMS — fallback only; not implemented in Sprint 1.1.
"""
import json
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.users.models import MFAEnrolment, UserProfile


# ── TOTP ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TOTPEnrolmentChallenge:
    enrolment_id: str
    secret: str
    provisioning_uri: str  # otpauth:// URL — caller renders this as a QR code.


def start_totp_enrolment(user: UserProfile, *, label: str = "") -> TOTPEnrolmentChallenge:
    """Generate an unconfirmed TOTP enrolment. User must call confirm_totp_enrolment
    with a fresh code from their authenticator app to activate it.
    """
    import pyotp

    secret = pyotp.random_base32()
    enrolment = MFAEnrolment.objects.create(
        user=user,
        factor_type=MFAEnrolment.FactorType.TOTP,
        label=label or "Authenticator app",
        credential_ref=secret,
    )
    provisioning_uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=user.email,
        issuer_name="NBES",
    )
    AuditEvent.record(
        actor_id=user.id,
        action="MFA_TOTP_ENROL_STARTED",
        entity_type="user",
        entity_id=user.id,
    )
    return TOTPEnrolmentChallenge(
        enrolment_id=str(enrolment.id),
        secret=secret,
        provisioning_uri=provisioning_uri,
    )


def confirm_totp_enrolment(user: UserProfile, *, enrolment_id: str, code: str) -> MFAEnrolment:
    """Verify a TOTP code against an unconfirmed enrolment and activate it."""
    import pyotp

    try:
        enrolment = MFAEnrolment.objects.get(
            id=enrolment_id,
            user=user,
            factor_type=MFAEnrolment.FactorType.TOTP,
        )
    except MFAEnrolment.DoesNotExist:
        raise ValueError("Enrolment not found.")

    if enrolment.confirmed_at is not None:
        raise ValueError("Enrolment is already confirmed.")

    totp = pyotp.TOTP(enrolment.credential_ref)
    if not totp.verify(code, valid_window=1):
        raise ValueError("Invalid code.")

    enrolment.confirmed_at = timezone.now()
    enrolment.last_used_at = timezone.now()
    enrolment.save(update_fields=["confirmed_at", "last_used_at"])

    if not user.mfa_enrolled:
        user.mfa_enrolled = True
        user.save(update_fields=["mfa_enrolled", "updated_at"])

    AuditEvent.record(
        actor_id=user.id,
        action="MFA_TOTP_ENROL_CONFIRMED",
        entity_type="user",
        entity_id=user.id,
        new_state={"factor": "totp"},
    )
    return enrolment


# ── WebAuthn (FIDO2) ─────────────────────────────────────────────────────────
# Uses py_webauthn — the registration/authentication ceremony is two-step
# (begin → finish) per the WebAuthn spec.

def _rp_id() -> str:
    return getattr(settings, "WEBAUTHN_RP_ID", "localhost")


def _rp_name() -> str:
    return getattr(settings, "WEBAUTHN_RP_NAME", "NBES")


def begin_webauthn_registration(user: UserProfile) -> dict:
    """Step 1 — return PublicKeyCredentialCreationOptions for the browser API."""
    from webauthn import generate_registration_options
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        UserVerificationRequirement,
    )

    existing = list(
        user.mfa_enrolments.filter(
            factor_type=MFAEnrolment.FactorType.WEBAUTHN,
            confirmed_at__isnull=False,
        ).values_list("credential_ref", flat=True)
    )
    exclude_creds = []
    for ref in existing:
        try:
            data = json.loads(ref)
            exclude_creds.append({"id": data["credential_id"], "type": "public-key"})
        except (json.JSONDecodeError, KeyError):
            continue

    options = generate_registration_options(
        rp_id=_rp_id(),
        rp_name=_rp_name(),
        user_id=str(user.id).encode("utf-8"),
        user_name=user.email,
        user_display_name=user.full_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    # Persist the challenge as an unconfirmed enrolment row.
    MFAEnrolment.objects.create(
        user=user,
        factor_type=MFAEnrolment.FactorType.WEBAUTHN,
        label="WebAuthn (pending)",
        credential_ref=json.dumps({"challenge": options.challenge.hex()}),
    )

    from webauthn.helpers import options_to_json
    return json.loads(options_to_json(options))


def finish_webauthn_registration(user: UserProfile, *, credential_payload: dict) -> MFAEnrolment:
    """Step 2 — verify the attestation and persist the credential.

    `credential_payload` is the PublicKeyCredential JSON returned by
    navigator.credentials.create() on the browser.
    """
    from webauthn import verify_registration_response

    pending = (
        user.mfa_enrolments
        .filter(factor_type=MFAEnrolment.FactorType.WEBAUTHN, confirmed_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if pending is None:
        raise ValueError("No pending WebAuthn registration.")

    pending_data = json.loads(pending.credential_ref)
    expected_challenge = bytes.fromhex(pending_data["challenge"])

    verification = verify_registration_response(
        credential=credential_payload,
        expected_challenge=expected_challenge,
        expected_rp_id=_rp_id(),
        expected_origin=getattr(settings, "WEBAUTHN_ORIGIN", "http://localhost:8000"),
    )

    pending.credential_ref = json.dumps({
        "credential_id": verification.credential_id.hex(),
        "public_key": verification.credential_public_key.hex(),
        "sign_count": verification.sign_count,
    })
    pending.confirmed_at = timezone.now()
    pending.label = credential_payload.get("clientExtensionResults", {}).get("label", "Security key")
    pending.save(update_fields=["credential_ref", "confirmed_at", "label"])

    if not user.mfa_enrolled:
        user.mfa_enrolled = True
        user.save(update_fields=["mfa_enrolled", "updated_at"])

    AuditEvent.record(
        actor_id=user.id,
        action="MFA_WEBAUTHN_ENROL_CONFIRMED",
        entity_type="user",
        entity_id=user.id,
    )
    return pending
