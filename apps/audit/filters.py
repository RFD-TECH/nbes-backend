"""apps/audit/filters.py — Query filtering for the auditor search API."""
from __future__ import annotations

from datetime import datetime, time, timezone as py_timezone

from django.db.models import Q


def _parse_iso_date(value: str, *, end: bool = False):
    """Parse YYYY-MM-DD or ISO-8601 datetime into a UTC datetime."""
    if not value:
        return None
    try:
        if "T" in value or " " in value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            d = datetime.fromisoformat(value).date()
            dt = datetime.combine(
                d,
                time.max if end else time.min,
                tzinfo=py_timezone.utc,
            )
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=py_timezone.utc)
    return dt


def build_audit_query(params) -> Q:
    """Build a Django ``Q`` from supported audit query parameters."""
    q = Q()

    actor = params.get("actor") or params.get("actor_id")
    if actor:
        q &= Q(actor_id=actor)

    action = params.get("action")
    if action:
        q &= Q(action=action)

    entity_type = params.get("entity_type")
    if entity_type:
        q &= Q(entity_type=entity_type)

    entity_id = params.get("entity_id")
    if entity_id:
        q &= Q(entity_id=entity_id)

    request_id = params.get("request_id")
    if request_id:
        q &= Q(request_id=request_id)

    source_system = params.get("source_system")
    if source_system:
        q &= Q(source_system=source_system)

    text = params.get("q")
    if text:
        q &= Q(action__icontains=text) | Q(entity_type__icontains=text)

    start = _parse_iso_date(params.get("from") or params.get("date_from"))
    if start is not None:
        q &= Q(created_at__gte=start)

    end = _parse_iso_date(params.get("to") or params.get("date_to"), end=True)
    if end is not None:
        q &= Q(created_at__lte=end)

    return q


def apply_audit_filters(queryset, params):
    """Backward-compatible queryset helper for older callers/tests."""
    return queryset.filter(build_audit_query(params))
