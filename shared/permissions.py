"""shared/permissions.py — DRF gateway. Thin wrapper over ``shared.rbac``.

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
    indicators = {
        "permission": codename,
        "roles": roles,
        "path": request.path,
        "method": request.method,
    }
    AuditEvent.record(
        actor_id=actor_id,
        action="AUTHZ_DENIED",
        entity_type="permission",
        new_state=indicators,
        ip_address=getattr(request, "ip_address", None),
        request_id=getattr(request, "request_id", None),
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
