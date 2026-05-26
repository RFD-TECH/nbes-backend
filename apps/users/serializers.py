"""Input/output shapes for the RBAC admin API."""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Permission, Role, RoleAssignmentApproval, RoleMutualExclusion


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = ["id", "codename", "description", "created_at"]
        read_only_fields = ["id", "codename", "created_at"]


class RoleSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = [
            "id",
            "name",
            "description",
            "is_active",
            "is_custom",
            "is_internal",
            "version",
            "permissions",
            "created_at",
        ]
        read_only_fields = ["id", "is_custom", "is_internal", "version", "permissions", "created_at"]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_permissions(self, obj):
        return sorted(obj.grants.values_list("permission__codename", flat=True))


class CreateRoleSerializer(serializers.Serializer):
    """Mirror an IAM-issued role name into NBES's local registry.

    NBES does not create roles in Keycloak — IAM does. This endpoint just
    records that NBES recognises the name and can map permissions to it.
    """
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True)


class UpdateRolePermissionsSerializer(serializers.Serializer):
    """Replace the codename grants on a role (full intended set)."""
    codenames = serializers.ListField(
        child=serializers.CharField(max_length=100),
        allow_empty=True,
    )

    def validate_codenames(self, value):
        unknown = set(value) - set(
            Permission.objects.filter(codename__in=value).values_list("codename", flat=True)
        )
        if unknown:
            raise serializers.ValidationError(
                f"Unknown codenames: {sorted(unknown)}. Codenames must be declared in code "
                "and seeded via migration; they cannot be created at runtime."
            )
        return value


class UserRoleSerializer(serializers.Serializer):
    role_name = serializers.CharField(source="role.name")
    effective_from = serializers.DateField()
    effective_to = serializers.DateField()


class UserProfileSerializer(serializers.ModelSerializer):
    roles = serializers.SerializerMethodField()

    class Meta:
        from .models import UserProfile
        model = UserProfile
        fields = [
            "id",
            "keycloak_sub",
            "email",
            "first_name",
            "last_name",
            "status",
            "metadata",
            "created_by",
            "deactivated_at",
            "created_at",
            "updated_at",
            "roles",
        ]
        read_only_fields = ["id", "keycloak_sub", "created_by", "deactivated_at", "created_at", "updated_at"]

    @extend_schema_field(UserRoleSerializer(many=True))
    def get_roles(self, obj):
        # Only return active roles (not revoked)
        active_roles = obj.user_roles.filter(revoked_at__isnull=True)
        return UserRoleSerializer(active_roles, many=True).data


class UserProfileCreateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    roles = serializers.ListField(child=serializers.CharField(max_length=100))
    effective_date = serializers.DateField(required=False)
    metadata = serializers.JSONField(default=dict, required=False)

    def validate_email(self, value):
        from .models import UserProfile
        if UserProfile.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user profile with this email already exists.")
        return value

    def validate_roles(self, value):
        from .models import Role
        unknown = set(value) - set(
            Role.objects.filter(name__in=value, is_active=True).values_list("name", flat=True)
        )
        if unknown:
            raise serializers.ValidationError(f"Unknown or inactive roles: {sorted(unknown)}")
        return value


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    deleted = serializers.BooleanField(required=False, default=False)

    class Meta:
        from .models import UserProfile
        model = UserProfile
        fields = ["first_name", "last_name", "email", "status", "metadata", "deleted"]
        extra_kwargs = {
            "first_name": {"required": False},
            "last_name": {"required": False},
            "email": {"required": False},
            "status": {"required": False},
            "metadata": {"required": False},
        }

    def validate_email(self, value):
        from .models import UserProfile
        instance = self.instance
        if instance and instance.email.lower() == value.lower():
            return value
        if UserProfile.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user profile with this email already exists.")
        return value


# ── Role assignment / revocation (POST /admin/users/{id}/roles/) ─────────────

class UserRoleAssignSerializer(serializers.Serializer):
    """Input for assigning or revoking a role on a user profile.

    Used by ``AdminUserRolesView``.  The ``action`` field disambiguates
    so the same endpoint handles both flows.
    """
    ACTION_ASSIGN = "assign"
    ACTION_REVOKE = "revoke"
    ACTION_CHOICES = [ACTION_ASSIGN, ACTION_REVOKE]

    action = serializers.ChoiceField(choices=ACTION_CHOICES)
    role = serializers.CharField(max_length=100)
    effective_from = serializers.DateField(required=False)
    effective_to = serializers.DateField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def validate_role(self, value):
        try:
            Role.objects.get(name=value, is_active=True)
        except Role.DoesNotExist:
            raise serializers.ValidationError(f"Role '{value}' not found or inactive.")
        return value


