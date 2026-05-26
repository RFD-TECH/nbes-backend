"""System 17 client.

Every inter-system call goes through System 17 (API Layer) with signed,
replay-protected payloads. This module is the canonical wrapper; feature
code never opens raw HTTP to a partner system.

Wire format (all headers):

* ``X-NBES-Timestamp`` — UTC ISO-8601 second-precision.
* ``X-NBES-Nonce``     — 128-bit random hex; System 17 caches recent
  nonces and rejects replays within ``SYSTEM_17_NONCE_WINDOW_SECONDS``.
* ``X-NBES-Signature`` — hex HMAC-SHA256 over ``timestamp + nonce + body``
  using ``SYSTEM_17_HMAC_SECRET``.
* ``X-Idempotency-Key`` — caller-supplied; required for state-mutating
  verbs. System 17 deduplicates on this key.
* ``X-Correlation-ID`` — propagates the original request's correlation
  for end-to-end tracing.

Retry policy: 3 tries with exponential backoff on ``5xx`` and connection
errors. ``4xx`` responses are returned verbatim — they are caller mistakes
and retrying would only amplify the wrong thing.

Reference: blueprint §1.2.8.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone as py_timezone
from typing import Any

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


@dataclass
class System17Response:
    """Normalised response envelope from System 17.

    ``ok=True`` means the call succeeded (2xx). ``ok=False`` carries the
    error code + message from System 17's standard envelope. ``retryable``
    surfaces System 17's hint so callers can decide whether to schedule a
    retry via Celery rather than hammering inline.
    """

    ok: bool
    status_code: int
    data: Any
    code: str = ""
    message: str = ""
    retryable: bool = False
    correlation_id: str = ""

    @classmethod
    def from_http(
        cls, response: requests.Response, correlation_id: str
    ) -> "System17Response":
        try:
            body = response.json()
        except ValueError:
            body = {}

        if 200 <= response.status_code < 300:
            return cls(
                ok=True,
                status_code=response.status_code,
                data=body.get("data", body) if isinstance(body, dict) else body,
                correlation_id=correlation_id,
            )

        if not isinstance(body, dict):
            body = {}
        err = body.get("error") or {}
        return cls(
            ok=False,
            status_code=response.status_code,
            data=body.get("data"),
            code=err.get("code") or "SYSTEM_17_ERROR",
            message=err.get("message") or f"System 17 returned {response.status_code}.",
            retryable=bool(err.get("retryable", response.status_code >= 500)),
            correlation_id=correlation_id,
        )


def call_system_17(
    endpoint: str,
    payload: dict,
    *,
    idempotency_key: str,
    correlation_id: str = "",
    method: str = "POST",
    timeout: float | None = None,
    max_retries: int = 3,
    traceparent: str = "",
    tracestate: str = "",
) -> System17Response:
    """Make a signed, idempotent call through System 17.

    Args:
        endpoint: path relative to ``SYSTEM_17_URL`` (e.g. ``/v1/notify``).
        payload: JSON-serialisable body.
        idempotency_key: caller-chosen key, ≤ 64 chars. Required even for
            GET so System 17's dedup cache can elide duplicates.
        correlation_id: optional originating request_id. When blank,
            generated server-side.
        method: HTTP verb. Defaults to POST.
        timeout: per-attempt timeout in seconds (default from settings).
        max_retries: retries on 5xx / connection errors. Default 3.

    Returns:
        :class:`System17Response`. Never raises on a remote error — the
        caller inspects ``ok`` and ``retryable``. Raises only on programmer
        errors (missing settings, malformed payload).
    """
    if not getattr(settings, "SYSTEM_17_URL", ""):
        raise RuntimeError("SYSTEM_17_URL is not configured.")
    if not getattr(settings, "SYSTEM_17_HMAC_SECRET", ""):
        raise RuntimeError("SYSTEM_17_HMAC_SECRET is not configured.")
    if not idempotency_key or len(idempotency_key) > 64:
        raise ValueError("idempotency_key must be a non-empty string ≤ 64 chars.")

    base = settings.SYSTEM_17_URL.rstrip("/")
    url = f"{base}{endpoint if endpoint.startswith('/') else '/' + endpoint}"
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    correlation_id = correlation_id or secrets.token_hex(8)
    request_timeout = (
        timeout
        if timeout is not None
        else getattr(settings, "SYSTEM_17_TIMEOUT_SECONDS", 5)
    )

    last_response: requests.Response | None = None
    for attempt in range(1, max_retries + 1):
        headers = _build_headers(
            body,
            idempotency_key,
            correlation_id,
            traceparent=traceparent,
            tracestate=tracestate,
        )
        try:
            last_response = requests.request(
                method,
                url,
                data=body,
                headers=headers,
                timeout=request_timeout,
            )
        except requests.RequestException as exc:
            logger.warning(
                "system17.transport_error attempt=%d endpoint=%s err=%s",
                attempt,
                endpoint,
                exc,
            )
            if attempt == max_retries:
                return System17Response(
                    ok=False,
                    status_code=0,
                    data=None,
                    code="SYSTEM_17_UNREACHABLE",
                    message=str(exc),
                    retryable=True,
                    correlation_id=correlation_id,
                )
            time.sleep(_backoff_seconds(attempt))
            continue

        if last_response.status_code < 500 or attempt == max_retries:
            return System17Response.from_http(last_response, correlation_id)

        logger.info(
            "system17.5xx attempt=%d status=%d endpoint=%s",
            attempt,
            last_response.status_code,
            endpoint,
        )
        time.sleep(_backoff_seconds(attempt))

    # Unreachable, but linters appreciate the explicit fallthrough.
    assert last_response is not None
    return System17Response.from_http(last_response, correlation_id)


def _build_headers(
    body: str,
    idempotency_key: str,
    correlation_id: str,
    traceparent: str = "",
    tracestate: str = "",
) -> dict:
    timestamp = datetime.now(py_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = secrets.token_hex(16)
    signature = hmac.new(
        settings.SYSTEM_17_HMAC_SECRET.encode("utf-8"),
        f"{timestamp}{nonce}{body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-NBES-Timestamp": timestamp,
        "X-NBES-Nonce": nonce,
        "X-NBES-Signature": signature,
        "X-Idempotency-Key": idempotency_key,
        "X-Correlation-ID": correlation_id,
    }
    if traceparent:
        headers["traceparent"] = traceparent
    if tracestate:
        headers["tracestate"] = tracestate
    return headers


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 0.5s, 1s, 2s with jitter."""
    base = 0.5 * (2 ** (attempt - 1))
    return base + (secrets.randbelow(100) / 1000.0)
