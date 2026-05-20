"""shared/rbac.py — NBES authorization resolver.

Single entry point: ``has_permission(jwt_payload, codename)``.

Resolution order:

1. Extract the NBES-scoped role names this user holds. The preferred source
   is ``resource_access[<NBES_CLIENT_ID>].roles`` (Keycloak client roles —
   the target architecture). During the IAM migration the resolver falls
   back to ``realm_access.roles`` and logs a structured warning so usage
   of the legacy path is observable.
2. ``super_admin`` in ``realm_access.roles`` always short-circuits to the
   wildcard. This is the only realm role NBES honours post-migration.
3. Intersect with NBES's local ``Role`` registry — JWT roles NBES does not
   recognise are ignored.
4. Resolve the union of granted codenames via ``RolePermission`` rows.
5. Result cached per role for ``CACHE_TTL`` (60 s, REQ-F000-02).

The role -> permissions cache is invalidated by ``invalidate_role(name)``,
called from the admin endpoints that mutate ``RolePermission`` rows.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache


logger = logging.getLogger(__name__)

CACHE_TTL = 60
_CACHE_PREFIX = "nbes:rbac:role:"

# IAM platform role that NBES treats as a full-access wildcard, mirroring
# IAM's own ROLE_PERMISSION_MAP["super_admin"] = ["*"]. Bearer is granted
# every permission NBES enforces, without needing a local Role row.
SUPER_ADMIN_ROLE = "super_admin"
WILDCARD = "*"


def _nbes_client_id() -> str:
    return getattr(settings, "NBES_CLIENT_ID", "nbes-api")


def get_nbes_role_names(jwt_payload: dict) -> list[str]:
    """Return the role names this user holds *in NBES*.

    Preferred source: ``resource_access[<NBES_CLIENT_ID>].roles`` (Keycloak
    client roles). If absent or empty, falls back to ``realm_access.roles``
    and logs a warning so the migration completion can be tracked.
    """
    if not jwt_payload:
        return []

    client_id = _nbes_client_id()
    resource_roles = (
        jwt_payload.get("resource_access", {})
        .get(client_id, {})
        .get("roles")
    )
    if isinstance(resource_roles, list) and resource_roles:
        return [r for r in resource_roles if isinstance(r, str)]

    realm_roles = jwt_payload.get("realm_access", {}).get("roles") or []
    if not isinstance(realm_roles, list):
        return []
    string_roles = [r for r in realm_roles if isinstance(r, str)]
    if string_roles:
        logger.warning(
            "rbac.legacy_realm_role_fallback",
            extra={
                "sub": jwt_payload.get("sub", ""),
                "nbes_client_id": client_id,
                "realm_role_count": len(string_roles),
            },
        )
    return string_roles


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


def _has_super_admin(jwt_payload: dict) -> bool:
    """``super_admin`` is a *realm role* in the target architecture.
    Check ``realm_access.roles`` directly so the wildcard works regardless
    of whether the token also carries ``resource_access`` entries."""
    realm_roles = (jwt_payload or {}).get("realm_access", {}).get("roles") or []
    return isinstance(realm_roles, list) and SUPER_ADMIN_ROLE in realm_roles


def permissions_for(jwt_payload: dict) -> set[str]:
    """All codenames this user holds in NBES, resolved from the JWT.

    IAM ``super_admin`` (a realm role) short-circuits to the wildcard
    sentinel — bearer is granted every NBES permission without needing a
    local Role row.
    """
    if _has_super_admin(jwt_payload):
        return {WILDCARD}

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
    granted = permissions_for(jwt_payload)
    return WILDCARD in granted or codename in granted


def invalidate_role(role_name: str) -> None:
    """Drop the cached permission set for one role. Call after editing
    ``RolePermission`` rows so changes propagate within the cache window."""
    cache.delete(_CACHE_PREFIX + role_name)