# ── Mutual-Exclusion Rules ────────────────────────────────────────────────────

class RoleMutualExclusionSerializer(serializers.ModelSerializer):
    role_a_name = serializers.CharField(source="role_a.name", read_only=True)
    role_b_name = serializers.CharField(source="role_b.name", read_only=True)

    class Meta:
        model = RoleMutualExclusion
        fields = [
            "id",
            "role_a",
            "role_a_name",
            "role_b",
            "role_b_name",
            "reason",
            "created_at",
        ]
        read_only_fields = ["id", "role_a_name", "role_b_name", "created_at"]


class RoleMutualExclusionCreateSerializer(serializers.Serializer):
    """Create a new mutual-exclusion rule. Roles are normalised (sorted) server-side."""
    role_a = serializers.CharField(max_length=100, help_text="Role name (order doesn't matter)")
    role_b = serializers.CharField(max_length=100, help_text="Role name (order doesn't matter)")
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")

    def _resolve(self, name):
        try:
            return Role.objects.get(name=name)
        except Role.DoesNotExist:
            raise serializers.ValidationError(f"Role '{name}' not found.")

    def validate(self, data):
        role_a = self._resolve(data["role_a"])
        role_b = self._resolve(data["role_b"])
        if role_a.pk == role_b.pk:
            raise serializers.ValidationError("A role cannot be mutually exclusive with itself.")
        # Normalise: store as (lower_name, higher_name)
        if role_a.name > role_b.name:
            role_a, role_b = role_b, role_a
        if RoleMutualExclusion.objects.filter(role_a=role_a, role_b=role_b).exists():
            raise serializers.ValidationError(
                f"Exclusion rule for ({role_a.name}, {role_b.name}) already exists."
            )
        data["role_a_obj"] = role_a
        data["role_b_obj"] = role_b
        return data


# ── Two-Administrator Approval ────────────────────────────────────────────────

class RoleAssignmentApprovalSerializer(serializers.ModelSerializer):
    target_user_email = serializers.CharField(source="target_user.email", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)
    requested_by_email = serializers.CharField(source="requested_by.email", read_only=True)
    reviewed_by_email = serializers.SerializerMethodField()

    class Meta:
        model = RoleAssignmentApproval
        fields = [
            "id",
            "target_user",
            "target_user_email",
            "role",
            "role_name",
            "effective_from",
            "effective_to",
            "status",
            "reason",
            "review_note",
            "requested_by",
            "requested_by_email",
            "reviewed_by",
            "reviewed_by_email",
            "expires_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "target_user_email", "role_name",
            "requested_by_email", "reviewed_by_email",
            "expires_at", "created_at", "updated_at",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_reviewed_by_email(self, obj):
        return obj.reviewed_by.email if obj.reviewed_by else None


class RoleApprovalActionSerializer(serializers.Serializer):
    """Body for the approve / reject endpoints."""
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


# ── Bulk Import ───────────────────────────────────────────────────

class BulkImportRowErrorSerializer(serializers.Serializer):
    row = serializers.IntegerField()
    email = serializers.CharField(allow_blank=True)
    errors = serializers.ListField(child=serializers.CharField())


class BulkImportRecordSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    original_filename = serializers.CharField()
    file_hash = serializers.CharField()
    status = serializers.CharField()
    total_rows = serializers.IntegerField()
    success_count = serializers.IntegerField()
    failure_count = serializers.IntegerField()
    row_errors = BulkImportRowErrorSerializer(many=True)
    created_at = serializers.DateTimeField()
    completed_at = serializers.DateTimeField(allow_null=True)


# ── Bulk Role Assignment  ─────────────────────────────────────────────

class BulkRoleAssignSerializer(serializers.Serializer):
    """Assign or revoke a role for multiple existing profiles at once."""
    ACTION_ASSIGN = "assign"
    ACTION_REVOKE = "revoke"

    action = serializers.ChoiceField(choices=[ACTION_ASSIGN, ACTION_REVOKE])
    role = serializers.CharField(max_length=100)
    user_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=500,
        help_text="List of UserProfile UUIDs to assign/revoke the role on.",
    )
    effective_from = serializers.DateField(required=False)
    effective_to = serializers.DateField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def validate_role(self, value):
        try:
            Role.objects.get(name=value, is_active=True)
        except Role.DoesNotExist:
            raise serializers.ValidationError(f"Role '{value}' not found or inactive.")
        return value
