"""
shared/integrations/system22.py — System 22 (SIEM / tamper-evident audit store) client.

Used by:
  - apps/audit/tasks.py  → export_daily_audit_anchor  (daily at 01:00 UTC)
  - apps/users/views.py  → auth event forwarding (AUTH_FAILED, IP_BLOCKED, etc.)

In dev (SYSTEM_22_URL not configured): logs the call, returns a stub reference.

Reference: NBES Architecture §1.2.8 — System 22 integration patterns
"""
import json
import logging
import uuid

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class System22Client:
    def __init__(self):
        # System 22 URL not yet in settings — derive from System 17 config area.
        self.base_url = getattr(settings, "SYSTEM_22_URL", "").rstrip("/")
        self.api_key = getattr(settings, "SYSTEM_22_API_KEY", "")
        self._dev_mode = not self.base_url or not self.api_key

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
                date, head_hash, stub_ref,
            )
            return stub_ref

        resp = requests.post(
            f"{self.base_url}/api/v1/audit-anchors",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("anchor_ref", "")

    def send_security_event(self, event_type: str, payload: dict) -> None:
        """
        Forward a security event (auth failures, IP blocks, anomalies) to System 22 SIEM.
        In dev: logs the call only.
        """
        body = {"source": "nbes", "event_type": event_type, **payload}
        if self._dev_mode:
            logger.info(
                "System22 [DEV STUB] send_security_event type=%s payload=%s",
                event_type, json.dumps(body),
            )
            return

        requests.post(
            f"{self.base_url}/api/v1/security-events",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=10,
        )
