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
from rest_framework.exceptions import APIException


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

    if response is None:
        return Response(
            {
                "success": False,
                "error": {
                    "code": "SERVER_ERROR",
                    "message": "Internal server error.",
                },
                "meta": {"request_id": request_id},
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if response is not None:
        error_code = _get_error_code(response.status_code)
        error_detail = (
            response.data.copy()
            if isinstance(response.data, dict)
            else response.data
        )

        # Separate field-level errors from top-level messages
        fields = None
        message = str(exc)
        if isinstance(exc, APIException):
            message = str(exc.detail)
        if isinstance(error_detail, dict):
            non_field = error_detail.pop("non_field_errors", None)
            detail = error_detail.pop("detail", None)
            if detail:
                message = str(detail)
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
