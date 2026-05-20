"""shared/keycloak_admin.py — Keycloak Admin API client for NBES.

Used to revoke Keycloak roles from NBEC members when their tenure expires,
ensuring IAM access is removed in lockstep with the local DB status change.

Only makes live HTTP calls when KEYCLOAK_ENABLED=True. In dev mode every
call is a no-op so local environments need no running Keycloak.
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_ADMIN_TOKEN_CACHE_KEY = "nbes:keycloak:admin_token"
_ADMIN_TOKEN_TTL_BUFFER = 15  # seconds to subtract from expires_in as safety margin


def _realm_base() -> tuple[str, str]:
    """Parse (base_url, realm_name) out of KEYCLOAK_REALM_URL.

    e.g. "http://keycloak:8080/realms/clet-internal"
         → ("http://keycloak:8080", "clet-internal")
    """
    realm_url = (getattr(settings, "KEYCLOAK_REALM_URL", "") or "").rstrip("/")
    if "/realms/" not in realm_url:
        raise ValueError(
            f"Cannot parse realm from KEYCLOAK_REALM_URL={realm_url!r}. "
            "Expected format: http://<host>/realms/<realm-name>"
        )
    base, realm = realm_url.rsplit("/realms/", 1)
    return base.rstrip("/"), realm


def _get_admin_token() -> str:
    cached = cache.get(_ADMIN_TOKEN_CACHE_KEY)
    if cached:
        return cached

    base_url, realm = _realm_base()
    client_id = getattr(settings, "KEYCLOAK_ADMIN_CLIENT_ID", "")
    client_secret = getattr(settings, "KEYCLOAK_ADMIN_CLIENT_SECRET", "")

    resp = requests.post(
        f"{base_url}/realms/{realm}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]
    expires_in = int(body.get("expires_in", 60))
    ttl = max(expires_in - _ADMIN_TOKEN_TTL_BUFFER, 1)
    cache.set(_ADMIN_TOKEN_CACHE_KEY, token, ttl)
    return token


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_admin_token()}",
        "Content-Type": "application/json",
    }


def revoke_realm_role(user_sub: str, role_name: str) -> None:
    """Remove a realm-level role from a Keycloak user identified by their sub UUID.

    Safe to call regardless of KEYCLOAK_ENABLED — in dev mode it logs and
    returns without making any HTTP request.

    Raises requests.HTTPError on unexpected Keycloak errors so the caller can
    decide whether to retry or swallow.
    """
    if not getattr(settings, "KEYCLOAK_ENABLED", False):
        logger.info(
            "keycloak_admin.revoke_realm_role skipped (dev mode): user=%s role=%s",
            user_sub,
            role_name,
        )
        return

    base_url, realm = _realm_base()
    admin_base = f"{base_url}/admin/realms/{realm}"

    # Step 1 — fetch the role representation (id + name required by Keycloak)
    role_resp = requests.get(
        f"{admin_base}/roles/{role_name}",
        headers=_auth_headers(),
        timeout=10,
    )
    if role_resp.status_code == 404:
        logger.warning(
            "keycloak_admin: realm role %r not found — nothing to revoke for user %s",
            role_name,
            user_sub,
        )
        return
    role_resp.raise_for_status()
    role_repr = role_resp.json()

    # Step 2 — delete the role mapping from the user
    del_resp = requests.delete(
        f"{admin_base}/users/{user_sub}/role-mappings/realm",
        json=[role_repr],
        headers=_auth_headers(),
        timeout=10,
    )
    if del_resp.status_code == 404:
        logger.warning(
            "keycloak_admin: user %s not found in Keycloak — skipping role revoke",
            user_sub,
        )
        return
    del_resp.raise_for_status()
    logger.info(
        "keycloak_admin: removed realm role %r from user %s",
        role_name,
        user_sub,
    )
