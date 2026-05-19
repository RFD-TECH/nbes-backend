"""apps/users/views.py — NBES RBAC admin endpoints.

All endpoints require an authenticated user with the ``rbac:manage``
permission (held by ``system-administrator`` per the seed). Every mutation
emits an AuditEvent and invalidates the in-process role cache so the
change takes effect within 60 s for every NBES node.
"""
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from shared import rbac
from shared.permissions import HasPermission

from .models import Permission, Role, RolePermission
from .serializers import (
    CreateRoleSerializer,
    PermissionSerializer,
    RoleSerializer,
    UpdateRolePermissionsSerializer,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _envelope(data, status_code=status.HTTP_200_OK, request_id=""):
    """NBES success envelope. Errors go through shared.exceptions handler."""
    return Response(
        {"success": True, "data": data, "meta": {"request_id": str(request_id)}},
        status=status_code,
    )


def _actor_id(request):
    return (request.auth or {}).get("sub") or None


def _audit(request, action, entity_id=None, new_state=None, old_state=None):
    AuditEvent.record(
        actor_id=_actor_id(request),
        action=action,
        entity_type="rbac",
        entity_id=entity_id,
        old_state=old_state,
        new_state=new_state,
        request_id=getattr(request, "request_id", None),
        ip_address=getattr(request, "ip_address", None),
    )


def _rbac_manage():
    """Permission-class factory bound to ``rbac:manage``."""
    class _RbacManage(HasPermission):
        def __init__(self):
            super().__init__("rbac:manage")
    return _RbacManage


# ── permissions catalog (read-only) ──────────────────────────────────────────

class PermissionListView(APIView):
    """``GET /api/v1/admin/rbac/permissions`` — list seeded codenames.

    Read-only: codenames are declared in code, never invented at runtime.
    """
    authentication_classes_setting = None  # use DRF default
    permission_classes = [IsAuthenticated, _rbac_manage()]

    def get(self, request):
        data = PermissionSerializer(
            Permission.objects.all().order_by("codename"), many=True
        ).data
        return _envelope(
            {"count": len(data), "permissions": data},
            request_id=getattr(request, "request_id", ""),
        )


# ── role registry (mirror of IAM roles NBES recognises) ─────────────────────

class RoleListCreateView(APIView):
    """``GET /api/v1/admin/rbac/roles`` — list registered roles.

    ``POST /api/v1/admin/rbac/roles`` — register an IAM role name so NBES
    starts recognising it. Does NOT create the role in Keycloak; that
    happens in IAM. NBES will only resolve permissions for roles whose
    names are in this table.
    """
    permission_classes = [IsAuthenticated, _rbac_manage()]

    def get(self, request):
        data = RoleSerializer(
            Role.objects.all().order_by("name"), many=True
        ).data
        return _envelope(
            {"count": len(data), "roles": data},
            request_id=getattr(request, "request_id", ""),
        )

    def post(self, request):
        serializer = CreateRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        role, created = Role.objects.get_or_create(
            name=d["name"],
            defaults={"description": d.get("description", "")},
        )
        if not created and d.get("description") and not role.description:
            role.description = d["description"]
            role.save(update_fields=["description", "updated_at"])

        _audit(
            request,
            action="RBAC_ROLE_REGISTERED" if created else "RBAC_ROLE_UPDATED",
            entity_id=role.id,
            new_state={"name": role.name, "description": role.description},
        )
        return _envelope(
            RoleSerializer(role).data,
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            request_id=getattr(request, "request_id", ""),
        )


class RoleDetailView(APIView):
    """``GET/PATCH/DELETE /api/v1/admin/rbac/roles/{id}``.

    DELETE only deactivates (``is_active=False``) so historical audit rows
    keep referring to a real role. The local cache is invalidated so
    revocations take effect within 60 s.
    """
    permission_classes = [IsAuthenticated, _rbac_manage()]

    def _get(self, pk):
        try:
            return Role.objects.get(id=pk), None
        except Role.DoesNotExist:
            return None, Response(
                {
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "Role not found."},
                    "meta": {},
                },
                status=status.HTTP_404_NOT_FOUND,
            )

    def get(self, request, pk):
        role, err = self._get(pk)
        if err:
            return err
        return _envelope(RoleSerializer(role).data, request_id=getattr(request, "request_id", ""))

    def patch(self, request, pk):
        role, err = self._get(pk)
        if err:
            return err

        before = {"description": role.description, "is_active": role.is_active}
        if "description" in request.data:
            role.description = request.data["description"]
        if "is_active" in request.data:
            role.is_active = bool(request.data["is_active"])
        role.save(update_fields=["description", "is_active", "updated_at"])
        rbac.invalidate_role(role.name)

        _audit(
            request,
            action="RBAC_ROLE_UPDATED",
            entity_id=role.id,
            old_state=before,
            new_state={"description": role.description, "is_active": role.is_active},
        )
        return _envelope(RoleSerializer(role).data, request_id=getattr(request, "request_id", ""))

    def delete(self, request, pk):
        role, err = self._get(pk)
        if err:
            return err

        if not role.is_active:
            return _envelope(
                {"detail": "Role is already inactive."},
                request_id=getattr(request, "request_id", ""),
            )

        role.is_active = False
        role.save(update_fields=["is_active", "updated_at"])
        rbac.invalidate_role(role.name)

        _audit(
            request,
            action="RBAC_ROLE_DEACTIVATED",
            entity_id=role.id,
            old_state={"is_active": True},
            new_state={"is_active": False},
        )
        return _envelope(RoleSerializer(role).data, request_id=getattr(request, "request_id", ""))


