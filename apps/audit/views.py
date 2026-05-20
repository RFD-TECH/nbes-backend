"""apps/audit/views.py — Audit trail search and chain-hash export."""
import datetime

from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from shared.pagination import StandardResultsPagination
from shared.permissions import has_permission

from .filters import apply_audit_filters
from .models import AuditEvent
from .serializers import AuditEventSerializer


def _envelope(data, request_id=""):
    return Response({"success": True, "data": data, "meta": {"request_id": str(request_id)}})


def _error(code, message, http_status):
    return Response(
        {"success": False, "error": {"code": code, "message": message}, "meta": {}},
        status=http_status,
    )


class AuditSearchView(APIView):
    """``GET /api/v1/audit/search`` — paginated search over the audit log.

    Requires the ``audit:export`` permission (held by the ``auditor`` role).
    All filter parameters are optional and cumulative (AND logic).
    """
    permission_classes = [IsAuthenticated, has_permission("audit:export")]

    @extend_schema(
        tags=["Audit"],
        summary="Search audit events",
        operation_id="audit_event_search",
        description=(
            "Paginated, filterable view of the append-only audit log. "
            "All parameters are optional; multiple filters are ANDed. "
            "Results are ordered oldest-first (stable for pagination)."
        ),
        parameters=[
            OpenApiParameter("actor_id", str, description="Filter by Keycloak sub UUID"),
            OpenApiParameter("action", str, description="Exact action code, e.g. ITEM_APPROVED"),
            OpenApiParameter("entity_type", str, description="e.g. item, rbac, candidate"),
            OpenApiParameter("entity_id", str, description="UUID of the affected entity"),
            OpenApiParameter("date_from", str, description="Start date YYYY-MM-DD (inclusive)"),
            OpenApiParameter("date_to", str, description="End date YYYY-MM-DD (inclusive)"),
            OpenApiParameter("source_system", str, description="Source system tag, default 'nbes'"),
            OpenApiParameter("q", str, description="Free-text search on action / entity_type"),
            OpenApiParameter("page", int, description="Page number (default 1)"),
            OpenApiParameter("page_size", int, description="Results per page (default 20, max 200)"),
        ],
        responses={
            200: inline_serializer(
                name="AuditSearchResponse",
                fields={
                    "success": serializers.BooleanField(default=True),
                    "data": AuditEventSerializer(many=True),
                    "meta": inline_serializer(
                        name="AuditSearchMeta",
                        fields={
                            "page": serializers.IntegerField(),
                            "total": serializers.IntegerField(),
                            "pages": serializers.IntegerField(),
                        },
                    ),
                },
            ),
            401: inline_serializer(
                name="AuditSearchUnauthorized",
                fields={
                    "success": serializers.BooleanField(default=False),
                    "error": inline_serializer(
                        name="AuditSearchUnauthorizedError",
                        fields={"code": serializers.CharField(), "message": serializers.CharField()},
                    ),
                    "meta": serializers.DictField(),
                },
            ),
            403: inline_serializer(
                name="AuditSearchForbidden",
                fields={
                    "success": serializers.BooleanField(default=False),
                    "error": inline_serializer(
                        name="AuditSearchForbiddenError",
                        fields={"code": serializers.CharField(), "message": serializers.CharField()},
                    ),
                    "meta": serializers.DictField(),
                },
            ),
        },
    )
    def get(self, request):
        qs = apply_audit_filters(
            AuditEvent.objects.all().order_by("id"),
            request.query_params,
        )

        paginator = StandardResultsPagination()
        page = paginator.paginate_queryset(qs, request)
        if page is not None:
            serializer = AuditEventSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = AuditEventSerializer(qs, many=True)
        return _envelope(serializer.data, request_id=getattr(request, "request_id", ""))


