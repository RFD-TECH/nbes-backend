"""DRF gateway. Thin wrapper over ``shared.rbac``.

Usage in views::

    from shared.permissions import HasPermission

    permission_classes = [IsAuthenticated, HasPermission("item:approve")]

Or the factory form (lets DRF instantiate without arguments)::

    permission_classes = [IsAuthenticated, has_permission("item:approve")]

Every denial emits an ``AUTHZ_DENIED`` AuditEvent per the NBES blueprint §4.
"""

import logging

from rest_framework.permissions import BasePermission

from shared import rbac

logger = logging.getLogger(__name__)


def _record_denial(request, codename: str) -> None:
    """Emit a 403 audit + security event. Imported lazily so this module
    stays usable during migrations and management commands where apps
    aren't loaded yet."""
    from apps.audit.models import AuditEvent

    payload = request.auth or {}
    actor_id = payload.get("sub") or None
    roles = rbac.get_nbes_role_names(payload)
    # Blueprint §1.2.5: every 403 must include user_agent .
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    indicators = {
        "permission": codename,
        "roles": roles,
        "path": request.path,
        "method": request.method,
        "user_agent": user_agent,
    }
    AuditEvent.record(
        actor_id=actor_id,
        action="AUTHZ_DENIED",
        entity_type="permission",
        new_state=indicators,
        ip_address=getattr(request, "ip_address", None),
        request_id=getattr(request, "request_id", None),
        user_agent=user_agent,
    )
    try:
        from shared.secops import record_security_event

        record_security_event(
            category="authz_denied",
            ip_address=getattr(request, "ip_address", None),
            actor_id=actor_id,
            request_id=getattr(request, "request_id", None),
            indicators=indicators,
        )
    except Exception:
        logger.exception("secops.record_security_event failed")

    # per-user 403 rate detection (§1.2.6).
    if actor_id:
        _check_user_denial_rate(actor_id, request)


_USER_DENIAL_WINDOW = 15 * 60  # 15 minutes in seconds
_USER_DENIAL_THRESHOLD = 50


def _check_user_denial_rate(actor_id: str, request) -> None:
    """Emit a SecurityEvent when one user generates >50 403s in 15 minutes."""
    try:
        from django.core.cache import cache

        key = f"nbes:denied:user:{actor_id}:15m"
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=_USER_DENIAL_WINDOW)
            count = 1
        if count == _USER_DENIAL_THRESHOLD:
            from shared.secops import record_security_event

            record_security_event(
                category="user_excessive_denials",
                actor_id=actor_id,
                ip_address=getattr(request, "ip_address", None),
                request_id=getattr(request, "request_id", None),
                indicators={
                    "count": count,
                    "window_seconds": _USER_DENIAL_WINDOW,
                    "actor_id": actor_id,
                },
            )
    except Exception:
        logger.exception("user_denial_rate_check failed")


class HasPermission(BasePermission):
    """Checks that the JWT carries a role granting ``permission`` in NBES."""

    def __init__(self, permission: str):
        self.permission = permission

    def has_permission(self, request, view):
        if not request.auth:
            return False
        if rbac.has_permission(request.auth, self.permission):
            return True
        _record_denial(request, self.permission)
        return False

    # Lets DRF call HasPermission(...) and then HasPermission(...)() — no-op.
    def __call__(self):
        return self


def has_permission(permission: str):
    """Factory: returns a no-arg class suitable for ``permission_classes``."""

    class _Permission(HasPermission):
        def __init__(self):
            super().__init__(permission)

    _Permission.__name__ = f"HasPermission_{permission.replace(':', '_')}"
    return _Permission


class HasPermissionWithStepUp(HasPermission):
    """Checks that the JWT carries a role granting ``permission`` in NBES,
    AND if that permission requires step-up, verifies the step-up headers.
    """

    def has_permission(self, request, view):
        # 1. Base RBAC check
        if not super().has_permission(request, view):
            return False

        # 2. Check if this codename requires step-up
        from shared.step_up import (
            requires_step_up,
            check_step_up,
            _record_step_up_denial,
        )

        if requires_step_up(self.permission):
            if not check_step_up(request):
                _record_step_up_denial(request, self.permission)
                return False

        return True


def has_permission_with_step_up(permission: str):
    """Factory: returns a no-arg class suitable for ``permission_classes``."""

    class _Permission(HasPermissionWithStepUp):
        def __init__(self):
            super().__init__(permission)

    _Permission.__name__ = f"HasPermissionWithStepUp_{permission.replace(':', '_')}"
    return _Permission


# ── Machine-token (service-to-service) guards ───────────────────────


def _is_service_account(request) -> bool:
    """Return True when the authenticated principal is a Keycloak service account."""
    user = getattr(request, "user", None)
    if user is None:
        return False
    metadata = getattr(user, "metadata", None) or {}
    if metadata.get("is_service_account"):
        return True
    # Also detect from the raw JWT payload (populated by auth.py)
    payload = request.auth or {}
    preferred = payload.get("preferred_username", "")
    return preferred.startswith("service-account-")


class ServiceAccountOnly(BasePermission):
    """Allows only machine/service-account tokens.

    Use on endpoints that should only be called by trusted internal services
    (e.g. System 10B pushing response data into NBES).
    """

    message = "This endpoint is reserved for service-to-service calls."

    def has_permission(self, request, _view):
        if not request.auth:
            return False
        return _is_service_account(request)


class HumanUserOnly(BasePermission):
    """Blocks service-account tokens — for endpoints that only humans should call."""

    message = "Service accounts are not permitted to call this endpoint."

    def has_permission(self, request, _view):
        if not request.auth:
            return False
        return not _is_service_account(request)
