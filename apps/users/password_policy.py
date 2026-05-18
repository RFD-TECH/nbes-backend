"""apps/users/password_policy.py — SRS §1.2.3 password policy.

Rules:
- Minimum 12 characters
- Mixed case
- At least one digit
- At least one special character
- Not in HaveIBeenPwned breached-password corpus (k-anonymity SHA-1 prefix)
- Not in the user's last 12 password hashes
"""
import hashlib
import re

import requests
from django.contrib.auth.hashers import check_password
from django.core.exceptions import ValidationError


PASSWORD_MIN_LENGTH = 12
PASSWORD_HISTORY_DEPTH = 12
HIBP_API_URL = "https://api.pwnedpasswords.com/range/{prefix}"
HIBP_TIMEOUT_SECONDS = 3


def _has_upper(s: str) -> bool:
    return any(c.isupper() for c in s)


def _has_lower(s: str) -> bool:
    return any(c.islower() for c in s)


def _has_digit(s: str) -> bool:
    return any(c.isdigit() for c in s)


def _has_special(s: str) -> bool:
    return bool(re.search(r"[^A-Za-z0-9]", s))


def validate_password_complexity(raw_password: str) -> None:
    """Raise ValidationError if the password fails the policy. Local checks only."""
    errors: list[str] = []
    if len(raw_password) < PASSWORD_MIN_LENGTH:
        errors.append(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if not _has_upper(raw_password):
        errors.append("Password must contain at least one uppercase letter.")
    if not _has_lower(raw_password):
        errors.append("Password must contain at least one lowercase letter.")
    if not _has_digit(raw_password):
        errors.append("Password must contain at least one digit.")
    if not _has_special(raw_password):
        errors.append("Password must contain at least one special character.")
    if errors:
        raise ValidationError(errors, code="password_policy")


def check_hibp_breach(raw_password: str) -> bool:
    """Return True if the password appears in the HIBP breach corpus.

    Uses k-anonymity: only the first 5 hex chars of the SHA-1 hash are sent.
    Network failures fail open (return False) — we never block a login just
    because HIBP is unreachable; treat that as a separate operational concern.
    """
    sha1 = hashlib.sha1(raw_password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]
    try:
        resp = requests.get(
            HIBP_API_URL.format(prefix=prefix),
            timeout=HIBP_TIMEOUT_SECONDS,
            headers={"Add-Padding": "true"},
        )
        if resp.status_code != 200:
            return False
        for line in resp.text.splitlines():
            hash_suffix, _, _ = line.partition(":")
            if hash_suffix.strip().upper() == suffix:
                return True
        return False
    except requests.RequestException:
        return False


def validate_password_not_breached(raw_password: str) -> None:
    if check_hibp_breach(raw_password):
        raise ValidationError(
            "This password appears in a known data breach. Choose a different one.",
            code="password_breached",
        )


def validate_password_not_reused(user, raw_password: str) -> None:
    """Block reuse against the user's last PASSWORD_HISTORY_DEPTH hashes."""
    if user is None or user.pk is None:
        return
    recent = user.password_history.order_by("-created_at")[:PASSWORD_HISTORY_DEPTH]
    for entry in recent:
        if check_password(raw_password, entry.password_hash):
            raise ValidationError(
                f"This password matches one of your last {PASSWORD_HISTORY_DEPTH} passwords.",
                code="password_reused",
            )


def validate_password(user, raw_password: str) -> None:
    """Run the full policy. Call from serializers / services before set_password."""
    validate_password_complexity(raw_password)
    validate_password_not_breached(raw_password)
    validate_password_not_reused(user, raw_password)