class AuditChainView(APIView):
    """``GET /api/v1/audit/chain/{date}`` — chain-hash anchor for a UTC date.

    Returns the SHA-256 anchor of the last event recorded on *date* (YYYY-MM-DD).
    System 22 (the external audit archive) polls this nightly to verify
    chain continuity. If no events exist for the date, ``anchor_hash`` is null.
    """
    permission_classes = [IsAuthenticated, has_permission("audit:export")]

    @extend_schema(
        tags=["Audit"],
        summary="Get hash-chain anchor for a date",
        operation_id="audit_chain_retrieve",
        description=(
            "Returns the cumulative SHA-256 chain hash of the last audit event "
            "recorded on the given UTC date. Use this to verify chain continuity "
            "or to anchor daily exports to System 22."
        ),
        responses={
            200: inline_serializer(
                name="AuditChainResponse",
                fields={
                    "success": serializers.BooleanField(default=True),
                    "data": inline_serializer(
                        name="AuditChainData",
                        fields={
                            "date": serializers.DateField(),
                            "event_count": serializers.IntegerField(),
                            "first_event_id": serializers.UUIDField(allow_null=True),
                            "last_event_id": serializers.UUIDField(allow_null=True),
                            "anchor_hash": serializers.CharField(
                                allow_null=True,
                                help_text="SHA-256 chain hash of the last event on this date, or null.",
                            ),
                        },
                    ),
                    "meta": serializers.DictField(),
                },
            ),
            400: inline_serializer(
                name="AuditChainBadRequest",
                fields={
                    "success": serializers.BooleanField(default=False),
                    "error": inline_serializer(
                        name="AuditChainBadRequestError",
                        fields={"code": serializers.CharField(), "message": serializers.CharField()},
                    ),
                    "meta": serializers.DictField(),
                },
            ),
            401: inline_serializer(
                name="AuditChainUnauthorized",
                fields={
                    "success": serializers.BooleanField(default=False),
                    "error": inline_serializer(
                        name="AuditChainUnauthorizedError",
                        fields={"code": serializers.CharField(), "message": serializers.CharField()},
                    ),
                    "meta": serializers.DictField(),
                },
            ),
        },
    )
    def get(self, request, date_str):
        import hashlib
        import json
        from .models import DailyHashAnchor

        try:
            target = datetime.date.fromisoformat(date_str)
        except ValueError:
            return _error(
                "VALIDATION_ERROR",
                f"Invalid date '{date_str}'. Use YYYY-MM-DD.",
                status.HTTP_400_BAD_REQUEST,
            )

        qs = AuditEvent.objects.filter(created_at__date=target).order_by("id")
        count = qs.count()

        anchor = DailyHashAnchor.objects.filter(date=target).first()
        exported_to_s22_at = anchor.exported_to_s22_at.isoformat() if (anchor and anchor.exported_to_s22_at) else None
        anchor_ref = anchor.anchor_ref if anchor else ""

        if count == 0:
            return _envelope(
                {
                    "date": str(target),
                    "event_count": 0,
                    "first_event_id": None,
                    "last_event_id": None,
                    "anchor_hash": None,
                    "chain_valid": True,
                    "exported_to_s22_at": exported_to_s22_at,
                    "anchor_ref": anchor_ref,
                },
                request_id=getattr(request, "request_id", ""),
            )

        first = qs.values("event_id").first()
        last_row = qs.values("event_id", "chain_hash").last()

        # Verify chain integrity by replaying all hashes for the date
        chain_valid = True
        prev_hash = None
        for evt in qs.values("event_id", "actor_id", "action", "entity_type", "entity_id", "new_state", "created_at", "chain_hash"):
            payload = json.dumps({
                "event_id": str(evt["event_id"]),
                "actor_id": str(evt["actor_id"] or ""),
                "action": evt["action"],
                "entity_type": evt["entity_type"] or "",
                "entity_id": str(evt["entity_id"] or ""),
                "new_state": evt["new_state"] or {},
                "created_at": evt["created_at"].isoformat(),
            }, sort_keys=True)

            if prev_hash is None:
                # prev_hash for first event in the day is unknown without full chain scan;
                # we trust the stored hash for cross-day linking and just verify within-day links
                prev_hash = evt["chain_hash"]
                continue

            expected = hashlib.sha256(f"{prev_hash}{payload}".encode()).hexdigest()
            if expected != evt["chain_hash"]:
                chain_valid = False
                break
            prev_hash = evt["chain_hash"]

        return _envelope(
            {
                "date": str(target),
                "event_count": count,
                "first_event_id": str(first["event_id"]),
                "last_event_id": str(last_row["event_id"]),
                "anchor_hash": last_row["chain_hash"],
                "chain_valid": chain_valid,
                "exported_to_s22_at": exported_to_s22_at,
                "anchor_ref": anchor_ref,
            },
            request_id=getattr(request, "request_id", ""),
        )
