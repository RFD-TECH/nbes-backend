"""
shared/auth.py — JWT Authentication for NBES
=============================================

In dev (KEYCLOAK_ENABLED=False):
    Validates JWTs using the shared HS256 secret (JWT_SECRET_KEY).
    Token payload contains: sub, user_id, email, role, jti.
    Looks up the Session by jti and rejects revoked sessions — this is how
    SRS §1.2.2's 60-second permission propagation is enforced.

In production (KEYCLOAK_ENABLED=True):
    Validates Keycloak-issued RS256 JWTs by fetching JWKS from the realm URL.
    Reads realm_access.roles to populate request.auth["role"].
    NOT YET IMPLEMENTED — Keycloak is the production target IdP but not online.
    See memory/auth_mode.md for the cutover plan.

Usage in views:
    request.auth["sub"]        — Keycloak sub UUID (user identity)
    request.auth["user_id"]    — local UserProfile.id (UUID)
    request.auth["email"]      — user email
    request.auth["role"]       — primary NBES role string
    request.auth["roles"]      — full list of roles from realm_access.roles
    request.auth["jti"]        — session identifier; used by logout
"""

import jwt
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


class KeycloakJWTAuthentication(BaseAuthentication):
    """DRF authentication. Validates Bearer JWT and populates request.auth."""

    def authenticate(self, request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header.split(" ", 1)[1]

        try:
            if settings.KEYCLOAK_ENABLED:
                # TODO: JWKS-based RS256 validation when Keycloak comes online.
                # See memory/auth_mode.md and GSL IAM Keycloak Specification v1.0.
                raise NotImplementedError(
                    "Keycloak JWKS validation not yet implemented."
                )
            else:
                payload = jwt.decode(
                    token,
                    settings.JWT_SECRET_KEY,
                    algorithms=[settings.JWT_ALGORITHM],
                )
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed("Token has expired.")
        except jwt.InvalidTokenError as e:
            raise AuthenticationFailed(f"Invalid token: {e}")

        if payload.get("type") not in (None, "access"):
            raise AuthenticationFailed("Wrong token type for this endpoint.")

        # Normalise payload — Keycloak uses 'sub'; dev tokens carry both 'sub' and 'user_id'.
        payload.setdefault("sub", payload.get("user_id", ""))
        payload.setdefault("roles", [payload.get("role", "")])

        user = self._resolve_user(payload)

        # Honor session revocation (SRS §1.2.2 — 60-second propagation).
        jti = payload.get("jti")
        if jti:
            from apps.users.models import Session
            try:
                session = Session.objects.only("revoked_at").get(jti=jti)
            except Session.DoesNotExist:
                raise AuthenticationFailed("Session not found.")
            if session.revoked_at is not None:
                raise AuthenticationFailed("Session has been revoked.")

        return user, payload

    def _resolve_user(self, payload):
        """Look up or mirror the UserProfile referenced by the JWT.

        Dev mode: tokens are issued by our own auth_service, so user_id matches
        a local UserProfile. Keycloak mode: keycloak_sub is the link.
        """
        from apps.users.models import UserProfile

        user_id = payload.get("user_id") or None
        sub = payload.get("sub") or None

        if user_id:
            try:
                return UserProfile.objects.get(id=user_id)
            except UserProfile.DoesNotExist:
                pass

        if sub:
            # Keycloak mode: mirror the user record on first sight.
            user, _ = UserProfile.objects.get_or_create(
                keycloak_sub=sub,
                defaults={
                    "email": payload.get("email", "") or f"{sub}@unknown",
                    "role": payload.get("role", ""),
                },
            )
            return user

        raise AuthenticationFailed("Token missing sub/user_id.")

    def authenticate_header(self, request):
        return "Bearer"
