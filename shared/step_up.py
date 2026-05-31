"""MFA & Step-Up Policy enforcement.

Enforces that high-stakes actions require a verified step-up session,
validated via X-Mfa-Verified or X-Acr headers injected by the API Gateway.
"""

from __future__ import annotations

import logging
from django.conf import settings
from rest_framework.permissions import BasePermission

logger = logging.getLogger(__name__)

# Canonical list of high-stakes permission codenames requiring step-up.
# Declared in code per blueprint §1.8.
STEP_UP_ACTIONS: frozenset[str] = frozenset(
    {
        # Vault & item security
        "vault:operate",  # vault export initiation + dual-control cosign
        "item:vault:export",  # direct vault export permission (future)
        # Examination management overrides
        "sitting:lock:override",
        "registration:eligibility:override",
        "resit:exception:grant",
        # Role & privilege administration
        "users:manage",  # any role assignment / revocation
        "rbac:manage",  # exclusion rules, approval actions
        # Results lifecycle
        "results:publish",
        "results:publish:approve",
        "results:ratify",
        # Certification trigger (blueprint §1.8 — high-stakes)
        "cert:trigger",
        # Candidate high-stakes
        "results:view:own",  # candidate self-service result view
        "candidate:register",  # registration (locked-field profile)
        # Committee / board ratification
        "committee:approve",
        "committee:chair",
        "committee:manage",
        # Audit export (non-repudiation chain)
        "audit:export",
    }
)


def requires_step_up(codename: str) -> bool:
    """Check if the given permission codename requires MFA/step-up."""
    return codename in STEP_UP_ACTIONS


def check_step_up(request) -> bool:
    """Read step-up headers from request.

    Returns True if:
    - KEYCLOAK_ENABLED is False (dev mode bypass).
    - X-Mfa-Verified: true (gateway convenience header).
    - X-Acr >= settings.STEP_UP_MIN_ACR_LEVEL (default 2).
    """
    if not getattr(settings, "KEYCLOAK_ENABLED", False):
        logger.debug("Step-up check bypassed because KEYCLOAK_ENABLED=False")
        return True

    mfa_header_key = getattr(settings, "STEP_UP_HEADER_MFA", "HTTP_X_MFA_VERIFIED")
    acr_header_key = getattr(settings, "STEP_UP_HEADER_ACR", "HTTP_X_ACR")
    min_acr = getattr(settings, "STEP_UP_MIN_ACR_LEVEL", 2)

    mfa_verified = request.META.get(mfa_header_key, "")
    if mfa_verified.lower() == "true":
        return True

    acr = request.META.get(acr_header_key, "")
    if acr:
        try:
            if int(acr) >= min_acr:
                return True
        except ValueError:
            logger.warning(
                "invalid_acr_header_value",
                extra={
                    "acr_value": acr,
                    "request_id": getattr(request, "request_id", None),
                },
            )

    return False


def _record_step_up_denial(request, codename: str) -> None:
    """Emit a STEP_UP_REQUIRED AuditEvent and step_up_denied SecurityEvent."""
    from apps.audit.models import AuditEvent
    from shared import rbac

    payload = request.auth or {}
    actor_id = payload.get("sub") or None
    roles = rbac.get_nbes_role_names(payload)
    indicators = {
        "permission": codename,
        "roles": roles,
        "path": request.path,
        "method": request.method,
        "step_up_required": True,
    }

    AuditEvent.record(
        actor_id=actor_id,
        action="STEP_UP_REQUIRED",
        entity_type="permission",
        new_state=indicators,
        ip_address=getattr(request, "ip_address", None),
        request_id=getattr(request, "request_id", None),
    )

    try:
        from shared.secops import record_security_event

        record_security_event(
            category="step_up_denied",
            ip_address=getattr(request, "ip_address", None),
            actor_id=actor_id,
            request_id=getattr(request, "request_id", None),
            indicators=indicators,
        )
    except Exception:
        logger.exception("secops.record_security_event failed for step_up_denied")


class HasStepUp(BasePermission):
    """Requires an MFA step-up signal on high-stakes endpoints."""

    message = "Step-up authentication required."

    def has_permission(self, request, view):
        # We need the permission codename to log it correctly.
        # This class can be used directly if a view has its own way of determining
        # high stakes, but normally has_permission_with_step_up is preferred.
        codename = getattr(view, "step_up_codename", "unknown")
        if not check_step_up(request):
            _record_step_up_denial(request, codename)
            return False
        return True
