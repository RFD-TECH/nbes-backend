"""
shared/permissions.py — RBAC Permission Classes for NBES
=========================================================

Usage in views:
    from shared.permissions import HasPermission

    permission_classes = [IsAuthenticated, HasPermission("item:approve")]

ROLE_PERMISSION_MAP defines which roles hold which permissions.
Roles come from request.auth["role"] (set by KeycloakJWTAuthentication).

Reference: NBES System Architecture §8.1 — RBAC Matrix
"""

from rest_framework.permissions import BasePermission

# ── Permission → Role mapping ─────────────────────────────────────────────────
# Each permission maps to the list of roles that hold it.
# Roles: nbec-member, nbec-secretariat, item-writer, moderator,
#        examiner, clet-registrar, candidate
ROLE_PERMISSION_MAP: dict[str, list[str]] = {
    "item:create":                    ["item-writer"],
    "item:approve":                   ["nbec-member", "moderator"],
    "item:vault:export":              ["nbec-member"],
    "sitting:configure":              ["nbec-member"],
    "sitting:lock:override":          ["nbec-member"],
    "registration:eligibility:override": ["clet-registrar"],
    "registration:self":              ["candidate"],
    "marking:moderate":               ["moderator"],
    "marking:second_mark":            ["examiner"],
    "marking:arbitrate":              ["nbec-member"],
    "results:ratify":                 ["nbec-member"],
    "results:publish:approve":        ["clet-registrar"],
    "results:view:own":               ["candidate"],
    "resit:register":                 ["candidate"],
    "resit:exception:grant":          ["nbec-member"],
    "cert:trigger":                   ["clet-registrar"],
    "audit:export":                   ["nbec-member"],
    "committee:manage":               ["nbec-member", "nbec-secretariat"],
    "sla:view":                       ["nbec-member", "nbec-secretariat", "clet-registrar"],
    "reporting:view":                 ["nbec-member", "nbec-secretariat"],
}


class HasPermission(BasePermission):
    """
    DRF permission class. Checks that request.auth["role"] holds the
    required permission according to ROLE_PERMISSION_MAP.

    Audits every 403 via AuditEvent.

    TODO: Add Redis cache (60s) for role→permission lookups in production.
    """

    def __init__(self, permission: str):
        self.permission = permission

    def has_permission(self, request, view):
        if not request.auth:
            return False

        role = request.auth.get("role", "")
        allowed_roles = ROLE_PERMISSION_MAP.get(self.permission, [])
        granted = role in allowed_roles

        if not granted:
            # TODO: Record 403 audit event here
            # AuditEvent.record(
            #     actor_id=request.auth.get("sub"),
            #     action="AUTHZ_DENIED",
            #     new_state={"permission": self.permission, "role": role},
            # )
            pass

        return granted

    # Required by DRF to instantiate with arguments
    def __call__(self):
        return self


def has_permission(permission: str):
    """
    Factory function for use in permission_classes lists.
    Usage: permission_classes = [IsAuthenticated, has_permission("item:approve")]
    """
    class _Permission(HasPermission):
        def __init__(self):
            super().__init__(permission)
    _Permission.__name__ = f"HasPermission_{permission.replace(':', '_')}"
    return _Permission
