"""apps/dashboards/views.py — Role dashboard skeleton endpoints.

* ``GET /api/v1/dashboard/me`` — returns the panel list for the
  intersection of the JWT's NBES role names with the seeded
  ``DashboardPanel`` rows.
* ``PATCH /api/v1/dashboard/panels/{panel_key}`` — admin-only edit. Lets
  the operator hide a panel for everyone or reorder it without a
  redeploy.

Frontend renders. Backend just serves the contract.
"""
from __future__ import annotations

from django.db import transaction
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from shared import rbac
from shared.permissions import has_permission

from .models import DashboardPanel
from .serializers import DashboardPanelSerializer, PatchPanelSerializer


def _envelope(data, request, status_code=status.HTTP_200_OK):
    return Response(
        {
            "success": True,
            "data": data,
            "meta": {"request_id": str(getattr(request, "request_id", ""))},
        },
        status=status_code,
    )


class MyDashboardView(APIView):
    """``GET /api/v1/dashboard/me`` — panels for the bearer's roles."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Dashboards"],
        summary="Get the current user's dashboard skeleton",
        operation_id="dashboard_me",
        description=(
            "Returns the active DashboardPanel rows for every NBES role "
            "the bearer holds, deduplicated and ordered by "
            "``(role_codename, display_order)``. Roles the user holds but "
            "NBES does not recognise are ignored — same rules as "
            "``/me/permissions``."
        ),
        responses={200: inline_serializer(
            name="MyDashboardResponse",
            fields={
                "success": serializers.BooleanField(default=True),
                "data": inline_serializer(
                    name="MyDashboardData",
                    fields={
                        "roles": serializers.ListField(child=serializers.CharField()),
                        "panels": DashboardPanelSerializer(many=True),
                    },
                ),
                "meta": inline_serializer(
                    name="MyDashboardMeta",
                    fields={"request_id": serializers.CharField()},
                ),
            },
        )},
        examples=[
            OpenApiExample(
                name="Examiner dashboard",
                value={
                    "success": True,
                    "data": {
                        "roles": ["examiner"],
                        "panels": [
                            {
                                "panel_key": "examiner.marking_queue",
                                "panel_name": "Marking queue",
                                "role_codename": "examiner",
                                "display_order": 10,
                                "is_active": True,
                                "default_config": {"filter": "assigned_to_me"},
                                "updated_at": "2026-05-20T12:00:00Z",
                            },
                            {
                                "panel_key": "examiner.borderline_review",
                                "panel_name": "Borderline review queue",
                                "role_codename": "examiner",
                                "display_order": 20,
                                "is_active": True,
                                "default_config": {},
                                "updated_at": "2026-05-20T12:00:00Z",
                            },
                        ],
                    },
                    "meta": {"request_id": "..."},
                },
            ),
        ],
    )
    def get(self, request):
        payload = request.auth or {}
        roles = rbac.get_nbes_role_names(payload) or []
        panels = (
            DashboardPanel.objects
            .filter(role_codename__in=roles, is_active=True)
            .order_by("role_codename", "display_order", "panel_name")
        )
        return _envelope(
            {
                "roles": sorted(set(roles)),
                "panels": DashboardPanelSerializer(panels, many=True).data,
            },
            request,
        )


class PanelDetailView(APIView):
    """``PATCH /api/v1/dashboard/panels/{panel_key}`` — admin-only edit."""

    permission_classes = [IsAuthenticated, has_permission("dashboards:manage")]

    @extend_schema(
        tags=["Dashboards"],
        summary="Update a dashboard panel's display config (admin)",
        operation_id="dashboard_panel_update",
        request=PatchPanelSerializer,
        responses={
            200: inline_serializer(
                name="DashboardPanelUpdateResponse",
                fields={
                    "success": serializers.BooleanField(default=True),
                    "data": DashboardPanelSerializer(),
                    "meta": inline_serializer(
                        name="DashboardPanelUpdateMeta",
                        fields={"request_id": serializers.CharField()},
                    ),
                },
            ),
            404: inline_serializer(
                name="PanelNotFound",
                fields={
                    "success": serializers.BooleanField(default=False),
                    "error": inline_serializer(
                        name="PanelNotFoundError",
                        fields={
                            "code": serializers.CharField(),
                            "message": serializers.CharField(),
                        },
                    ),
                },
            ),
        },
    )
    def patch(self, request, panel_key):
        try:
            panel = DashboardPanel.objects.get(panel_key=panel_key)
        except DashboardPanel.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "Panel not found."},
                    "meta": {"request_id": str(getattr(request, "request_id", ""))},
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PatchPanelSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        before = {
            "panel_name": panel.panel_name,
            "display_order": panel.display_order,
            "is_active": panel.is_active,
            "default_config": panel.default_config,
        }
        update_fields = []
        for field in ("panel_name", "display_order", "is_active", "default_config"):
            if field in d:
                setattr(panel, field, d[field])
                update_fields.append(field)
        with transaction.atomic():
            if update_fields:
                update_fields.append("updated_at")
                panel.save(update_fields=update_fields)

            AuditEvent.record(
                actor_id=(request.auth or {}).get("sub") or None,
                action="DASHBOARD_PANEL_UPDATED",
                entity_type="dashboard_panel",
                old_state=before,
                new_state={
                    field: getattr(panel, field)
                    for field in update_fields
                    if field != "updated_at"
                },
                ip_address=getattr(request, "ip_address", None),
                request_id=getattr(request, "request_id", None),
            )

        return _envelope(DashboardPanelSerializer(panel).data, request)