# ── role ↔ permission matrix (the editable bit) ─────────────────────────────

class RolePermissionsView(APIView):
    """``GET /api/v1/admin/rbac/roles/{id}/permissions`` — current grants.

    ``PUT /api/v1/admin/rbac/roles/{id}/permissions`` — replace with the
    given set. Computes additions/removals, persists in one transaction,
    invalidates the role cache.
    """
    permission_classes = [IsAuthenticated, _rbac_manage()]

    def get(self, request, pk):
        try:
            role = Role.objects.get(id=pk)
        except Role.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "Role not found."},
                    "meta": {},
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        codenames = sorted(
            role.grants.values_list("permission__codename", flat=True)
        )
        return _envelope(
            {"role_id": str(role.id), "role_name": role.name, "permissions": codenames},
            request_id=getattr(request, "request_id", ""),
        )

    def put(self, request, pk):
        try:
            role = Role.objects.get(id=pk)
        except Role.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "Role not found."},
                    "meta": {},
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = UpdateRolePermissionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        desired = set(serializer.validated_data["codenames"])

        with transaction.atomic():
            current = set(role.grants.values_list("permission__codename", flat=True))
            to_remove = current - desired
            to_add = desired - current

            if to_remove:
                role.grants.filter(permission__codename__in=to_remove).delete()
            if to_add:
                permissions = Permission.objects.filter(codename__in=to_add)
                RolePermission.objects.bulk_create(
                    [
                        RolePermission(role=role, permission=p, granted_by=_actor_id(request))
                        for p in permissions
                    ]
                )

        rbac.invalidate_role(role.name)
        _audit(
            request,
            action="RBAC_ROLE_PERMISSIONS_UPDATED",
            entity_id=role.id,
            old_state={"permissions": sorted(current)},
            new_state={"permissions": sorted(desired)},
        )

        return _envelope(
            {
                "role_id": str(role.id),
                "role_name": role.name,
                "permissions": sorted(desired),
                "added": sorted(to_add),
                "removed": sorted(to_remove),
            },
            request_id=getattr(request, "request_id", ""),
        )


# ── current-user introspection ──────────────────────────────────────────────

class MyPermissionsView(APIView):
    """``GET /api/v1/me/permissions`` — what NBES thinks the current user can do.

    Useful for clients to hide buttons before the API rejects them. The
    list reflects the *resolved* permissions after intersecting JWT roles
    with NBES's role registry — i.e. the same set the gateway enforces.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        payload = request.auth or {}
        jwt_roles = rbac.get_nbes_role_names(payload)
        known_roles = sorted(
            Role.objects.filter(name__in=jwt_roles, is_active=True)
            .values_list("name", flat=True)
        )
        permissions = sorted(rbac.permissions_for(payload))
        return _envelope(
            {
                "sub": payload.get("sub"),
                "email": payload.get("email"),
                "roles_in_jwt": jwt_roles,
                "roles_recognised_by_nbes": known_roles,
                "permissions": permissions,
            },
            request_id=getattr(request, "request_id", ""),
        )
