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
from django.db import models


logger = logging.getLogger(__name__)

CACHE_TTL = 60
_CACHE_PREFIX = "nbes:rbac:role:"
_USER_CACHE_PREFIX = "nbes:rbac:user:"

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
        jwt_payload.get("resource_access", {}).get(client_id, {}).get("roles")
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


def _user_db_roles(sub: str) -> set[str]:
    """Return role names the user currently holds in the UserRole DB table.

    This is the authoritative source — it reflects revocations that have
    already happened in NBES even if the JWT hasn't expired yet (up to 8h
    token lifetime). Results cached per user with CACHE_TTL (60 s).

    Raises on DB error so callers can fall back to JWT roles explicitly.
    """
    cached = cache.get(_USER_CACHE_PREFIX + sub)
    if cached is not None:
        return set(cached)

    from django.utils import timezone as tz
    from apps.users.models import UserRole

    today = tz.now().date()
    role_names = set(
        UserRole.objects.filter(
            user__keycloak_sub=sub,
            revoked_at__isnull=True,
            effective_from__lte=today,
        )
        .filter(
            models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=today)
        )
        .values_list("role__name", flat=True)
    )

    cache.set(_USER_CACHE_PREFIX + sub, list(role_names), CACHE_TTL)
    return role_names


def _permissions_for_role(role_name: str) -> set[str]:
    cached = cache.get(_CACHE_PREFIX + role_name)
    if cached is not None:
        return set(cached)

    # Local import: avoids circular import at module load.
    from apps.users.models import RolePermission

    codenames = set(
        RolePermission.objects.filter(
            role__name=role_name, role__is_active=True
        ).values_list("permission__codename", flat=True)
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
    """All codenames this user holds in NBES.

    Resolution order (§1.2.2):
    1. ``super_admin`` realm role → wildcard, bypasses everything.
    2. UserRole DB table (authoritative — reflects revocations within 60 s
       even while the JWT is still alive, satisfying REQ-F000-02).
    3. Falls back to JWT role claims only when the DB is unreachable.
    """
    if _has_super_admin(jwt_payload):
        return {WILDCARD}

    jwt_roles = set(get_nbes_role_names(jwt_payload))
    sub = (jwt_payload or {}).get("sub", "")

    db_roles: set[str] | None = None
    if sub:
        try:
            db_roles = _user_db_roles(sub)
        except Exception:
            logger.exception(
                "rbac.permissions_for: DB lookup failed for sub=%s — falling back to JWT", sub
            )

    all_roles = db_roles if db_roles is not None else jwt_roles
    if not all_roles:
        return set()

    from apps.users.models import Role

    known = set(
        Role.objects.filter(name__in=all_roles, is_active=True).values_list(
            "name", flat=True
        )
    )
    if not known:
        return set()

    permissions: set[str] = set()
    for role_name in known:
        permissions |= _permissions_for_role(role_name)
    return permissions


def has_permission(jwt_payload: dict, codename: str) -> bool:
    """Return True if the bearer holds ``codename`` in NBES."""
    granted = permissions_for(jwt_payload)
    return WILDCARD in granted or codename in granted


def invalidate_role(role_name: str) -> None:
    """Drop the cached permission set for one role. Call after editing
    ``RolePermission`` rows so changes propagate within the cache window."""
    cache.delete(_CACHE_PREFIX + role_name)


def invalidate_user(sub: str) -> None:
    """Drop the per-user DB-roles cache for one user.

    Call after assigning or revoking a UserRole so the change takes effect
    within CACHE_TTL (60 s) rather than waiting for the JWT to expire.
    """
    cache.delete(_USER_CACHE_PREFIX + sub)
