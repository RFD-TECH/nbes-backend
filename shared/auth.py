"""
shared/auth.py — JWT Authentication for NBES
=============================================

In dev (KEYCLOAK_ENABLED=False):
    Validates JWTs using the shared HS256 secret (JWT_SECRET_KEY).
    Token payload must contain: user_id, email, role.

In production (KEYCLOAK_ENABLED=True):
    Validates Keycloak-issued RS256 JWTs by fetching JWKS from the realm URL.
    Reads realm_access.roles to populate request.auth["role"].
    Public key is cached per realm to avoid per-request JWKS fetches.

Usage in views:
    request.auth["sub"]        — Keycloak sub UUID (user identity)
    request.auth["email"]      — user email
    request.auth["role"]       — primary NBES role string
    request.auth["roles"]      — full list of roles from realm_access.roles

Reference: GSL IAM Keycloak Specification v1.0 § KeycloakJWTAuthentication
"""

import jwt
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


class KeycloakJWTAuthentication(BaseAuthentication):
    """
    DRF authentication class.
    Validates Bearer JWT from the Authorization header.
    Populates request.auth with decoded token payload.
    request.user is set to an AnonymousUser — identity comes from request.auth.

    TODO (production): Implement JWKS fetching and RS256 validation when
    KEYCLOAK_ENABLED=True. See GSL IAM Keycloak Specification v1.0.
    """

    def authenticate(self, request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header.split(" ", 1)[1]

        try:
            if settings.KEYCLOAK_ENABLED:
                # TODO: Implement JWKS-based RS256 validation
                # 1. Decode header to get kid
                # 2. Fetch JWKS from settings.KEYCLOAK_REALM_URL + /protocol/openid-connect/certs
                # 3. Match kid to JWKS key
                # 4. Verify RS256 signature
                # 5. Validate iss, exp, aud claims
                raise NotImplementedError(
                    "Keycloak JWKS validation not yet implemented. "
                    "See GSL IAM Keycloak Specification v1.0."
                )
            else:
                # Dev mode: shared HS256 secret
                payload = jwt.decode(
                    token,
                    settings.JWT_SECRET_KEY,
                    algorithms=[settings.JWT_ALGORITHM],
                )
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed("Token has expired.")
        except jwt.InvalidTokenError as e:
            raise AuthenticationFailed(f"Invalid token: {e}")

        # Normalise payload — Keycloak uses 'sub'; dev tokens use 'user_id'
        payload.setdefault("sub", payload.get("user_id", ""))
        payload.setdefault("roles", [payload.get("role", "")])

        # get_or_create a thin UserProfile for Django admin compatibility
        from apps.users.models import UserProfile
        user, _ = UserProfile.objects.get_or_create(
            keycloak_sub=payload["sub"],
            defaults={
                "email": payload.get("email", ""),
                "role": payload.get("role", ""),
            },
        )

        return user, payload

    def authenticate_header(self, request):
        return "Bearer"
