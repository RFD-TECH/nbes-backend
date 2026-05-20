"""apps/audit/secops_views.py — Security Operations Console endpoints.

Mounted at ``/api/v1/secops/`` by ``config/urls.py``. Every endpoint is
behind ``secops:view``, granted to ``security_officer`` and
``system_administrator``.

Four surfaces (blueprint §1.2.6 / §1.12 "Security Operations Console"):

* ``GET /api/v1/secops/auth-failures``  — counts × top IPs in a window
* ``GET /api/v1/secops/throttled-ips``  — currently throttled / blocked
* ``GET /api/v1/secops/anomalies``      — placeholder; detector is COULD
* ``GET /api/v1/secops/daily-summary``  — fixed-day rollup

Numbers come from the ``SecurityEvent`` hot table populated by
``shared.secops.record_security_event`` and the edge throttle middleware.
Cold storage lives in System 22.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone as py_timezone

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_date
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

from shared.permissions import has_permission

from .models import SecurityEvent


_WINDOW_TO_SECONDS = {
    "15m": 15 * 60,
    "24h": 24 * 60 * 60,
    "7d":  7 * 24 * 60 * 60,
}


def _envelope(data, request, status_code=status.HTTP_200_OK):
    return Response(
        {
            "success": True,
            "data": data,
            "meta": {"request_id": str(getattr(request, "request_id", ""))},
        },
        status=status_code,
    )


# ──────────────────────────────────────────────────────────────────────────
# /api/v1/secops/auth-failures
# ──────────────────────────────────────────────────────────────────────────

class AuthFailuresView(APIView):
    permission_classes = [IsAuthenticated, has_permission("secops:view")]

    @extend_schema(
        tags=["Security Operations"],
        summary="Authentication-failure counts in a window",
        operation_id="secops_auth_failures",
        description=(
            "Counts of SecurityEvent rows in the chosen window, bucketed "
            "by category, with the top 10 originating IPs. Use this on "
            "the SOC dashboard as the headline tile."
        ),
        parameters=[
            OpenApiParameter(
                "window", str,
                description="One of 15m | 24h | 7d (default 24h).",
                required=False,
            ),
        ],
        responses={
            200: inline_serializer(
                name="AuthFailuresResponse",
                fields={
                    "success": serializers.BooleanField(default=True),
                    "data": inline_serializer(
                        name="AuthFailuresData",
                        fields={
                            "window": serializers.CharField(),
                            "window_seconds": serializers.IntegerField(),
                            "since": serializers.DateTimeField(),
                            "total": serializers.IntegerField(),
                            "by_category": serializers.DictField(child=serializers.IntegerField()),
                            "top_ips": serializers.ListField(
                                child=inline_serializer(
                                    name="TopIPRow",
                                    fields={
                                        "ip": serializers.CharField(),
                                        "count": serializers.IntegerField(),
                                    },
                                ),
                            ),
                        },
                    ),
                    "meta": inline_serializer(
                        name="AuthFailuresMeta",
                        fields={"request_id": serializers.CharField()},
                    ),
                },
            ),
        },
    )
    def get(self, request):
        window = request.query_params.get("window", "24h")
        seconds = _WINDOW_TO_SECONDS.get(window)
        if seconds is None:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "window must be one of 15m, 24h, 7d.",
                    },
                    "meta": {"request_id": str(getattr(request, "request_id", ""))},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        since = timezone.now() - timedelta(seconds=seconds)
        queryset = SecurityEvent.objects.filter(occurred_at__gte=since)
        by_category = dict(
            queryset.values_list("category")
            .annotate(c=Count("id"))
            .values_list("category", "c")
        )
        top_ips = list(
            queryset.exclude(ip_address__isnull=True)
            .values("ip_address")
            .annotate(c=Count("id"))
            .order_by("-c")[:10]
        )

        return _envelope(
            {
                "window": window,
                "window_seconds": seconds,
                "since": since,
                "total": queryset.count(),
                "by_category": by_category,
                "top_ips": [
                    {"ip": row["ip_address"], "count": row["c"]} for row in top_ips
                ],
            },
            request,
        )


# ──────────────────────────────────────────────────────────────────────────
# /api/v1/secops/throttled-ips
# ──────────────────────────────────────────────────────────────────────────

class ThrottledIPsView(APIView):
    """Lists the IPs the edge throttle has actioned in the last 24 hours,
    indicating which are currently throttled / blocked and what their
    remaining retry window is."""
    permission_classes = [IsAuthenticated, has_permission("secops:view")]

    @extend_schema(
        tags=["Security Operations"],
        summary="Currently throttled or blocked IPs",
        operation_id="secops_throttled_ips",
        description=(
            "Returns SecurityEvent rows of category ``throttle_applied`` "
            "or ``ip_blocked`` from the last 24h, joined with the live "
            "Redis sentinel that tracks whether the throttle/block is "
            "still active and how long until it lifts."
        ),
        responses={200: inline_serializer(
            name="ThrottledIPsResponse",
            fields={
                "success": serializers.BooleanField(default=True),
                "data": serializers.ListField(
                    child=inline_serializer(
                        name="ThrottledIPRow",
                        fields={
                            "ip": serializers.CharField(),
                            "category": serializers.CharField(),
                            "occurred_at": serializers.DateTimeField(),
                            "active": serializers.BooleanField(),
                            "retry_after_seconds": serializers.IntegerField(allow_null=True),
                            "indicators": serializers.DictField(),
                        },
                    ),
                ),
                "meta": inline_serializer(
                    name="ThrottledIPsMeta",
                    fields={"request_id": serializers.CharField()},
                ),
            },
        )},
    )
    def get(self, request):
        since = timezone.now() - timedelta(hours=24)
        rows = (
            SecurityEvent.objects.filter(
                category__in=["throttle_applied", "ip_blocked"],
                occurred_at__gte=since,
            )
            .exclude(ip_address__isnull=True)
            .order_by("-occurred_at")[:200]
        )

        seen: dict[tuple[str, str], dict] = {}
        for ev in rows:
            key = (ev.ip_address, ev.category)
            if key in seen:
                continue
            sentinel = (
                f"nbes:edge:block-active:{ev.ip_address}"
                if ev.category == "ip_blocked"
                else f"nbes:edge:throttle-active:{ev.ip_address}"
            )
            active = bool(cache.get(sentinel))
            retry_after = None
            if active and hasattr(cache, "ttl"):
                try:
                    retry_after = cache.ttl(sentinel)
                except Exception:
                    retry_after = None
            seen[key] = {
                "ip": ev.ip_address,
                "category": ev.category,
                "occurred_at": ev.occurred_at,
                "active": active,
                "retry_after_seconds": retry_after,
                "indicators": ev.indicators or {},
            }

        # Show blocks before plain throttles.
        result = sorted(
            seen.values(),
            key=lambda row: (0 if row["category"] == "ip_blocked" else 1, row["occurred_at"]),
            reverse=True,
        )
        return _envelope(result, request)


# ──────────────────────────────────────────────────────────────────────────
# /api/v1/secops/anomalies — placeholder
# ──────────────────────────────────────────────────────────────────────────

class AnomaliesView(APIView):
    """Anomaly detector is a COULD per blueprint §1.2.6 / §1.9. Endpoint
    exists so the dashboard contract is stable; payload is an empty list
    until the detector lands."""
    permission_classes = [IsAuthenticated, has_permission("secops:view")]

    @extend_schema(
        tags=["Security Operations"],
        summary="Anomalous login patterns (placeholder)",
        operation_id="secops_anomalies",
        description=(
            "Returns ``anomaly_detected`` SecurityEvent rows from the last "
            "24h. The detector itself is COULD per blueprint §1.9 and not "
            "shipped in Sprint 1.3 — this endpoint exists so the dashboard "
            "contract is stable from day one."
        ),
        responses={200: inline_serializer(
            name="AnomaliesResponse",
            fields={
                "success": serializers.BooleanField(default=True),
                "data": serializers.ListField(child=serializers.DictField()),
                "meta": inline_serializer(
                    name="AnomaliesMeta",
                    fields={"request_id": serializers.CharField()},
                ),
            },
        )},
    )
    def get(self, request):
        since = timezone.now() - timedelta(hours=24)
        rows = (
            SecurityEvent.objects.filter(
                category="anomaly_detected", occurred_at__gte=since,
            )
            .order_by("-occurred_at")[:200]
        )
        return _envelope(
            [
                {
                    "event_id": str(r.event_id),
                    "ip": r.ip_address,
                    "actor_id": str(r.actor_id) if r.actor_id else None,
                    "occurred_at": r.occurred_at,
                    "indicators": r.indicators or {},
                }
                for r in rows
            ],
            request,
        )


# ──────────────────────────────────────────────────────────────────────────
# /api/v1/secops/daily-summary
# ──────────────────────────────────────────────────────────────────────────

class DailySummaryView(APIView):
    """Yesterday's (or any specified UTC day's) security rollup. Mirrors
    the payload of the ``daily_security_summary`` Celery task that emails
    the Security Officer."""
    permission_classes = [IsAuthenticated, has_permission("secops:view")]

    @extend_schema(
        tags=["Security Operations"],
        summary="Security daily summary for one UTC day",
        operation_id="secops_daily_summary",
        parameters=[
            OpenApiParameter(
                "date", str,
                description="ISO-8601 (YYYY-MM-DD). Defaults to yesterday UTC.",
                required=False,
            ),
        ],
        responses={200: inline_serializer(
            name="DailySummaryResponse",
            fields={
                "success": serializers.BooleanField(default=True),
                "data": inline_serializer(
                    name="DailySummaryData",
                    fields={
                        "date": serializers.DateField(),
                        "total": serializers.IntegerField(),
                        "by_category": serializers.DictField(child=serializers.IntegerField()),
                        "by_severity": serializers.DictField(child=serializers.IntegerField()),
                        "top_ips": serializers.ListField(child=serializers.DictField()),
                    },
                ),
                "meta": inline_serializer(
                    name="DailySummaryMeta",
                    fields={"request_id": serializers.CharField()},
                ),
            },
        )},
    )
    def get(self, request):
        date_str = request.query_params.get("date")
        if date_str:
            target = parse_date(date_str)
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
        else:
            target = (timezone.now().astimezone(py_timezone.utc) - timedelta(days=1)).date()

        day_start = datetime.combine(target, time.min, tzinfo=py_timezone.utc)
        day_end = day_start + timedelta(days=1)
        queryset = SecurityEvent.objects.filter(
            occurred_at__gte=day_start, occurred_at__lt=day_end,
        )
        by_category = dict(
            queryset.values_list("category").annotate(c=Count("id")).values_list("category", "c")
        )
        by_severity = dict(
            queryset.values_list("severity").annotate(c=Count("id")).values_list("severity", "c")
        )
        top_ips = list(
            queryset.exclude(ip_address__isnull=True)
            .values("ip_address").annotate(c=Count("id")).order_by("-c")[:10]
        )

        return _envelope(
            {
                "date": target,
                "total": queryset.count(),
                "by_category": by_category,
                "by_severity": by_severity,
                "top_ips": [
                    {"ip": row["ip_address"], "count": row["c"]} for row in top_ips
                ],
            },
            request,
        )
