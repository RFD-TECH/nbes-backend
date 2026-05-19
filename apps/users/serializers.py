"""apps/users/serializers.py — Input/output shapes for the RBAC admin API."""
from rest_framework import serializers

from .models import Permission, Role


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = ["id", "codename", "description", "created_at"]
        read_only_fields = ["id", "codename", "created_at"]


class RoleSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = ["id", "name", "description", "is_active", "permissions", "created_at"]
        read_only_fields = ["id", "permissions", "created_at"]

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
