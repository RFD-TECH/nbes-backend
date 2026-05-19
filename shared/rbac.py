"""shared/rbac.py — NBES authorization resolver.

Single entry point: ``has_permission(jwt_payload, codename)``.
Resolution order:
1. Extract the NBES-scoped role names this user holds (see
   ``get_nbes_role_names`` below — swappable when IAM ships the
   ``system_roles`` JWT claim).
2. Intersect with NBES's local Role registry — JWT roles NBES does not
   recognise are ignored.
3. Resolve the union of granted codenames via ``RolePermission`` rows.
4. Result cached per role for ``CACHE_TTL`` (60 s, REQ-F000-02).
The role -> permissions cache is invalidated by ``invalidate_role(name)``,
called from the admin endpoints that mutate ``RolePermission`` rows.
"""
from __future__ import annotations

from django.core.cache import cache


CACHE_TTL = 60
_CACHE_PREFIX = "nbes:rbac:role:"


def get_nbes_role_names(jwt_payload: dict) -> list[str]:
    """Return the role names this user holds *in NBES*.

    Today: reads ``realm_access.roles`` from the JWT. NBES later filters
    this list against its own ``Role`` table so unknown roles are ignored.

    When IAM ships the ``system_roles`` custom claim, replace the body with
    ``return jwt_payload.get("system_roles", {}).get("NBES", [])`` — no
    callers change.
    """
    if not jwt_payload:
        return []
    roles = jwt_payload.get("realm_access", {}).get("roles") or []
    if not isinstance(roles, list):
        return []
    return [r for r in roles if isinstance(r, str)]


def _permissions_for_role(role_name: str) -> set[str]:
    cached = cache.get(_CACHE_PREFIX + role_name)
    if cached is not None:
        return set(cached)

    # Local import: avoids circular import at module load.
    from apps.users.models import RolePermission

    codenames = set(
        RolePermission.objects
        .filter(role__name=role_name, role__is_active=True)
        .values_list("permission__codename", flat=True)
    )
    cache.set(_CACHE_PREFIX + role_name, list(codenames), CACHE_TTL)
    return codenames


def permissions_for(jwt_payload: dict) -> set[str]:
    """All codenames this user holds in NBES, resolved from the JWT."""
    role_names = get_nbes_role_names(jwt_payload)
    if not role_names:
        return set()

    from apps.users.models import Role

    known = set(
        Role.objects
        .filter(name__in=role_names, is_active=True)
        .values_list("name", flat=True)
    )
    if not known:
        return set()

    permissions: set[str] = set()
    for role_name in known:
        permissions |= _permissions_for_role(role_name)
    return permissions


def has_permission(jwt_payload: dict, codename: str) -> bool:
    return codename in permissions_for(jwt_payload)


def invalidate_role(role_name: str) -> None:
    """Drop the cached permission set for one role. Call after editing
    ``RolePermission`` rows so changes propagate within the cache window."""
    cache.delete(_CACHE_PREFIX + role_name)
