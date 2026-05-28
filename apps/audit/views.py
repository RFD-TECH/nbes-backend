"""Auditor-facing endpoints."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone as py_timezone

from django.db.models import Q
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from shared.pagination import StandardResultsPagination
from shared.permissions import has_permission, has_permission_with_step_up

from .filters import build_audit_query
from .models import AuditEvent, DailyHashAnchor
from .serializers import AuditEventSerializer, DailyHashAnchorSerializer


def _yesterday_utc_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return inclusive UTC bounds for yesterday."""
    now = now or datetime.now(py_timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=py_timezone.utc)
    yesterday = now.astimezone(py_timezone.utc).date() - timedelta(days=1)
    start = datetime.combine(yesterday, time.min, tzinfo=py_timezone.utc)
    end = datetime.combine(yesterday, time.max, tzinfo=py_timezone.utc)
    return start, end


def _audit_export_query(params, *, default_bounds=None):
    """Build export query, defaulting an unbounded request to yesterday UTC."""
    q = build_audit_query(params)
    if not _has_export_window(params):
        start, end = default_bounds or _yesterday_utc_bounds()
        q &= Q(created_at__gte=start, created_at__lte=end)
    return q


def _has_export_window(params) -> bool:
    return any(params.get(name) for name in ("from", "to", "date_from", "date_to"))


def _meta_audit(request, *, action: str, new_state: dict) -> None:
    """Emit an audit event for access to the audit surfaces."""
    payload = request.auth or {}
    AuditEvent.record(
        actor_id=payload.get("sub") or None,
        action=action,
        entity_type="audit",
        new_state=new_state,
        ip_address=getattr(request, "ip_address", None),
        request_id=getattr(request, "request_id", None),
    )


class AuditSearchView(APIView):
    """``GET /api/v1/audit/search`` — filterable audit search."""

    permission_classes = [IsAuthenticated, has_permission("audit:search")]
    pagination_class = StandardResultsPagination

    @extend_schema(
        tags=["Audit"],
        summary="Search the audit trail",
        operation_id="audit_search",
        parameters=[
            OpenApiParameter("actor", str, description="Actor UUID (Keycloak sub)"),
            OpenApiParameter("actor_id", str, description="Legacy alias for actor"),
            OpenApiParameter("action", str, description="Action name, e.g. ITEM_APPROVED"),
            OpenApiParameter("entity_type", str, description="Entity type, e.g. item"),
            OpenApiParameter("entity_id", str, description="Entity UUID"),
            OpenApiParameter("request_id", str, description="Request correlation id"),
            OpenApiParameter("source_system", str, description="Source system tag"),
            OpenApiParameter("q", str, description="Free-text action/entity search"),
            OpenApiParameter("from", str, description="ISO-8601 lower bound"),
            OpenApiParameter("to", str, description="ISO-8601 upper bound"),
            OpenApiParameter("date_from", str, description="Legacy alias for from"),
            OpenApiParameter("date_to", str, description="Legacy alias for to"),
            OpenApiParameter("page", int, required=False),
            OpenApiParameter("page_size", int, required=False),
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
                            "request_id": serializers.CharField(),
                        },
                    ),
                },
            ),
        },
    )
    def get(self, request):
        q = build_audit_query(request.query_params)
        queryset = AuditEvent.objects.filter(q).order_by("-id")

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = AuditEventSerializer(page, many=True).data

        _meta_audit(
            request,
            action="AUDIT_SEARCH",
            new_state={
                "filters": {k: v for k, v in request.query_params.items() if v},
                "result_count": len(data),
            },
        )

        response = paginator.get_paginated_response(data)
        meta = response.data.setdefault("meta", {})
        meta["request_id"] = str(getattr(request, "request_id", ""))
        return response


