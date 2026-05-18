"""apps/users/serializers.py — DRF serializers for auth and admin endpoints."""
from rest_framework import serializers

from apps.users.models import MFAEnrolment, UserProfile


# ── Auth flow ────────────────────────────────────────────────────────────────

class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class MFAVerifySerializer(serializers.Serializer):
    challenge_token = serializers.CharField()
    code = serializers.CharField(max_length=10)


class RefreshSerializer(serializers.Serializer):
    refresh_token = serializers.CharField()


class AcceptInviteSerializer(serializers.Serializer):
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


# ── MFA enrolment ────────────────────────────────────────────────────────────

class TOTPEnrolStartSerializer(serializers.Serializer):
    label = serializers.CharField(required=False, allow_blank=True, max_length=100)


class TOTPEnrolConfirmSerializer(serializers.Serializer):
    enrolment_id = serializers.UUIDField()
    code = serializers.CharField(max_length=10)


class WebAuthnFinishSerializer(serializers.Serializer):
    credential = serializers.JSONField()


# ── Admin User Console ───────────────────────────────────────────────────────

class CreateUserSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    role = serializers.CharField(max_length=50)


class EditUserSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150, required=False)
    last_name = serializers.CharField(max_length=150, required=False)
    role = serializers.CharField(max_length=50, required=False)
    status = serializers.ChoiceField(choices=UserProfile.Status.choices, required=False)


class DeactivateUserSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


# ── Response shapes ──────────────────────────────────────────────────────────

class MFAEnrolmentResponseSerializer(serializers.ModelSerializer):
    class Meta:
        model = MFAEnrolment
        fields = ["id", "factor_type", "label", "confirmed_at", "last_used_at", "created_at"]


class UserResponseSerializer(serializers.ModelSerializer):
    mfa_enrolments = MFAEnrolmentResponseSerializer(many=True, read_only=True)
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = UserProfile
        fields = [
            "id", "email", "first_name", "last_name", "full_name",
            "role", "status", "mfa_enrolled", "mfa_enrolments",
            "last_login_at", "deactivated_at", "created_at", "updated_at",
        ]
        read_only_fields = fields


class TokenResponseSerializer(serializers.Serializer):
    access_token = serializers.CharField()
    refresh_token = serializers.CharField()
    expires_in = serializers.IntegerField()
    refresh_expires_in = serializers.IntegerField()
    session_id = serializers.CharField()
    token_type = serializers.CharField(default="Bearer")


class MFAChallengeResponseSerializer(serializers.Serializer):
    mfa_required = serializers.BooleanField(default=True)
    challenge_token = serializers.CharField()
    factors = serializers.ListField(child=serializers.CharField())
