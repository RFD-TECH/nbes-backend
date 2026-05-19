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

import uuid
import logging
from django.http import JsonResponse
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