class AuditChainView(APIView):
    """``GET /api/v1/audit/chain/{date}`` — hash-chain proof for one UTC day."""

    permission_classes = [IsAuthenticated, has_permission("audit:verify")]

    @extend_schema(
        tags=["Audit"],
        summary="Hash-chain proof for one UTC day",
        operation_id="audit_chain",
        description=(
            "Returns the day's anchored head hash and the System 22 anchor "
            "reference once notarisation completes."
        ),
        responses={
            200: DailyHashAnchorSerializer,
            404: inline_serializer(
                name="AuditChainNotFound",
                fields={
                    "success": serializers.BooleanField(default=False),
                    "error": inline_serializer(
                        name="AuditChainNotFoundError",
                        fields={
                            "code": serializers.CharField(),
                            "message": serializers.CharField(),
                        },
                    ),
                },
            ),
        },
        examples=[
            OpenApiExample(
                name="Anchored day",
                value={
                    "success": True,
                    "data": {
                        "date": "2026-05-19",
                        "head_event_id": "11111111-1111-1111-1111-111111111111",
                        "head_hash": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                        "event_count": 482,
                        "exported_to_s22_at": "2026-05-20T01:00:14Z",
                        "anchor_ref": "s22:anc:2026-05-19:9f86d08188",
                        "verifiable": True,
                        "created_at": "2026-05-20T01:00:00Z",
                    },
                    "meta": {"request_id": "..."},
                },
            ),
        ],
    )
    def get(self, request, date):
        target = parse_date(date)
        if target is None:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "date must be ISO-8601 (YYYY-MM-DD).",
                    },
                    "meta": {"request_id": str(getattr(request, "request_id", ""))},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            anchor = DailyHashAnchor.objects.get(date=target)
        except DailyHashAnchor.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "NOT_FOUND",
                        "message": (
                            f"No hash anchor exists for {target.isoformat()} yet. "
                            "The daily anchor task runs at 01:00 UTC the following day."
                        ),
                    },
                    "meta": {"request_id": str(getattr(request, "request_id", ""))},
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        _meta_audit(
            request,
            action="AUDIT_CHAIN_VIEWED",
            new_state={"date": target.isoformat()},
        )

        return Response(
            {
                "success": True,
                "data": DailyHashAnchorSerializer(anchor).data,
                "meta": {"request_id": str(getattr(request, "request_id", ""))},
            },
            status=status.HTTP_200_OK,
        )


class AuditExportView(APIView):
    """``GET /api/v1/audit/export?from=&to=`` — streamed NDJSON export."""

    permission_classes = [IsAuthenticated, has_permission_with_step_up("audit:export")]

    @extend_schema(
        tags=["Audit"],
        summary="Stream audit events as NDJSON",
        operation_id="audit_export",
        description=(
            "Streams matching AuditEvent rows as newline-delimited JSON. "
            "When no window is supplied, the export defaults to yesterday UTC."
        ),
        parameters=[
            OpenApiParameter("from", str, description="ISO-8601 lower bound"),
            OpenApiParameter("to", str, description="ISO-8601 upper bound"),
            OpenApiParameter("action", str, description="Optional action filter"),
            OpenApiParameter("entity_type", str, description="Optional entity_type filter"),
        ],
        responses={200: OpenApiResponse(description="NDJSON stream")},
    )
    def get(self, request):
        default_bounds = (
            _yesterday_utc_bounds()
            if not _has_export_window(request.query_params)
            else None
        )
        upper_bound = timezone.now()
        q = _audit_export_query(request.query_params, default_bounds=default_bounds)
        q &= Q(created_at__lte=upper_bound)
        queryset = AuditEvent.objects.filter(q).order_by("id").iterator(chunk_size=500)

        _meta_audit(
            request,
            action="AUDIT_EXPORT",
            new_state={
                "filters": {k: v for k, v in request.query_params.items() if v},
            },
        )

        def lines():
            from json import dumps

            for event in queryset:
                row = AuditEventSerializer(event).data
                yield dumps(row, default=str) + "\n"

        response = StreamingHttpResponse(lines(), content_type="application/x-ndjson")
        start = request.query_params.get("from") or request.query_params.get("date_from", "")
        end = request.query_params.get("to") or request.query_params.get("date_to", "")
        if default_bounds:
            default_start, default_end = default_bounds
            start = default_start.date().isoformat()
            end = default_end.date().isoformat()
        filename = f"audit-export-{start or 'all'}-{end or 'all'}.ndjson"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Request-ID"] = str(getattr(request, "request_id", ""))
        return response
