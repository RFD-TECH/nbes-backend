"""apps/dashboards/serializers.py — Output + admin-edit shapes."""
from rest_framework import serializers

from .models import DashboardPanel


class DashboardPanelSerializer(serializers.ModelSerializer):
    class Meta:
        model = DashboardPanel
        fields = [
            "panel_key",
            "panel_name",
            "role_codename",
            "display_order",
            "is_active",
            "default_config",
            "updated_at",
        ]
        read_only_fields = ["panel_key", "role_codename", "updated_at"]


class PatchPanelSerializer(serializers.Serializer):
    """PATCH /api/v1/dashboard/panels/{panel_key} — admin edits."""
    panel_name = serializers.CharField(max_length=120, required=False)
    display_order = serializers.IntegerField(required=False, min_value=0)
    is_active = serializers.BooleanField(required=False)
    default_config = serializers.DictField(required=False)
