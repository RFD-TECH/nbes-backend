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

from datetime import datetime, timezone
from http import HTTPStatus
from django_fsm import TransitionNotAllowed
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import APIException


def format_rfc7807_error(
    status_code: int, error_code: str, message: str, request_id: str, fields=None
) -> dict:
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:
        title = "Error"
    payload = {
        "type": f"https://api.nbes.gov.gh/errors/{error_code.lower().replace('_', '-')}",
        "title": title,
        "status": status_code,
        "detail": message,
        "errorCode": error_code,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instance": f"urn:nbes:request:{request_id}"
        if request_id
        else "urn:nbes:request:unknown",
    }
    if fields:
        payload["invalid_params"] = fields
    return payload


def nbes_exception_handler(exc, context):
    """Custom DRF exception handler — wraps errors in RFC 7807 Problem Details."""
    request = context.get("request")
    request_id = str(getattr(request, "request_id", "")) if request else ""

    # Handle FSM transition errors specifically
    if isinstance(exc, TransitionNotAllowed):
        data = format_rfc7807_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="TRANSITION_NOT_ALLOWED",
            message=str(exc),
            request_id=request_id,
        )
        return Response(
            data,
            status=status.HTTP_400_BAD_REQUEST,
            content_type="application/problem+json",
        )

    # Fall through to DRF's default handler for all other exceptions
    response = exception_handler(exc, context)

    if response is None:
        data = format_rfc7807_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code="SERVER_ERROR",
            message="Internal server error.",
            request_id=request_id,
        )
        return Response(
            data,
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content_type="application/problem+json",
        )

    if response is not None:
        error_code = _get_error_code(response.status_code)
        error_detail = (
            response.data.copy() if isinstance(response.data, dict) else response.data
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

        data = format_rfc7807_error(
            status_code=response.status_code,
            error_code=error_code,
            message=message,
            request_id=request_id,
            fields=fields,
        )
        response.data = data
        response.content_type = "application/problem+json"

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


def success_response(
    data=None, message="Success", status_code=status.HTTP_200_OK, meta=None
):
    """
    Standard NBES success envelope.
    Usage: return success_response(data={"item_id": 123}, status_code=201)
    """
    response_data = {
        "success": True,
        "message": message,
        "data": data if data is not None else {},
        "meta": meta if meta is not None else {},
    }
    return Response(response_data, status=status_code)


def error_response(
    message,
    code="ERROR",
    errors=None,
    status_code=status.HTTP_400_BAD_REQUEST,
    meta=None,
):
    """
    Standard NBES error envelope for manual error triggers (bypassing the exception handler).
    Usage: return error_response("Invalid file", code="INVALID_FORMAT")
    """
    request_id = ""
    if meta and "request_id" in meta:
        request_id = meta["request_id"]

    data = format_rfc7807_error(
        status_code=status_code,
        error_code=code,
        message=message,
        request_id=request_id,
        fields=errors,
    )
    return Response(data, status=status_code, content_type="application/problem+json")
