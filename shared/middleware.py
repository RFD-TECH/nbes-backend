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
from django.utils.deprecation import MiddlewareMixin


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
