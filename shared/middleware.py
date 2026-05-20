"""
shared/middleware.py — NBES Request Middleware
==============================================

AuditMiddleware:
    - Injects a unique X-Request-ID UUID into every request.
    - Attaches IP address and User-Agent to the request for use by
      AuditEvent.record() calls in services and views.
    - Echoes X-Request-ID in the response header.

Reference: NBES System Architecture §2.1 — shared/middleware.py
"""

import json
import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.utils.deprecation import MiddlewareMixin


logger = logging.getLogger(__name__)


class JsonExceptionMiddleware:
    """Return JSON envelopes for uncaught API exceptions."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            response = self.get_response(request)
        except Exception as exc:
            if not request.path.startswith("/api/"):
                raise

            logger.exception("Unhandled API exception")
            return self._error_response(request, 500, "SERVER_ERROR")

        if self._should_wrap_response(request, response):
            return self._error_response(
                request,
                response.status_code,
                self._error_code(response.status_code),
            )
        return response

    @staticmethod
    def _should_wrap_response(request, response):
        content_type = response.get("Content-Type", "")
        return (
            request.path.startswith("/api/")
            and response.status_code >= 400
            and content_type.startswith("text/html")
        )

    @staticmethod
    def _error_code(status_code):
        return {
            400: "VALIDATION_ERROR",
            401: "NOT_AUTHENTICATED",
            403: "AUTHZ_DENIED",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            429: "RATE_LIMITED",
            500: "SERVER_ERROR",
        }.get(status_code, "ERROR")

    @staticmethod
    def _error_message(status_code):
        return {
            404: "Not found.",
            500: "Internal server error.",
        }.get(status_code, "Request failed.")

    def _error_response(self, request, status_code, error_code):
        request_id = str(getattr(request, "request_id", ""))
        return JsonResponse(
            {
                "success": False,
                "error": {
                    "code": error_code,
                    "message": self._error_message(status_code),
                },
                "meta": {"request_id": request_id},
            },
            status=status_code,
        )


class AuditMiddleware(MiddlewareMixin):
    """
    Injects request_id, ip_address, and user_agent onto every request object.
    These are consumed by AuditEvent.record() throughout the codebase.
    """

    def process_request(self, request):
        request.request_id = uuid.uuid4()
        request.ip_address = self._get_client_ip(request)
        request.user_agent = request.META.get("HTTP_USER_AGENT", "")

    def process_response(self, request, response):
        request_id = getattr(request, "request_id", None)
        if request_id:
            response["X-Request-ID"] = str(request_id)
        return response

    @staticmethod
    def _get_client_ip(request) -> str:
        """
        Extract real client IP, respecting X-Forwarded-For from the gateway.
        """
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "")


class IdempotencyKeyMiddleware:
    """Enforces ``Idempotency-Key`` on state-mutating API calls.

    Behaviour (only for paths under ``/api/`` and verbs that mutate):

    * **Missing header** → 400 with ``IDEMPOTENCY_KEY_REQUIRED``.
    * **First call** → request proceeds; if the response status code is
      < 500, the response is cached for
      ``IDEMPOTENCY_CACHE_TTL_SECONDS`` (default 24h).
    * **Replay with the same key** → the cached response is returned
      verbatim, body included, with ``X-Idempotent-Replay: true``.

    Cache key includes the JWT ``sub`` (when present) so two clients can
    use the same idempotency key without colliding. Unauthenticated
    requests use a hash of the remote IP instead.

    Mutating verbs: ``POST``, ``PUT``, ``PATCH``, ``DELETE``. ``GET`` /
    ``HEAD`` / ``OPTIONS`` pass through untouched.

    Reference: blueprint §1.2.8 ("Idempotency keys on every state-mutating
    call; safe retry semantics.").
    """

    MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
    HEADER = "HTTP_IDEMPOTENCY_KEY"
    CACHE_PREFIX = "nbes:idempotency"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not self._applies(request):
            return self.get_response(request)

        key = request.META.get(self.HEADER, "").strip()
        if not key:
            return self._err(request, "Idempotency-Key header is required for this verb.")
        if len(key) > 64:
            return self._err(request, "Idempotency-Key must be ≤ 64 characters.")

        cache_key = self._build_cache_key(request, key)
        cached = cache.get(cache_key)
        if cached is not None:
            return self._replay(cached, request)

        response = self.get_response(request)
        if response.status_code < 500:
            self._store(cache_key, response)
        return response

    def _applies(self, request) -> bool:
        if not request.path.startswith("/api/"):
            return False
        return request.method in self.MUTATING_METHODS

    @staticmethod
    def _build_cache_key(request, key: str) -> str:
        # Trust ``request.auth`` if DRF has populated it; otherwise fall
        # back to a stable hash of the remote IP so unauthenticated retries
        # still dedupe.
        sub = ""
        auth = getattr(request, "auth", None)
        if isinstance(auth, dict):
            sub = str(auth.get("sub", ""))
        scope = sub or f"ip:{request.META.get('REMOTE_ADDR', '')}"
        return f"{IdempotencyKeyMiddleware.CACHE_PREFIX}:{scope}:{key}"

    @staticmethod
    def _store(cache_key: str, response) -> None:
        ttl = getattr(settings, "IDEMPOTENCY_CACHE_TTL_SECONDS", 86400)
        try:
            body = response.content
        except AttributeError:
            return
        cache.set(
            cache_key,
            {
                "status": response.status_code,
                "content_type": response.get("Content-Type", "application/json"),
                "body": body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body,
            },
            timeout=ttl,
        )

    @staticmethod
    def _replay(cached: dict, request) -> HttpResponse:
        response = HttpResponse(
            cached["body"],
            status=cached["status"],
            content_type=cached.get("content_type", "application/json"),
        )
        response["X-Idempotent-Replay"] = "true"
        request_id = getattr(request, "request_id", None)
        if request_id:
            response["X-Request-ID"] = str(request_id)
        return response

    @staticmethod
    def _err(request, message: str) -> JsonResponse:
        request_id = str(getattr(request, "request_id", ""))
        return JsonResponse(
            {
                "success": False,
                "error": {"code": "IDEMPOTENCY_KEY_REQUIRED", "message": message},
                "meta": {"request_id": request_id},
            },
            status=400,
        )


class EdgeRateLimitMiddleware:
    """Edge per-IP throttle and 24h block, on rejected requests only.

    Counts every response with status in ``COUNTED_STATUSES`` (401, 403, 429
    by default) keyed on the remote IP, using Redis-backed *fixed* windows.
    A fixed window allows up to 2× the threshold across the boundary —
    acceptable trade-off for Sprint 1.3; switch to token-bucket in a
    later hardening pass if needed. Two thresholds:

    * **15-minute throttle.** Default 100 rejections. Once breached, all
      requests from that IP get ``429`` for the remainder of the window
      and a ``SecurityEvent(throttle_applied)`` is recorded.
    * **24-hour block.** Default 1000 rejections. Once breached, the IP is
      blocked for a full 24 hours, a ``SecurityEvent(ip_blocked)`` is
      recorded, and the security-officer notification fires.

    This middleware runs *before* DRF authentication so anonymous abusers
    are throttled too. It does **not** count successful 2xx/3xx — only
    rejections — so legitimate traffic from shared NATs is not penalised.

    Reference: blueprint §1.2.6 (100/15m + 1000/24h, F000-06 acceptance).
    """

    THROTTLE_WINDOW_SECONDS = 15 * 60
    BLOCK_WINDOW_SECONDS = 24 * 60 * 60
    COUNTED_STATUSES = {401, 403, 429}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith("/api/"):
            return self.get_response(request)

        ip = self._client_ip(request)
        if not ip:
            return self.get_response(request)

        # Cheap pre-check: already blocked or throttled?
        if cache.get(self._block_key(ip)):
            return self._block_response(request)
        if cache.get(self._throttle_key(ip)):
            return self._throttle_response(request)

        response = self.get_response(request)

        if response.status_code in self.COUNTED_STATUSES:
            self._record_rejection(request, ip)

        return response

    # ----- counters ---------------------------------------------------------

    def _record_rejection(self, request, ip: str) -> None:
        throttle_threshold = self._get(
            "EDGE_THROTTLE_THRESHOLD", 100,
        )
        block_threshold = self._get(
            "EDGE_BLOCK_THRESHOLD_24H", 1000,
        )

        throttle_count = self._incr(
            self._counter_key(ip, "throttle"), self.THROTTLE_WINDOW_SECONDS
        )
        block_count = self._incr(
            self._counter_key(ip, "block"), self.BLOCK_WINDOW_SECONDS
        )

        if throttle_count == throttle_threshold:
            cache.set(self._throttle_key(ip), True, timeout=self.THROTTLE_WINDOW_SECONDS)
            self._emit_secops(
                request, ip, category="throttle_applied",
                count=throttle_count,
                window_seconds=self.THROTTLE_WINDOW_SECONDS,
            )

        if block_count == block_threshold:
            cache.set(self._block_key(ip), True, timeout=self.BLOCK_WINDOW_SECONDS)
            self._emit_secops(
                request, ip, category="ip_blocked",
                count=block_count,
                window_seconds=self.BLOCK_WINDOW_SECONDS,
            )

    # ----- responses --------------------------------------------------------

    def _throttle_response(self, request) -> JsonResponse:
        return self._too_many(
            request,
            "Too many rejected requests from this IP — try again later.",
            self.THROTTLE_WINDOW_SECONDS,
        )

    def _block_response(self, request) -> JsonResponse:
        return self._too_many(
            request,
            "This IP is temporarily blocked due to repeated rejections.",
            self.BLOCK_WINDOW_SECONDS,
        )

    @staticmethod
    def _too_many(request, message: str, retry_after: int) -> JsonResponse:
        request_id = str(getattr(request, "request_id", ""))
        response = JsonResponse(
            {
                "success": False,
                "error": {"code": "RATE_LIMITED", "message": message},
                "meta": {"request_id": request_id},
            },
            status=429,
        )
        response["Retry-After"] = str(retry_after)
        return response

    # ----- helpers ----------------------------------------------------------

    @staticmethod
    def _client_ip(request) -> str:
        """Mirrors AuditMiddleware._get_client_ip — kept duplicated to
        avoid a hard dependency on middleware load order."""
        fwd = request.META.get("HTTP_X_FORWARDED_FOR")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "")

    @staticmethod
    def _counter_key(ip: str, kind: str) -> str:
        return f"nbes:edge:{kind}:{ip}"

    @staticmethod
    def _throttle_key(ip: str) -> str:
        return f"nbes:edge:throttle-active:{ip}"

    @staticmethod
    def _block_key(ip: str) -> str:
        return f"nbes:edge:block-active:{ip}"

    @staticmethod
    def _incr(key: str, window_seconds: int) -> int:
        """Increment a counter and set TTL on first hit."""
        try:
            value = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=window_seconds)
            return 1
        # Some cache backends drop TTL on incr; refresh on every hit. Cheap
        # and resilient to backend differences (LocMem, Redis, ...).
        ttl = cache.ttl(key) if hasattr(cache, "ttl") else None
        if ttl is None or ttl < 1:
            cache.expire(key, timeout=window_seconds) if hasattr(cache, "expire") else None
        return value

    @staticmethod
    def _get(name: str, default):
        return getattr(settings, name, default)

    @staticmethod
    def _emit_secops(request, ip: str, *, category: str, count: int, window_seconds: int) -> None:
        try:
            from shared.secops import record_security_event
            record_security_event(
                category=category,
                ip_address=ip,
                request_id=getattr(request, "request_id", None),
                indicators={
                    "count": count,
                    "window_seconds": window_seconds,
                    "path": request.path,
                    "method": request.method,
                },
            )
        except Exception as exc:
            logger.error("edge_rate_limit.emit_failed err=%s", exc)
