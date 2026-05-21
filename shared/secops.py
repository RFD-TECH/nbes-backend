"""shared/secops.py — Single entry point for recording security events.

Used by ``shared/auth.py`` (token rejection), ``shared/permissions.py``
(403 denied), ``shared/middleware.py::EdgeRateLimitMiddleware`` (throttle
/ block), and any future anomaly detector.

Every call writes one ``SecurityEvent`` row and emits one ``OutboxEvent``
so System 22 sees the same data via the outbox poller. The two writes
are inside the caller's transaction (or an implicit autocommit one) so
they are atomic from the perspective of any reader.

The blueprint §1.2.6 anchors this — "security event taxonomy aligned
with the System 22 SIEM schema."
"""
from __future__ import annotations

import logging
from typing import Any

from django.db import transaction


logger = logging.getLogger(__name__)


CATEGORY_SEVERITY = {
    "auth_token_invalid":     "warning",
    "auth_token_expired":     "info",
    "auth_audience_mismatch": "warning",
    "authz_denied":           "warning",
    "throttle_applied":       "warning",
    "ip_blocked":             "high",
    "anomaly_detected":       "high",
}


def record_security_event(
    *,
    category: str,
    ip_address: str | None = None,
    actor_id: Any = None,
    request_id: Any = None,
    indicators: dict | None = None,
    severity: str | None = None,
) -> None:
    """Record a SecurityEvent row and outbox-publish the same payload.

    Best-effort: failures are logged but never raised — security recording
    must not break the request path that triggered it. The outbox write
    guarantees System 22 sees the event even if the row is later cleaned
    out of the hot table.
    """
    from apps.audit.models import SecurityEvent
    from shared.events import publish

    sev = severity or CATEGORY_SEVERITY.get(category, "warning")
    inds = indicators or {}

    try:
        with transaction.atomic():
            event = SecurityEvent.objects.create(
                category=category,
                severity=sev,
                indicators=inds,
                ip_address=ip_address or None,
                actor_id=actor_id or None,
                request_id=request_id or None,
            )
            publish(
                "SecurityEventRecorded",
                {
                    "event_id": str(event.event_id),
                    "category": category,
                    "severity": sev,
                    "indicators": inds,
                    "ip_address": ip_address or None,
                    "actor_id": str(actor_id) if actor_id else None,
                    "request_id": str(request_id) if request_id else None,
                    "occurred_at": event.occurred_at.isoformat(),
                },
                topic="nbes.secops",
            )
    except Exception as exc:
        # Never let security recording bring down the request path. Logging
        # is the last-ditch evidence.
        logger.error(
            "secops.record_failed category=%s ip=%s err=%s",
            category, ip_address, exc,
        )
