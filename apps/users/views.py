"""apps/users/views.py — Auth, MFA enrolment, /me, Admin User Console.

Routes are wired in apps/users/urls.py. Business logic lives in
auth_service.py, mfa_service.py, and services.py — views are thin.
"""
from dataclasses import asdict

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from apps.users import auth_service, mfa_service, services
from apps.users.models import UserProfile
from apps.users.serializers import (
    AcceptInviteSerializer,
    CreateUserSerializer,
    DeactivateUserSerializer,
    EditUserSerializer,
    LoginSerializer,
    MFAChallengeResponseSerializer,
    MFAVerifySerializer,
    RefreshSerializer,
    TOTPEnrolConfirmSerializer,
    TOTPEnrolStartSerializer,
    TokenResponseSerializer,
    UserResponseSerializer,
    WebAuthnFinishSerializer,
)
from shared.exceptions import error_response, success_response
from shared.permissions import has_permission


# ── Helpers ──────────────────────────────────────────────────────────────────

def _auth_error_to_response(exc: auth_service.AuthError, request):
    return error_response(
        code=exc.code,
        message=str(exc) or exc.code,
        status_code=exc.status_code,
        request=request,
    )


def _client_ip(request) -> str | None:
    return getattr(request, "ip_address", None)


def _ua(request) -> str:
    return getattr(request, "user_agent", "")


def _get_actor(request) -> UserProfile:
    return request.user


# ── Auth flow ────────────────────────────────────────────────────────────────

class LoginView(APIView):
    """POST /api/v1/auth/login — credential auth.

    Returns either a TokenResponse (auth complete) or an MFAChallengeResponse
    (caller must call /auth/mfa next).
    """
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = auth_service.authenticate(
                email=serializer.validated_data["email"],
                password=serializer.validated_data["password"],
                ip=_client_ip(request),
                user_agent=_ua(request),
            )
        except auth_service.AuthError as exc:
            return _auth_error_to_response(exc, request)

        if isinstance(result, auth_service.MFAChallenge):
            payload = MFAChallengeResponseSerializer(asdict(result) | {"mfa_required": True}).data
            return success_response(payload, request=request)

        payload = TokenResponseSerializer(asdict(result)).data
        return success_response(payload, request=request)


class MFAVerifyView(APIView):
    """POST /api/v1/auth/mfa — verify TOTP code; returns a TokenResponse on success."""
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = MFAVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            tokens = auth_service.verify_mfa_totp(
                challenge_token=serializer.validated_data["challenge_token"],
                code=serializer.validated_data["code"],
                ip=_client_ip(request),
                user_agent=_ua(request),
            )
        except auth_service.AuthError as exc:
            return _auth_error_to_response(exc, request)

        return success_response(TokenResponseSerializer(asdict(tokens)).data, request=request)


class RefreshView(APIView):
    """POST /api/v1/auth/refresh — rotate the access + refresh tokens."""
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tokens = auth_service.refresh_session(
                refresh_token=serializer.validated_data["refresh_token"],
                ip=_client_ip(request),
                user_agent=_ua(request),
            )
        except auth_service.AuthError as exc:
            return _auth_error_to_response(exc, request)
        return success_response(TokenResponseSerializer(asdict(tokens)).data, request=request)


class LogoutView(APIView):
    """POST /api/v1/auth/logout — revoke the current session."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        jti = (request.auth or {}).get("jti")
        if jti:
            auth_service.logout(
                jti=jti,
                actor_id=getattr(_get_actor(request), "id", None),
                ip=_client_ip(request),
            )
        return success_response({"logged_out": True}, request=request)


class AcceptInviteView(APIView):
    """POST /api/v1/auth/accept-invite — set password from the invite token."""
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = AcceptInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.accept_invite(
                token=serializer.validated_data["token"],
                password=serializer.validated_data["password"],
                ip=_client_ip(request),
            )
        except DjangoValidationError as exc:
            raise ValidationError(detail={"password": list(exc.messages)})
        except ValueError as exc:
            return error_response(
                code="INVALID_INVITE",
                message=str(exc),
                status_code=status.HTTP_400_BAD_REQUEST,
                request=request,
            )
        return success_response(UserResponseSerializer(user).data, request=request)


class MeView(APIView):
    """GET /api/v1/me — current user profile and effective permissions."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from shared.permissions import ROLE_PERMISSION_MAP
        user = _get_actor(request)
        permissions = sorted(
            p for p, roles in ROLE_PERMISSION_MAP.items() if user.role in roles
        )
        body = UserResponseSerializer(user).data | {"permissions": permissions}
        return success_response(body, request=request)


# ── MFA enrolment ────────────────────────────────────────────────────────────

