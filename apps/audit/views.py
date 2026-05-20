"""apps/audit/views.py — Auditor-facing endpoints.

Three surfaces:

* ``GET /api/v1/audit/search`` — paginated, filterable search over
  ``AuditEvent``. Gated by ``audit:search``.
* ``GET /api/v1/audit/chain/{date}`` — hash-chain proof for a UTC day,
  including the System 22 anchor reference once notarisation completes.
  Gated by ``audit:verify``.
* ``GET /api/v1/audit/export`` — streamed NDJSON export (one event per
  line). Gated by ``audit:export``. The Auditor uses this to produce a
  signed bundle for judicial review.

Every successful call emits an ``AUDIT_SEARCH`` / ``AUDIT_CHAIN_VIEWED``
/ ``AUDIT_EXPORT`` event so we have a meta-audit of who looked at what —
required by NBE-N02.
"""
from __future__ import annotations

from datetime import datetime, time, timezone as py_timezone

from django.http import StreamingHttpResponse
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
from shared.permissions import has_permission

from .filters import build_audit_query, _parse_iso_date
from .models import AuditEvent, DailyHashAnchor
from .serializers import AuditEventSerializer, DailyHashAnchorSerializer


def _meta_audit(request, *, action: str, new_state: dict) -> None:
    """Emit a meta-audit event — who queried what. Lazy import to avoid
    circular load through ``shared.permissions`` at module import."""
    payload = request.auth or {}
    AuditEvent.record(
        actor_id=payload.get("sub") or None,
        action=action,
        entity_type="audit",
        new_state=new_state,
        ip_address=getattr(request, "ip_address", None),
        request_id=getattr(request, "request_id", None),
    )


# ──────────────────────────────────────────────────────────────────────────
# /api/v1/audit/search
# ──────────────────────────────────────────────────────────────────────────

class AuditSearchView(APIView):
    """``GET /api/v1/audit/search`` — filterable audit search."""

    permission_classes = [IsAuthenticated, has_permission("audit:search")]
    pagination_class = StandardResultsPagination

    @extend_schema(
        tags=["Audit"],
        summary="Search the audit trail",
        operation_id="audit_search",
        description=(
            "Returns a paginated slice of AuditEvent rows matching the "
            "supplied filters. All filters are optional; an unfiltered call "
            "returns the most recent events first. Emits an "
            "``AUDIT_SEARCH`` event on every call so meta-access is itself "
            "auditable."
        ),
        parameters=[
            OpenApiParameter("actor", str, description="Actor UUID (Keycloak sub)"),
            OpenApiParameter("action", str, description="Action name, e.g. ITEM_APPROVED"),
            OpenApiParameter("entity_type", str, description="Entity type, e.g. item"),
            OpenApiParameter("entity_id", str, description="Entity UUID"),
            OpenApiParameter("request_id", str, description="Request correlation id"),
            OpenApiParameter("from", str, description="ISO-8601 lower bound (inclusive)"),
            OpenApiParameter("to", str, description="ISO-8601 upper bound (inclusive)"),
            OpenApiParameter("page", int, required=False),
            OpenApiParameter("page_size", int, required=False),
        ],
        responses={
            200: inline_serializer(
                name="AuditSearchPaginatedResponse",
                fields={
                    "success": serializers.BooleanField(),
                    "data": AuditEventSerializer(many=True),
                    "meta": inline_serializer(
                        name="AuditSearchPaginationMeta",
                        fields={
                            "page": serializers.IntegerField(required=False),
                            "page_size": serializers.IntegerField(required=False),
                            "count": serializers.IntegerField(required=False),
                            "num_pages": serializers.IntegerField(required=False),
                            "next": serializers.CharField(required=False, allow_null=True),
                            "previous": serializers.CharField(required=False, allow_null=True),
                            "request_id": serializers.CharField(required=False, allow_blank=True),
                        },
                    ),
                },
            )
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


# ──────────────────────────────────────────────────────────────────────────
# /api/v1/audit/chain/{date}
# ──────────────────────────────────────────────────────────────────────────

class AuditChainView(APIView):
    """``GET /api/v1/audit/chain/{date}`` — hash-chain proof for one UTC day."""

    permission_classes = [IsAuthenticated, has_permission("audit:verify")]

    @extend_schema(
        tags=["Audit"],
        summary="Hash-chain proof for one UTC day",
        operation_id="audit_chain",
        description=(
            "Returns the day's head hash and (once notarised) the anchor "
            "reference returned by System 22.\n\n"
            "**Verification procedure:**\n"
            "1. Pull all AuditEvent rows where ``created_at`` falls on the "
            "requested UTC day, ordered by ``id``.\n"
            "2. Re-derive the chain hash starting from the previous day's "
            "``head_hash`` (or the 64-zero genesis for day 1).\n"
            "3. Compare the final hash to ``head_hash`` returned here.\n"
            "4. Cross-check ``anchor_ref`` against System 22's tamper-evident "
            "store using its published public key."
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
            OpenApiExample(
                name="Day with no events",
                value={
                    "success": True,
                    "data": {
                        "date": "2026-05-18",
                        "head_event_id": None,
                        "head_hash": "0000000000000000000000000000000000000000000000000000000000000000",
                        "event_count": 0,
                        "exported_to_s22_at": None,
                        "anchor_ref": "",
                        "verifiable": False,
                        "created_at": "2026-05-19T01:00:00Z",
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


# ──────────────────────────────────────────────────────────────────────────
# /api/v1/audit/export
# ──────────────────────────────────────────────────────────────────────────

class AuditExportView(APIView):
    """``GET /api/v1/audit/export?from=&to=`` — streamed NDJSON export.

    Each line is one AuditEvent serialised as JSON. The default window is
    yesterday. The endpoint streams rather than buffering so multi-year
    exports for judicial review don't OOM the worker.
    """
    permission_classes = [IsAuthenticated, has_permission("audit:export")]

    @extend_schema(
        tags=["Audit"],
        summary="Stream audit events as NDJSON",
        operation_id="audit_export",
        description=(
            "Streams matching AuditEvent rows as newline-delimited JSON "
            "(one event per line). Suitable for judicial-review bundles. "
            "Emits an ``AUDIT_EXPORT`` event on every call."
        ),
        parameters=[
            OpenApiParameter("from", str, description="ISO-8601 lower bound (inclusive)"),
            OpenApiParameter("to", str, description="ISO-8601 upper bound (inclusive)"),
            OpenApiParameter("action", str, description="Optional action filter"),
            OpenApiParameter("entity_type", str, description="Optional entity_type filter"),
        ],
        responses={200: OpenApiResponse(description="NDJSON stream")},
    )
    def get(self, request):
        q = build_audit_query(request.query_params)
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
        # Suggest a filename so curl -OJ saves it cleanly.
        start = request.query_params.get("from", "")
        end = request.query_params.get("to", "")
        filename = f"audit-export-{start or 'all'}-{end or 'all'}.ndjson"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Request-ID"] = str(getattr(request, "request_id", ""))
        return response
