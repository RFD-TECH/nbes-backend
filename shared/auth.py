"""shared/auth.py — JWT authentication for NBES.

Two modes, switched by ``settings.KEYCLOAK_ENABLED``:

* **Production** (``KEYCLOAK_ENABLED=True``): validates Keycloak RS256
  tokens. The signing key is fetched from the realm JWKS endpoint and
  cached for ``JWKS_CACHE_SECONDS`` (5 minutes). Issuer is checked against
  ``settings.KEYCLOAK_REALM_URL``; audience against
  ``settings.KEYCLOAK_VALID_AUDIENCES`` (when set).

* **Dev** (``KEYCLOAK_ENABLED=False``): validates an HS256 token signed
  with ``settings.JWT_SECRET_KEY`` so local development needs no Keycloak.

On success ``request.auth`` carries the decoded payload — including
``sub``, ``email``, and ``realm_access.roles``. ``request.user`` is a thin
``UserProfile`` mirror (created on first sight). Identity, MFA, sessions
and roles all stay with IAM; NBES never stores credentials.

Ported from ``iam/users/authentication.py`` so the wire-format and JWKS
behaviour stay identical.
"""
from __future__ import annotations

import json

import jwt
import requests
from django.conf import settings
from django.core.cache import cache
from jwt.algorithms import RSAAlgorithm
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


JWKS_CACHE_SECONDS = 300


def _categorise(message: str) -> str:
    """Pick a SecurityEvent category from an AuthenticationFailed message."""
    low = (message or "").lower()
    if "expired" in low:
        return "auth_token_expired"
    if "audience" in low or "aud" in low:
        return "auth_audience_mismatch"
    return "auth_token_invalid"


def _normalise_url(url: str) -> str:
    return (url or "").rstrip("/")


def _fetch_jwks(realm_url: str) -> dict:
    cache_key = f"nbes:keycloak:jwks:{realm_url}"
    jwks = cache.get(cache_key)
    if jwks:
        return jwks

    response = requests.get(
        f"{realm_url}/protocol/openid-connect/certs",
        timeout=5,
    )
    response.raise_for_status()
    jwks = response.json()
    cache.set(cache_key, jwks, timeout=JWKS_CACHE_SECONDS)
    return jwks


def _signing_key(realm_url: str, kid: str):
    jwks = _fetch_jwks(realm_url)
    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            return RSAAlgorithm.from_jwk(json.dumps(key_data))

    # Key rotation: drop the cached JWKS and try once more.
    cache.delete(f"nbes:keycloak:jwks:{realm_url}")
    jwks = _fetch_jwks(realm_url)
    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            return RSAAlgorithm.from_jwk(json.dumps(key_data))

    raise AuthenticationFailed("Token signing key not found.")


def _decode_rs256(token: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["RS256"],
        )
    except Exception as exc:  # malformed header / body
        raise AuthenticationFailed("Invalid token format.") from exc

    realm_url = _normalise_url(getattr(settings, "KEYCLOAK_REALM_URL", ""))
    if not realm_url:
        raise AuthenticationFailed("KEYCLOAK_REALM_URL is not configured.")

    if _normalise_url(unverified.get("iss", "")) != realm_url:
        raise AuthenticationFailed("Token issuer not recognised.")

    key = _signing_key(realm_url, header.get("kid", ""))

    audiences = [
        aud for aud in getattr(settings, "KEYCLOAK_VALID_AUDIENCES", []) or [] if aud
    ]
    decode_kwargs = {
        "key": key,
        "algorithms": ["RS256"],
        "issuer": realm_url,
    }
    if audiences:
        decode_kwargs["audience"] = audiences
    else:
        decode_kwargs["options"] = {"verify_aud": False}

    try:
        return jwt.decode(token, **decode_kwargs)
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationFailed("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationFailed(f"Token validation failed: {exc}") from exc


def _decode_hs256(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationFailed("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationFailed(f"Invalid token: {exc}") from exc


class KeycloakJWTAuthentication(BaseAuthentication):
    """DRF authentication class.

    Dispatches on the token's ``alg`` header:

    * ``RS256`` → Keycloak path (JWKS, issuer check). Requires
      ``settings.KEYCLOAK_REALM_URL`` regardless of ``KEYCLOAK_ENABLED``,
      so a dev developer can paste a real IAM token by just setting that
      one variable.
    * ``HS256`` → dev path with the shared secret. Refused when
      ``KEYCLOAK_ENABLED=True`` so prod won't accept forged HS256 tokens.
    """

    def authenticate(self, request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return None

        try:
            alg = jwt.get_unverified_header(token).get("alg")
        except Exception as exc:
            self._record_failure(request, "auth_token_invalid", reason=str(exc))
            raise AuthenticationFailed("Invalid token format.") from exc

        try:
            if alg == "RS256":
                payload = _decode_rs256(token)
            elif alg == "HS256":
                if settings.KEYCLOAK_ENABLED:
                    raise AuthenticationFailed(
                        "HS256 tokens are not accepted in Keycloak mode."
                    )
                payload = _decode_hs256(token)
            else:
                raise AuthenticationFailed(
                    f"Unsupported signing algorithm: {alg}. Use the RS256 "
                    "Keycloak access_token returned by IAM /api/auth/mfa/verify/."
                )
        except AuthenticationFailed as exc:
            self._record_failure(request, _categorise(str(exc)), reason=str(exc))
            raise

        # Normalise to the production shape: callers downstream rely on
        # `sub` and `realm_access.roles` regardless of mode.
        payload.setdefault("sub", payload.get("user_id", ""))
        if "realm_access" not in payload:
            single = payload.get("role")
            roles = single if isinstance(single, list) else ([single] if single else [])
            payload["realm_access"] = {"roles": roles}

        user = self._mirror_profile(payload)
        return user, payload

    def authenticate_header(self, request):
        return "Bearer"

    @staticmethod
    def _record_failure(request, category, *, reason: str = "") -> None:
        """Best-effort SecurityEvent emission on auth failure.

        We do not import at module top level because some failure paths
        (e.g. early-boot import of the auth class by DRF) precede the
        app registry being ready.
        """
        try:
            from shared.secops import record_security_event
            record_security_event(
                category=category,
                ip_address=getattr(request, "ip_address", None)
                    or request.META.get("REMOTE_ADDR"),
                request_id=getattr(request, "request_id", None),
                indicators={
                    "path": request.path,
                    "method": request.method,
                    "reason": reason[:200],
                },
            )
        except Exception:
            # Never let security-event recording mask the auth failure.
            pass

    def _mirror_profile(self, payload: dict):
        """Get-or-create the thin local UserProfile keyed on Keycloak sub."""
        from apps.users.models import UserProfile

        sub = payload.get("sub")
        if not sub:
            raise AuthenticationFailed("Token subject missing.")

        roles = payload.get("realm_access", {}).get("roles") or []
        primary_role = roles[0] if roles else ""

        user, _ = UserProfile.objects.get_or_create(
            keycloak_sub=sub,
            defaults={
                "email": payload.get("email", ""),
                "role": primary_role,
            },
        )
        return user