class TOTPEnrolStartView(APIView):
    """POST /api/v1/auth/mfa/totp/enroll — start a TOTP enrolment.

    Returns secret + otpauth:// URL. Client renders QR; user scans and calls
    /auth/mfa/totp/confirm with a fresh code.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TOTPEnrolStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        challenge = mfa_service.start_totp_enrolment(
            _get_actor(request), label=serializer.validated_data.get("label", "")
        )
        return success_response(asdict(challenge), request=request)


class TOTPEnrolConfirmView(APIView):
    """POST /api/v1/auth/mfa/totp/confirm — activate the TOTP enrolment."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TOTPEnrolConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            enrolment = mfa_service.confirm_totp_enrolment(
                _get_actor(request),
                enrolment_id=str(serializer.validated_data["enrolment_id"]),
                code=serializer.validated_data["code"],
            )
        except ValueError as exc:
            return error_response(
                code="MFA_ENROL_FAILED",
                message=str(exc),
                status_code=status.HTTP_400_BAD_REQUEST,
                request=request,
            )
        return success_response(
            {"enrolment_id": str(enrolment.id), "confirmed": True}, request=request
        )


class WebAuthnRegisterBeginView(APIView):
    """POST /api/v1/auth/mfa/webauthn/register/begin — get registration options."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        options = mfa_service.begin_webauthn_registration(_get_actor(request))
        return success_response(options, request=request)


class WebAuthnRegisterFinishView(APIView):
    """POST /api/v1/auth/mfa/webauthn/register/finish — verify the attestation."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = WebAuthnFinishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            enrolment = mfa_service.finish_webauthn_registration(
                _get_actor(request),
                credential_payload=serializer.validated_data["credential"],
            )
        except ValueError as exc:
            return error_response(
                code="WEBAUTHN_REGISTRATION_FAILED",
                message=str(exc),
                status_code=status.HTTP_400_BAD_REQUEST,
                request=request,
            )
        return success_response(
            {"enrolment_id": str(enrolment.id), "confirmed": True}, request=request
        )


# ── Admin User Console ───────────────────────────────────────────────────────

class AdminUserListCreateView(APIView):
    """POST /api/v1/admin/users — create a user (sends invite email).
    GET  /api/v1/admin/users — list users.
    """
    permission_classes = [IsAuthenticated, has_permission("user:manage")]

    def get(self, request):
        users = UserProfile.objects.order_by("-created_at")[:200]
        return success_response(
            UserResponseSerializer(users, many=True).data, request=request
        )

    def post(self, request):
        serializer = CreateUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.create_user(
                email=serializer.validated_data["email"],
                first_name=serializer.validated_data["first_name"],
                last_name=serializer.validated_data["last_name"],
                role=serializer.validated_data["role"],
                actor=_get_actor(request),
                actor_ip=_client_ip(request),
            )
        except ValueError as exc:
            return error_response(
                code="USER_CREATE_FAILED",
                message=str(exc),
                status_code=status.HTTP_400_BAD_REQUEST,
                request=request,
            )
        return success_response(
            UserResponseSerializer(user).data,
            request=request,
            status_code=status.HTTP_201_CREATED,
        )


class AdminUserDetailView(APIView):
    """PATCH /api/v1/admin/users/{id}, DELETE /api/v1/admin/users/{id} (deactivate)."""
    permission_classes = [IsAuthenticated, has_permission("user:manage")]

    def _get_user(self, user_id):
        try:
            return UserProfile.objects.get(id=user_id)
        except UserProfile.DoesNotExist:
            raise NotFound(f"User {user_id} not found.")

    def get(self, request, user_id):
        user = self._get_user(user_id)
        return success_response(UserResponseSerializer(user).data, request=request)

    def patch(self, request, user_id):
        user = self._get_user(user_id)
        serializer = EditUserSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = services.edit_user(
            user_id=user.id,
            actor=_get_actor(request),
            actor_ip=_client_ip(request),
            **serializer.validated_data,
        )
        return success_response(UserResponseSerializer(user).data, request=request)

    def delete(self, request, user_id):
        user = self._get_user(user_id)
        serializer = DeactivateUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.deactivate_user(
                user_id=user.id,
                actor=_get_actor(request),
                actor_ip=_client_ip(request),
                reason=serializer.validated_data.get("reason", ""),
            )
        except ValueError as exc:
            return error_response(
                code="USER_DEACTIVATE_BLOCKED",
                message=str(exc),
                status_code=status.HTTP_409_CONFLICT,
                request=request,
            )
        return success_response(UserResponseSerializer(user).data, request=request)


class AdminUserMFAResetView(APIView):
    """POST /api/v1/admin/users/{id}/mfa/reset — clear a user's MFA enrolments."""
    permission_classes = [IsAuthenticated, has_permission("user:manage")]

    def post(self, request, user_id):
        try:
            user = UserProfile.objects.get(id=user_id)
        except UserProfile.DoesNotExist:
            raise NotFound(f"User {user_id} not found.")
        user = services.reset_mfa(
            user_id=user.id,
            actor=_get_actor(request),
            actor_ip=_client_ip(request),
        )
        return success_response(UserResponseSerializer(user).data, request=request)
