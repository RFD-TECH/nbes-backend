"""
shared/exceptions.py — Custom DRF Exception Handler
====================================================

Wraps all API responses in the standard NBES envelope:
    {
        "success": true,
        "data": { ... },
        "meta": { "request_id": "uuid" }
    }

Error responses:
    {
        "success": false,
        "error": {
            "code": "TRANSITION_NOT_ALLOWED",
            "message": "...",
            "fields": { ... }   // optional validation errors
        },
        "meta": { "request_id": "uuid" }
    }

Maps:
    django_fsm.TransitionNotAllowed → 400 TRANSITION_NOT_ALLOWED
    DRF ValidationError             → 400 VALIDATION_ERROR
    DRF PermissionDenied            → 403 AUTHZ_DENIED
    DRF NotAuthenticated            → 401 NOT_AUTHENTICATED
    DRF NotFound                    → 404 NOT_FOUND

Reference: NBES System Architecture §5.2 — Standard Response Envelope
"""

from django_fsm import TransitionNotAllowed
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status


def success_response(data=None, *, request=None, status_code: int = status.HTTP_200_OK) -> Response:
    """Wrap a success payload in the NBES standard envelope."""
    request_id = ""
    if request is not None:
        request_id = str(getattr(request, "request_id", "")) if request else ""
    return Response(
        {"success": True, "data": data, "meta": {"request_id": request_id}},
        status=status_code,
    )


def error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    request=None,
    fields: dict | None = None,
) -> Response:
    """Wrap an error payload in the NBES standard envelope.

    Prefer raising DRF exceptions so the global handler does this for you.
    Use this helper for non-exception error paths (e.g. business rule fails
    that map to a specific code/status not covered by DRF's defaults).
    """
    request_id = str(getattr(request, "request_id", "")) if request else ""
    err: dict = {"code": code, "message": message}
    if fields:
        err["fields"] = fields
    return Response(
        {"success": False, "error": err, "meta": {"request_id": request_id}},
        status=status_code,
    )


def nbes_exception_handler(exc, context):
    """Custom DRF exception handler — wraps errors in NBES standard envelope."""
    request = context.get("request")
    request_id = str(getattr(request, "request_id", "")) if request else ""

    # Handle FSM transition errors specifically
    if isinstance(exc, TransitionNotAllowed):
        return Response(
            {
                "success": False,
                "error": {
                    "code": "TRANSITION_NOT_ALLOWED",
                    "message": str(exc),
                },
                "meta": {"request_id": request_id},
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Fall through to DRF's default handler for all other exceptions
    response = exception_handler(exc, context)

    if response is not None:
        error_code = _get_error_code(response.status_code)
        error_detail = response.data

        # Separate field-level errors from top-level messages
        fields = None
        message = str(exc)
        if isinstance(error_detail, dict):
            non_field = error_detail.pop("non_field_errors", None)
            if error_detail:
                fields = {
                    k: [str(e) for e in v] if isinstance(v, list) else str(v)
                    for k, v in error_detail.items()
                }
            if non_field:
                message = " ".join(str(e) for e in non_field)

        error = {"code": error_code, "message": message}
        if fields:
            error["fields"] = fields

        response.data = {
            "success": False,
            "error": error,
            "meta": {"request_id": request_id},
        }

    return response


def _get_error_code(status_code: int) -> str:
    return {
        400: "VALIDATION_ERROR",
        401: "NOT_AUTHENTICATED",
        403: "AUTHZ_DENIED",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        429: "RATE_LIMITED",
        500: "SERVER_ERROR",
    }.get(status_code, "ERROR")
