"""Keycloak Admin API client for NBES.

Used to provision users, deactivate accounts, and sync roles in Keycloak/IAM
in lockstep with the local DB status change.

Only makes live HTTP calls when KEYCLOAK_ENABLED=True. In dev mode every
call is a no-op so local environments need no running Keycloak.
"""

from __future__ import annotations

import logging
import uuid
import time
import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_ADMIN_TOKEN_CACHE_KEY = "nbes:keycloak:admin_token"
_ADMIN_TOKEN_TTL_BUFFER = 15  # seconds to subtract from expires_in as safety margin


class IntegrationError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


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


def _get_client_uuid(client_id: str) -> str:
    cache_key = f"nbes:keycloak:client_uuid:{client_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    base_url, realm = _realm_base()
    resp = requests.get(
        f"{base_url}/admin/realms/{realm}/clients?clientId={client_id}",
        headers=_auth_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    clients = resp.json()
    if not clients:
        raise ValueError(f"Client {client_id} not found in Keycloak realm {realm}")
    client_uuid = clients[0]["id"]
    cache.set(cache_key, client_uuid, 86400)  # cache for 24 hours
    return client_uuid


def _nbes_client_id() -> str:
    return getattr(settings, "NBES_CLIENT_ID", "nbes-api")


def _execute_with_retry(fn, *args, **kwargs):
    retries = 3
    delay = 1
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 500
            if 500 <= status_code < 600:
                if attempt == retries - 1:
                    raise IntegrationError(str(exc), retryable=True) from exc
                time.sleep(delay)
                delay *= 2
            else:
                raise IntegrationError(str(exc), retryable=False) from exc
        except requests.exceptions.RequestException as exc:
            if attempt == retries - 1:
                raise IntegrationError(str(exc), retryable=True) from exc
            time.sleep(delay)
            delay *= 2


def create_user(
    email: str,
    first_name: str,
    last_name: str,
    roles: list[str],
    *,
    send_invite: bool = True,
) -> str:
    """Provision user in Keycloak/IAM. Returns the IAM user UUID (sub)."""
    if not getattr(settings, "KEYCLOAK_ENABLED", False):
        logger.info(
            "keycloak_admin.create_user skipped (dev mode): email=%s first=%s last=%s roles=%s",
            email,
            first_name,
            last_name,
            roles,
        )
        return str(uuid.uuid4())

    base_url, realm = _realm_base()
    admin_base = f"{base_url}/admin/realms/{realm}"

    # Step 1 — Create user
    user_payload = {
        "username": email,
        "email": email,
        "firstName": first_name,
        "lastName": last_name,
        "enabled": True,
        "emailVerified": False,
        "requiredActions": ["VERIFY_EMAIL", "UPDATE_PASSWORD"],
    }

    def _create():
        resp = requests.post(
            f"{admin_base}/users",
            json=user_payload,
            headers=_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp

    resp = _execute_with_retry(_create)
    location = resp.headers.get("Location")
    if not location:
        raise IntegrationError("Keycloak response missing Location header", retryable=False)
    user_uuid = location.rstrip("/").split("/")[-1]

    try:
        # Step 2 — Assign roles if any
        if roles:
            client_id = _nbes_client_id()
            client_uuid = _get_client_uuid(client_id)
            role_reprs = []
            for role_name in roles:
                try:
                    role_resp = requests.get(
                        f"{admin_base}/clients/{client_uuid}/roles/{role_name}",
                        headers=_auth_headers(),
                        timeout=10,
                    )
                    role_resp.raise_for_status()
                    role_reprs.append(role_resp.json())
                except requests.exceptions.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code == 404:
                        logger.warning("Keycloak client role %s not found", role_name)
                    else:
                        raise

            if role_reprs:

                def _assign():
                    m_resp = requests.post(
                        f"{admin_base}/users/{user_uuid}/role-mappings/clients/{client_uuid}",
                        json=role_reprs,
                        headers=_auth_headers(),
                        timeout=10,
                    )
                    m_resp.raise_for_status()

                _execute_with_retry(_assign)

        # Step 3 — Send actions email if requested
        if send_invite:

            def _invite():
                inv_resp = requests.put(
                    f"{admin_base}/users/{user_uuid}/execute-actions-email",
                    json=["VERIFY_EMAIL", "UPDATE_PASSWORD"],
                    headers=_auth_headers(),
                    timeout=10,
                )
                inv_resp.raise_for_status()

            _execute_with_retry(_invite)

    except Exception as e:
        # Compensating rollback: delete the partially-provisioned Keycloak user
        # so NBES and Keycloak don't diverge. Only attempt retryable errors.
        if not isinstance(e, IntegrationError) or not getattr(e, "retryable", True):
            raise

        def _delete_user():
            del_resp = requests.delete(
                f"{admin_base}/users/{user_uuid}",
                headers=_auth_headers(),
                timeout=10,
            )
            del_resp.raise_for_status()

        try:
            _execute_with_retry(_delete_user)
        except Exception:
            logger.error(
                "keycloak_admin: failed to rollback user %s after provisioning error",
                user_uuid,
            )
        raise

    logger.info(
        "keycloak_admin: provisioned user %s (sub=%s) with roles=%s",
        email,
        user_uuid,
        roles,
    )
    return user_uuid


def deactivate_user(user_sub: str) -> None:
    """Disable IAM account and revoke all active sessions."""
    if not getattr(settings, "KEYCLOAK_ENABLED", False):
        logger.info(
            "keycloak_admin.deactivate_user skipped (dev mode): sub=%s", user_sub
        )
        return

    base_url, realm = _realm_base()
    admin_base = f"{base_url}/admin/realms/{realm}"

    # Step 1 — Disable user
    def _disable():
        resp = requests.put(
            f"{admin_base}/users/{user_sub}",
            json={"enabled": False},
            headers=_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()

    _execute_with_retry(_disable)

    # Step 2 — Revoke all sessions
    def _revoke_sessions():
        resp = requests.delete(
            f"{admin_base}/users/{user_sub}/sessions",
            headers=_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()

    _execute_with_retry(_revoke_sessions)

    logger.info("keycloak_admin: deactivated user %s", user_sub)


def assign_client_role(user_sub: str, role_name: str) -> None:
    """Assign an NBES client role to a user in Keycloak."""
    if not getattr(settings, "KEYCLOAK_ENABLED", False):
        logger.info(
            "keycloak_admin.assign_client_role skipped (dev mode): sub=%s role=%s",
            user_sub,
            role_name,
        )
        return

    base_url, realm = _realm_base()
    admin_base = f"{base_url}/admin/realms/{realm}"
    client_id = _nbes_client_id()
    client_uuid = _get_client_uuid(client_id)

    # Fetch role representation (with retry for transient 5xx)
    def _fetch_role():
        return requests.get(
            f"{admin_base}/clients/{client_uuid}/roles/{role_name}",
            headers=_auth_headers(),
            timeout=10,
        )

    role_resp = _execute_with_retry(_fetch_role)
    if role_resp.status_code == 404:
        logger.warning(
            "keycloak_admin: client role %r not found — cannot assign to user %s",
            role_name,
            user_sub,
        )
        return
    role_resp.raise_for_status()
    role_repr = role_resp.json()

    # Assign mapping
    def _assign():
        resp = requests.post(
            f"{admin_base}/users/{user_sub}/role-mappings/clients/{client_uuid}",
            json=[role_repr],
            headers=_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()

    _execute_with_retry(_assign)

    logger.info("keycloak_admin: mapped client role %r to user %s", role_name, user_sub)


def remove_client_role(user_sub: str, role_name: str) -> None:
    """Remove an NBES client role from a user in Keycloak."""
    if not getattr(settings, "KEYCLOAK_ENABLED", False):
        logger.info(
            "keycloak_admin.remove_client_role skipped (dev mode): sub=%s role=%s",
            user_sub,
            role_name,
        )
        return

    base_url, realm = _realm_base()
    admin_base = f"{base_url}/admin/realms/{realm}"
    client_id = _nbes_client_id()
    client_uuid = _get_client_uuid(client_id)

    # Fetch role representation (with retry for transient 5xx)
    def _fetch_role():
        return requests.get(
            f"{admin_base}/clients/{client_uuid}/roles/{role_name}",
            headers=_auth_headers(),
            timeout=10,
        )

    role_resp = _execute_with_retry(_fetch_role)
    if role_resp.status_code == 404:
        logger.warning(
            "keycloak_admin: client role %r not found — nothing to revoke for user %s",
            role_name,
            user_sub,
        )
        return
    role_resp.raise_for_status()
    role_repr = role_resp.json()

    # Delete mapping
    def _remove():
        resp = requests.delete(
            f"{admin_base}/users/{user_sub}/role-mappings/clients/{client_uuid}",
            json=[role_repr],
            headers=_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()

    _execute_with_retry(_remove)

    logger.info(
        "keycloak_admin: removed client role %r from user %s", role_name, user_sub
    )


def bulk_create_users(users: list[dict]) -> list[dict]:
    """Batch provision users in Keycloak/IAM."""
    results = []
    for user_data in users:
        email = user_data.get("email")
        first_name = user_data.get("first_name", "")
        last_name = user_data.get("last_name", "")
        roles = user_data.get("roles", [])
        try:
            sub = create_user(email, first_name, last_name, roles)
            results.append({"email": email, "sub": sub, "error": None})
        except Exception as exc:
            logger.exception("keycloak_admin.bulk_create_users failed for %s", email)
            results.append({"email": email, "sub": None, "error": str(exc)})
    return results


def revoke_realm_role(user_sub: str, role_name: str) -> None:
    """Remove a realm-level role from a Keycloak user identified by their sub UUID.

    (Maintained for backward compatibility).
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
