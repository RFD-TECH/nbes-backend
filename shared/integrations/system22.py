"""
System 22 (SIEM / tamper-evident audit store) client.

NOTE: Direct calls to System 22 via this client are legacy/deprecated.
Consolidated integration pattern: all security, audit,
and anchor events now flow through the transactional outbox (OutboxEvent)
and are relayed via System 17 to the Kafka event bus.

This file is preserved as an integration reference for webhook receivers or direct
SIEM escalation if outbox-based delivery is bypassed in future hardening sprints.
"""

import json
import logging
import uuid

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


class System22Client:
    def __init__(self):
        # System 22 URL not yet in settings — derive from System 17 config area.
        self.base_url = getattr(settings, "SYSTEM_22_URL", "").rstrip("/")
        self.api_key = getattr(settings, "SYSTEM_22_API_KEY", "")
        self._dev_mode = not self.base_url or not self.api_key
        if self._dev_mode and not settings.DEBUG:
            raise ImproperlyConfigured(
                "SYSTEM_22_URL and SYSTEM_22_API_KEY are required when DEBUG=False."
            )

    def export_audit_anchor(self, date: str, head_hash: str, event_count: int) -> str:
        """
        Forward the daily hash anchor to System 22's tamper-evident store.
        Returns an anchor_ref string (System 22's storage reference).
        In dev: logs and returns a stub ref.
        """
        payload = {
            "source": "nbes",
            "date": date,
            "head_hash": head_hash,
            "event_count": event_count,
        }
        if self._dev_mode:
            stub_ref = f"S22-DEV-{date}-{uuid.uuid4().hex[:8].upper()}"
            logger.info(
                "System22 [DEV STUB] export_audit_anchor date=%s head_hash=%s ref=%s",
                date,
                head_hash,
                stub_ref,
            )
            return stub_ref

        resp = requests.post(
            f"{self.base_url}/api/v1/audit-anchors",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        anchor_ref = resp.json().get("anchor_ref", "")
        if not anchor_ref:
            raise ValueError(
                f"System 22 returned no anchor_ref for date={date}; "
                "export cannot be confirmed as durable."
            )
        return anchor_ref

    def send_security_event(self, event_type: str, payload: dict) -> None:
        """
        Forward a security event (auth failures, IP blocks, anomalies) to System 22 SIEM.
        In dev: logs the call only.
        """
        body = {"source": "nbes", "event_type": event_type, **payload}
        if self._dev_mode:
            logger.info(
                "System22 [DEV STUB] send_security_event type=%s payload=%s",
                event_type,
                json.dumps(body),
            )
            return

        resp = requests.post(
            f"{self.base_url}/api/v1/security-events",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
