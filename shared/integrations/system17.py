"""
shared/integrations/system17.py — System 17 (API Gateway) client.

All inter-system HTTP calls from NBES go through this client.
Payload is signed with HMAC-SHA256 for replay protection.
Every later phase must use this client — no raw HTTP calls in services.

Reference: NBES Architecture §1.2.8 — Integration Patterns
"""
import hashlib
import hmac
import json
import logging
import time
import uuid

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class IntegrationError(Exception):
    def __init__(self, message, retryable=False, correlation_id=None):
        super().__init__(message)
        self.retryable = retryable
        self.correlation_id = correlation_id


class System17Client:
    """
    Signed, replay-protected client for System 17 (API Gateway).

    Usage:
        client = System17Client()
        result = client.post("/api/v1/some-path", {"key": "value"}, idempotency_key="abc")
    """

    def __init__(self):
        self.base_url = getattr(settings, "SYSTEM_17_URL", "").rstrip("/")
        self.api_key = getattr(settings, "SYSTEM_17_API_KEY", "")
        self._dev_mode = not self.base_url or not self.api_key

    def post(self, path: str, payload: dict, idempotency_key: str) -> dict:
        """
        POST payload to System 17 with HMAC-SHA256 signature and replay protection.

        Retries up to 3 times with exponential backoff on 5xx responses.
        In dev (SYSTEM_17_URL not configured): logs the call and returns stub.
        """
        correlation_id = str(uuid.uuid4())
        if self._dev_mode:
            logger.info(
                "System17 [DEV STUB] POST %s | idempotency=%s | correlation=%s | payload=%s",
                path, idempotency_key, correlation_id, json.dumps(payload),
            )
            return {"status": "stub_ok", "correlation_id": correlation_id}

        nonce = str(uuid.uuid4())
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        body = json.dumps(payload, sort_keys=True)
        signature = self._sign(nonce, timestamp, body)

        headers = {
            "Content-Type": "application/json",
            "X-Nonce": nonce,
            "X-Timestamp": timestamp,
            "X-Signature": signature,
            "X-Idempotency-Key": idempotency_key,
            "X-Correlation-ID": correlation_id,
            "Authorization": f"Bearer {self.api_key}",
        }

        url = f"{self.base_url}{path}"
        for attempt in range(3):
            try:
                resp = requests.post(url, data=body, headers=headers, timeout=10)
                if 400 <= resp.status_code < 500:
                    raise IntegrationError(
                        f"System17 POST {path} failed with {resp.status_code}: {resp.text}",
                        retryable=False,
                        correlation_id=correlation_id,
                    )
                if resp.status_code < 500:
                    return resp.json()
                backoff = 2 ** attempt
                logger.warning(
                    "System17 %s returned %s (attempt %d/3), retrying in %ds",
                    path, resp.status_code, attempt + 1, backoff,
                )
                time.sleep(backoff)
            except IntegrationError:
                raise
            except requests.RequestException as exc:
                if attempt == 2:
                    raise IntegrationError(
                        f"System17 POST {path} failed after 3 attempts: {exc}",
                        retryable=True,
                        correlation_id=correlation_id,
                    ) from exc
                time.sleep(2 ** attempt)

        raise IntegrationError(
            f"System17 POST {path} exhausted retries", retryable=True,
            correlation_id=correlation_id,
        )

    def _sign(self, nonce: str, timestamp: str, body: str) -> str:
        """HMAC-SHA256(key=api_key, msg=f'{nonce}:{timestamp}:{body}')"""
        message = f"{nonce}:{timestamp}:{body}".encode()
        return hmac.new(self.api_key.encode(), message, hashlib.sha256).hexdigest()
