"""apps/users/views.py — NBES RBAC admin endpoints.
All endpoints require an authenticated user with the ``rbac:manage``
permission (held by ``system-administrator`` per the seed). Every mutation
emits an AuditEvent and invalidates the in-process role cache so the
change takes effect within 60 s for every NBES node.
"""
from django.db import transaction
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema,
    inline_serializer,
)
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import serializers
from rest_framework.views import APIView
from apps.audit.models import AuditEvent
from shared import rbac
from shared.permissions import has_permission

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


def _success_envelope(name, data_fields):
    return inline_serializer(
        name=name,
        fields={
            "success": serializers.BooleanField(default=True),
            "data": inline_serializer(name=f"{name}Data", fields=data_fields),
            "meta": inline_serializer(
                name=f"{name}Meta",
                fields={"request_id": serializers.CharField()},
            ),
        },
    )


def _success_envelope_with_serializer(name, data_serializer):
    return inline_serializer(
        name=name,
        fields={
            "success": serializers.BooleanField(default=True),
            "data": data_serializer,
            "meta": inline_serializer(
                name=f"{name}Meta",
                fields={"request_id": serializers.CharField()},
            ),
        },
    )


def _error_envelope(name):
    return inline_serializer(
        name=name,
        fields={
            "success": serializers.BooleanField(default=False),
            "error": inline_serializer(
                name=f"{name}Error",
                fields={
                    "code": serializers.CharField(),
                    "message": serializers.CharField(),
                },
            ),
            "meta": inline_serializer(
                name=f"{name}Meta",
                fields={"request_id": serializers.CharField()},
            ),
        },
    )


def _error_response(code, message, status_code, request_id=""):
    return Response(
        {
            "success": False,
            "error": {"code": code, "message": message},
            "meta": {"request_id": str(request_id)},
        },
        status=status_code,
    )


# ── permissions catalog (read-only) ──────────────────────────────────────────

