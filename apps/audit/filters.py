"""apps/audit/filters.py — Query filtering for the auditor search API.

Mounted by ``AuditSearchView``. Translates the query-string into a Django
``Q`` over ``AuditEvent``. All filters are optional; an unfiltered call
returns the most-recent first slice (paginator handles the cap).

Filterable fields are deliberately minimal — the blueprint §1.4 names
actor, action, entity, date range, and correlation/request id. Anything
beyond that is out of scope for Sprint 1.3.
"""
from __future__ import annotations

from datetime import datetime, time, timezone as py_timezone

from django.db.models import Q


def _parse_iso_date(value: str, *, end: bool = False):
    """Parse YYYY-MM-DD (date) or ISO-8601 (datetime). Returns a UTC datetime
    or None when the value is empty/invalid.

    ``end=True`` snaps a date-only value to 23:59:59.999999 so the caller's
    inclusive ``to=`` semantics work without surprises.
    """
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
    """Build a Django ``Q`` from the request's query parameters.

    Accepted params:
        actor       — UUID of the actor (Keycloak sub)
        action      — exact match on action name (e.g. ITEM_APPROVED)
        entity_type — exact match
        entity_id   — UUID
        request_id  — UUID, the request-correlation id from AuditMiddleware
        from        — ISO date/datetime (inclusive lower bound)
        to          — ISO date/datetime (inclusive upper bound)
    """
    q = Q()

    actor = params.get("actor")
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

    start = _parse_iso_date(params.get("from"))
    if start is not None:
        q &= Q(created_at__gte=start)

    end = _parse_iso_date(params.get("to"), end=True)
    if end is not None:
        q &= Q(created_at__lte=end)

    return q
