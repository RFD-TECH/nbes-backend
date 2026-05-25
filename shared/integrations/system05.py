"""
shared/integrations/system05.py — System 05 (Regulator Archive) client.

System 05 is the regulator's tamper-evident archive of immutable governance
records. NBES Phase 2 archives signed Minutes (and associated agendas /
attendance / resolutions) here within 1 hour of Chair sign-off (SRS §2.4.2,
§2.11 F01-05 acceptance).

Contract per SRS §2.2.5:
  - Payload is signed and includes an integrity hash.
  - Retention ≥ 15 years; tamper-evident chain via System 22.
  - Retries with exponential backoff up to 24 hours; permanent failures
    escalate to the Administrator.
  - Daily integrity checksum verified between the local copy and the
    System 05 archive copy.

In dev (``SYSTEM_05_URL`` not configured) every call is a logged no-op so
local environments need no running archive.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


class System05Error(Exception):
    """Raised when System 05 returns a non-recoverable error."""

    def __init__(self, message: str, *, retryable: bool = False, correlation_id: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.correlation_id = correlation_id


class System05Client:
    """HTTP client for the regulator's archive (System 05).

    Usage:
        client = System05Client()
        archive_ref = client.archive_minutes(
            minutes_id=str(minutes.id),
            meeting_reference=meeting.reference,
            content=minutes.content,
            signed_by=str(minutes.approved_by_id),
            signed_at=minutes.immutable_at.isoformat(),
            signature_ref=minutes.signature_ref,
        )
    """

    def __init__(self):
        self.base_url = (getattr(settings, "SYSTEM_05_URL", "") or "").rstrip("/")
        self.api_key = getattr(settings, "SYSTEM_05_API_KEY", "") or ""
        self._dev_mode = not self.base_url or not self.api_key
        if self._dev_mode and not settings.DEBUG:
            raise ImproperlyConfigured(
                "SYSTEM_05_URL and SYSTEM_05_API_KEY are required when DEBUG=False."
            )

    # ── public API ────────────────────────────────────────────────────────────

    def archive_minutes(
        self,
        *,
        minutes_id: str,
        meeting_reference: str,
        content: str,
        signed_by: str,
        signed_at: str,
        signature_ref: str,
        document_ref: str = "",
    ) -> str:
        """Submit signed Minutes to System 05. Returns the archive_ref."""
        payload = {
            "source": "nbes",
            "kind": "nbec_minutes",
            "record_id": minutes_id,
            "meeting_reference": meeting_reference,
            "content": content,
            "signed_by": signed_by,
            "signed_at": signed_at,
            "signature_ref": signature_ref,
            "document_ref": document_ref,
        }
        return self._submit("nbec_minutes", payload)

    def verify_integrity(self, *, archive_ref: str, local_hash: str) -> bool:
        """Ask System 05 whether ``archive_ref`` still hashes to ``local_hash``.

        Returns True when the archive's hash matches; False otherwise.
        Raises System05Error on transport failure so callers can retry.
        """
        if self._dev_mode:
            logger.info(
                "System05 [DEV STUB] verify_integrity archive_ref=%s local_hash=%s",
                archive_ref, local_hash,
            )
            return True

        correlation_id = str(uuid.uuid4())
        try:
            resp = requests.get(
                f"{self.base_url}/api/v1/archive/{archive_ref}/integrity",
                params={"expected_hash": local_hash},
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "X-Correlation-ID": correlation_id,
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            raise System05Error(
                f"System 05 integrity transport error: {exc}",
                retryable=True, correlation_id=correlation_id,
            ) from exc

        if resp.status_code == 404:
            raise System05Error(
                f"archive_ref {archive_ref!r} not found in System 05",
                retryable=False, correlation_id=correlation_id,
            )
        if 500 <= resp.status_code < 600:
            raise System05Error(
                f"System 05 integrity check {resp.status_code}",
                retryable=True, correlation_id=correlation_id,
            )
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as exc:
            raise System05Error(
                f"System 05 returned non-JSON integrity response: {exc}",
                retryable=True, correlation_id=correlation_id,
            ) from exc
        return bool(body.get("match", False))

    # ── internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def integrity_hash(payload: dict) -> str:
        """SHA-256 of the canonical JSON of the payload (sort_keys=True)."""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _submit(self, kind: str, payload: dict) -> str:
        integrity_hash = self.integrity_hash(payload)
        envelope = {
            "kind": kind,
            "payload": payload,
            "integrity_hash": integrity_hash,
        }

        if self._dev_mode:
            stub_ref = f"S05-DEV-{kind}-{uuid.uuid4().hex[:8].upper()}"
            logger.info(
                "System05 [DEV STUB] %s submitted record_id=%s integrity_hash=%s ref=%s",
                kind, payload.get("record_id"), integrity_hash, stub_ref,
            )
            return stub_ref

        correlation_id = str(uuid.uuid4())
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/archive",
                json=envelope,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "X-Correlation-ID": correlation_id,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise System05Error(
                f"System 05 transport error: {exc}",
                retryable=True, correlation_id=correlation_id,
            ) from exc

        if 400 <= resp.status_code < 500:
            raise System05Error(
                f"System 05 rejected {kind} ({resp.status_code}): {resp.text}",
                retryable=False, correlation_id=correlation_id,
            )
        if 500 <= resp.status_code < 600:
            raise System05Error(
                f"System 05 {resp.status_code}: {resp.text}",
                retryable=True, correlation_id=correlation_id,
            )
        resp.raise_for_status()

        try:
            body = resp.json()
        except ValueError as exc:
            raise System05Error(
                f"System 05 returned non-JSON archive response: {exc}",
                retryable=True, correlation_id=correlation_id,
            ) from exc
        archive_ref = body.get("archive_ref")
        if not archive_ref:
            raise System05Error(
                "System 05 returned no archive_ref; archival cannot be confirmed.",
                retryable=False, correlation_id=correlation_id,
            )
        return archive_ref