class PermissionListView(APIView):
    """``GET /api/v1/admin/rbac/permissions`` — list seeded codenames.

    Read-only: codenames are declared in code, never invented at runtime.
    """
    authentication_classes_setting = None  # use DRF default
    permission_classes = [IsAuthenticated, has_permission("rbac:manage")]

    @extend_schema(
        tags=["RBAC Admin"],
        summary="List NBES permission codenames",
        operation_id="rbac_permission_list",
        description="Returns the seeded permission catalog. Codenames are declared in code.",
        responses={
            200: _success_envelope(
                "PermissionListResponse",
                {
                    "count": serializers.IntegerField(),
                    "permissions": PermissionSerializer(many=True),
                },
            ),
            401: _error_envelope("UnauthorizedError"),
            403: _error_envelope("ForbiddenError"),
        },
    )
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
    permission_classes = [IsAuthenticated, has_permission("rbac:manage")]

    @extend_schema(
        tags=["RBAC Admin"],
        summary="List NBES-recognised roles",
        operation_id="rbac_role_list",
        responses={
            200: _success_envelope(
                "RoleListResponse",
                {
                    "count": serializers.IntegerField(),
                    "roles": RoleSerializer(many=True),
                },
            ),
            401: _error_envelope("RoleListUnauthorizedError"),
            403: _error_envelope("RoleListForbiddenError"),
        },
    )
    def get(self, request):
        data = RoleSerializer(
            Role.objects.all().order_by("name"), many=True
        ).data
        return _envelope(
            {"count": len(data), "roles": data},
            request_id=getattr(request, "request_id", ""),
        )

    @extend_schema(
        tags=["RBAC Admin"],
        summary="Register an IAM role in NBES",
        operation_id="rbac_role_create",
        description="Creates or updates the local NBES role registry entry. It does not create the role in Keycloak.",
        request=CreateRoleSerializer,
        responses={
            200: _success_envelope_with_serializer("RoleUpdatedResponse", RoleSerializer()),
            201: _success_envelope_with_serializer("RoleCreatedResponse", RoleSerializer()),
            400: _error_envelope("CreateRoleValidationError"),
            401: _error_envelope("CreateRoleUnauthorizedError"),
            403: _error_envelope("CreateRoleForbiddenError"),
        },
    )
    def post(self, request):
        serializer = CreateRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        role, created = Role.objects.get_or_create(
            name=d["name"],
            defaults={"description": d.get("description", ""), "is_custom": True},
        )
        if not created and not role.is_custom:
            return _error_response(
                "ROLE_LOCKED",
                "Seeded NBES roles cannot be updated or deleted.",
                status.HTTP_403_FORBIDDEN,
                getattr(request, "request_id", ""),
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
    permission_classes = [IsAuthenticated, has_permission("rbac:manage")]

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

    @extend_schema(
        tags=["RBAC Admin"],
        summary="Get a role",
        operation_id="rbac_role_retrieve",
        responses={
            200: _success_envelope_with_serializer("RoleDetailResponse", RoleSerializer()),
            404: _error_envelope("RoleNotFoundError"),
        },
    )
    def get(self, request, pk):
        role, err = self._get(pk)
        if err:
            return err
        return _envelope(RoleSerializer(role).data, request_id=getattr(request, "request_id", ""))

    @extend_schema(
        tags=["RBAC Admin"],
        summary="Update a role",
        operation_id="rbac_role_update",
        request=inline_serializer(
            name="RolePatchRequest",
            fields={
                "description": serializers.CharField(required=False, allow_blank=True),
                "is_active": serializers.BooleanField(required=False),
            },
        ),
        responses={
            200: _success_envelope_with_serializer("RolePatchResponse", RoleSerializer()),
            404: _error_envelope("RolePatchNotFoundError"),
        },
    )
    def patch(self, request, pk):
        role, err = self._get(pk)
        if err:
            return err
        if not role.is_custom:
            return _error_response(
                "ROLE_LOCKED",
                "Seeded NBES roles cannot be updated or deleted.",
                status.HTTP_403_FORBIDDEN,
                getattr(request, "request_id", ""),
            )

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

    @extend_schema(
        tags=["RBAC Admin"],
        summary="Deactivate a role",
        operation_id="rbac_role_deactivate",
        responses={
            200: _success_envelope_with_serializer(
                "RoleDeactivateResponse",
                RoleSerializer(),
            ),
            404: _error_envelope("RoleDeactivateNotFoundError"),
        },
    )
    def delete(self, request, pk):
        role, err = self._get(pk)
        if err:
            return err
        if not role.is_custom:
            return _error_response(
                "ROLE_LOCKED",
                "Seeded NBES roles cannot be updated or deleted.",
                status.HTTP_403_FORBIDDEN,
                getattr(request, "request_id", ""),
            )

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
    permission_classes = [IsAuthenticated, has_permission("rbac:manage")]

    @extend_schema(
        tags=["RBAC Admin"],
        summary="Get role permission grants",
        operation_id="rbac_role_permissions_retrieve",
        responses={
            200: _success_envelope(
                "RolePermissionsResponse",
                {
                    "role_id": serializers.UUIDField(),
                    "role_name": serializers.CharField(),
                    "permissions": serializers.ListField(child=serializers.CharField()),
                },
            ),
            404: _error_envelope("RolePermissionsNotFoundError"),
        },
    )
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

    @extend_schema(
        tags=["RBAC Admin"],
        summary="Replace role permission grants",
        operation_id="rbac_role_permissions_update",
        request=UpdateRolePermissionsSerializer,
        responses={
            200: _success_envelope(
                "RolePermissionsUpdateResponse",
                {
                    "role_id": serializers.UUIDField(),
                    "role_name": serializers.CharField(),
                    "permissions": serializers.ListField(child=serializers.CharField()),
                    "added": serializers.ListField(child=serializers.CharField()),
                    "removed": serializers.ListField(child=serializers.CharField()),
                },
            ),
            400: _error_envelope("RolePermissionsValidationError"),
            404: _error_envelope("RolePermissionsUpdateNotFoundError"),
        },
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

    **Role resolution (post-migration architecture):**

    1. NBES reads ``resource_access[<NBES_CLIENT_ID>].roles`` from the JWT —
       the Keycloak *client roles* IAM assigns when the user is invited /
       activated into NBES. ``NBES_CLIENT_ID`` defaults to ``nbes-api``.
    2. If that claim is absent (legacy tokens issued before IAM cut over),
       NBES falls back to ``realm_access.roles`` and emits a structured
       warning ``rbac.legacy_realm_role_fallback`` so the fallback usage is
       observable. The fallback will be removed in IAM Phase 7.
    3. ``super_admin`` in ``realm_access.roles`` short-circuits to the
       wildcard sentinel ``*``. This realm role stays a realm role; every
       other system role becomes a client role.

    **Audience verification.** Tokens for NBES must list ``nbes-api`` in
    ``aud`` (the ``audience-resolve`` mapper on ``clet-iam-internal``
    populates this automatically when the user holds NBES client roles).
    Production NBES (``prod.py``) fails closed at boot if
    ``KEYCLOAK_VALID_AUDIENCES`` is empty or missing ``nbes-api``.

    The ``roles_in_jwt`` field in the response reflects whichever source
    the resolver actually used — so an empty ``resource_access`` block on a
    legacy token will surface here as the realm-role names.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Current User"],
        summary="Get current user's resolved NBES permissions",
        operation_id="current_user_permissions",
        description=(
            "Returns the resolved NBES permission set for the bearer of "
            "the JWT.\n\n"
            "**Resolution order:**\n"
            "1. `resource_access[nbes-api].roles` — Keycloak client roles "
            "(target architecture).\n"
            "2. `realm_access.roles` — legacy fallback; logged for "
            "migration tracking.\n"
            "3. `super_admin` in `realm_access.roles` → wildcard `*`.\n\n"
            "**Audience:** the token's `aud` claim must include the value "
            "of `NBES_CLIENT_ID` (default `nbes-api`)."
        ),
        responses={
            200: _success_envelope(
                "MyPermissionsResponse",
                {
                    "sub": serializers.CharField(allow_blank=True, required=False),
                    "email": serializers.EmailField(allow_blank=True, required=False),
                    "roles_in_jwt": serializers.ListField(child=serializers.CharField()),
                    "roles_recognised_by_nbes": serializers.ListField(
                        child=serializers.CharField()
                    ),
                    "permissions": serializers.ListField(child=serializers.CharField()),
                },
            ),
            401: _error_envelope("MyPermissionsUnauthorizedError"),
        },
        examples=[
            OpenApiExample(
                name="Examiner — client-role path (target)",
                description=(
                    "User has `nbes-api/examiner` as a Keycloak client role. "
                    "Token's `resource_access['nbes-api'].roles` includes "
                    "`examiner`. NBES resolves `examiner` against its local "
                    "RolePermission table."
                ),
                value={
                    "success": True,
                    "data": {
                        "sub": "11111111-1111-1111-1111-111111111111",
                        "email": "examiner@example.com",
                        "roles_in_jwt": ["examiner"],
                        "roles_recognised_by_nbes": ["examiner"],
                        "permissions": [
                            "marking:moderate",
                            "marking:score"
                        ],
                    },
                    "meta": {"request_id": "0e1e..."},
                },
            ),
            OpenApiExample(
                name="Super admin — realm-role wildcard",
                description=(
                    "`super_admin` in `realm_access.roles` short-circuits "
                    "to wildcard. Token does not need `resource_access[nbes-api]`."
                ),
                value={
                    "success": True,
                    "data": {
                        "sub": "22222222-2222-2222-2222-222222222222",
                        "email": "root@example.com",
                        "roles_in_jwt": [],
                        "roles_recognised_by_nbes": [],
                        "permissions": ["*"],
                    },
                    "meta": {"request_id": "..."},
                },
            ),
            OpenApiExample(
                name="Legacy fallback — pre-migration token",
                description=(
                    "Token carries `realm_access.roles=['examiner']` but no "
                    "`resource_access[nbes-api]`. NBES still resolves it via "
                    "the realm-role fallback path AND emits "
                    "`rbac.legacy_realm_role_fallback` to logs."
                ),
                value={
                    "success": True,
                    "data": {
                        "sub": "33333333-3333-3333-3333-333333333333",
                        "email": "legacy@example.com",
                        "roles_in_jwt": ["examiner"],
                        "roles_recognised_by_nbes": ["examiner"],
                        "permissions": ["marking:moderate", "marking:score"],
                    },
                    "meta": {"request_id": "..."},
                },
            ),
        ],
    )
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


# ── role dashboard skeletons ────────────────────────────────────────────────

_DASHBOARD_PANELS = {
    "nbec-member": [
        {"id": "meeting_agenda", "title": "Meeting Agenda"},
        {"id": "pending_approvals", "title": "Pending Approvals"},
        {"id": "conflict_declarations", "title": "Conflict Declarations"},
        {"id": "audit_trail_viewer", "title": "Audit Trail"},
    ],
    "nbec-secretariat": [
        {"id": "committee_operations", "title": "Committee Operations"},
        {"id": "candidate_registration_desk", "title": "Candidate Registration Desk"},
        {"id": "exception_queue", "title": "Exception Queue"},
    ],
    "item-writer": [
        {"id": "my_items", "title": "My Items"},
        {"id": "drafts", "title": "Drafts"},
        {"id": "peer_review_feedback", "title": "Peer Review Feedback"},
    ],
    "moderator": [
        {"id": "review_queue", "title": "Review Queue"},
        {"id": "panel_decisions", "title": "Panel Decisions"},
        {"id": "item_search", "title": "Item Search"},
    ],
    "examiner": [
        {"id": "marking_queue", "title": "Marking Queue"},
        {"id": "borderline_review_queue", "title": "Borderline Review Queue"},
    ],
    "candidate": [
        {"id": "registration", "title": "Registration"},
        {"id": "payment", "title": "Payment"},
        {"id": "slip", "title": "Admission Slip"},
        {"id": "results", "title": "Results"},
        {"id": "remarking", "title": "Remarking"},
    ],
    "clet-registrar": [
        {"id": "override_queue", "title": "Override Queue"},
        {"id": "ratification_dashboard", "title": "Ratification Dashboard"},
        {"id": "cert_trigger_panel", "title": "Certificate Trigger Panel"},
    ],
    "invigilator": [
        {"id": "centre_operations", "title": "Centre Operations"},
        {"id": "candidate_checkin", "title": "Candidate Check-In"},
        {"id": "proctoring_queue", "title": "Proctoring Queue"},
    ],
    "centre-coordinator": [
        {"id": "centre_operations", "title": "Centre Operations"},
        {"id": "candidate_checkin", "title": "Candidate Check-In"},
        {"id": "proctoring_queue", "title": "Proctoring Queue"},
    ],
    "system-administrator": [
        {"id": "users", "title": "Users"},
        {"id": "roles", "title": "Roles"},
        {"id": "integrations", "title": "Integrations"},
        {"id": "audit", "title": "Audit"},
        {"id": "system_health", "title": "System Health"},
    ],
    "auditor": [
        {"id": "audit_trail_search", "title": "Audit Trail Search"},
        {"id": "hash_chain_verifier", "title": "Hash-Chain Verifier"},
        {"id": "export", "title": "Export"},
    ],
}


class DashboardView(APIView):
    """``GET /api/v1/me/dashboard`` — role dashboard skeleton for the current user.

    Returns an empty-state panel list for the user's primary role.
    Panels are populated by feature phases; Phase 1 ships the structure only.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Current User"],
        summary="Get role dashboard skeleton",
        operation_id="current_user_dashboard",
        responses={
            200: _success_envelope(
                "DashboardResponse",
                {
                    "role": serializers.CharField(allow_blank=True),
                    "panels": serializers.ListField(
                        child=inline_serializer(
                            name="DashboardPanel",
                            fields={
                                "id": serializers.CharField(),
                                "title": serializers.CharField(),
                                "data": serializers.JSONField(default=None, allow_null=True),
                                "status": serializers.CharField(default="not_implemented"),
                            },
                        )
                    ),
                },
            ),
            401: _error_envelope("DashboardUnauthorizedError"),
        },
    )
    def get(self, request):
        payload = request.auth or {}
        roles = rbac.get_nbes_role_names(payload)
        primary_role = roles[0] if roles else ""

        raw_panels = _DASHBOARD_PANELS.get(primary_role, [])
        panels = [
            {"id": p["id"], "title": p["title"], "data": None, "status": "not_implemented"}
            for p in raw_panels
        ]

        return _envelope(
            {"role": primary_role, "panels": panels},
            request_id=getattr(request, "request_id", ""),
        )
