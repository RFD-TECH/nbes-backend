"""NBEC Committee API views.

Endpoints (Phase 2 — NBE-F01):
  POST   /api/v1/nbec/members/                  — create member
  PATCH  /api/v1/nbec/members/{id}/             — amend member
  POST   /api/v1/nbec/members/{id}/activate/    — activate member
  POST   /api/v1/nbec/coi/                      — declare COI
  POST   /api/v1/nbec/coi/{id}/review/          — review COI (approve/dismiss)
  POST   /api/v1/nbec/meetings/                 — schedule meeting
  POST   /api/v1/nbec/meetings/{id}/agenda/     — publish agenda
  POST   /api/v1/nbec/meetings/{id}/attendance/ — record attendance
  POST   /api/v1/nbec/meetings/{id}/convene/    — convene meeting
  POST   /api/v1/nbec/meetings/{id}/adjourn/    — adjourn meeting
  POST   /api/v1/nbec/minutes/{id}/sign/        — Chair signs minutes
  POST   /api/v1/nbec/minutes/{id}/addendum/    — Chair issues addendum
  GET    /api/v1/nbec/policy/coi/               — internal COI check
"""

from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from shared.pagination import StandardResultsPagination
from shared.permissions import has_permission, has_permission_with_step_up

from . import services
from .models import ConflictDeclaration, Meeting, Minutes, NBECMember
from .serializers import (
    AddendumCreateSerializer,
    AgendaPublishSerializer,
    AgendaSerializer,
    AttendanceSerializer,
    COIDeclareSerializer,
    COIPolicyResponseSerializer,
    COIReviewSerializer,
    ConflictDeclarationSerializer,
    MeetingCreateSerializer,
    MeetingSerializer,
    MinutesAddendumSerializer,
    MinutesSerializer,
    MinutesSignSerializer,
    NBECMemberAmendSerializer,
    NBECMemberCreateSerializer,
    NBECMemberSerializer,
)


def _ok(data, request_id="", http_status=status.HTTP_200_OK):
    return Response(
        {"success": True, "data": data, "meta": {"request_id": str(request_id)}},
        status=http_status,
    )


def _err(code, message, http_status):
    return Response(
        {"success": False, "error": {"code": code, "message": message}, "meta": {}},
        status=http_status,
    )


def _actor(request):
    return (request.auth or {}).get("sub") if request.auth else None


def _rid(request):
    return getattr(request, "request_id", "")


def _ip(request):
    return getattr(request, "ip_address", None)


# ── NBECMember ────────────────────────────────────────────────────────────────


class MemberListCreateView(APIView):
    """``GET /api/v1/nbec/members/`` — List members. ``POST`` — Create."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="List NBEC members",
        operation_id="nbec_member_list",
        responses={200: NBECMemberSerializer(many=True)},
    )
    def get(self, request):
        qs = NBECMember.objects.all().order_by("full_name")
        from shared.pagination import StandardResultsPagination

        paginator = StandardResultsPagination()
        result = paginator.paginate_queryset(qs, request)
        data = NBECMemberSerializer(result, many=True).data
        return paginator.get_paginated_response(data)

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Create NBEC member",
        operation_id="nbec_member_create",
        request=NBECMemberCreateSerializer,
        responses={201: NBECMemberSerializer},
    )
    def post(self, request):
        ser = NBECMemberCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        member = services.create_member(
            _actor(request),
            ser.validated_data,
            request_id=_rid(request),
            ip_address=_ip(request),
        )
        return _ok(
            NBECMemberSerializer(member).data, _rid(request), status.HTTP_201_CREATED
        )


class MemberDetailView(APIView):
    """``PATCH /api/v1/nbec/members/{id}/`` — Amend a member record."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    def _get_member(self, pk):
        try:
            return NBECMember.objects.get(pk=pk)
        except NBECMember.DoesNotExist:
            return None

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Amend NBEC member",
        operation_id="nbec_member_amend",
        request=NBECMemberAmendSerializer,
        responses={200: NBECMemberSerializer},
    )
    def patch(self, request, pk):
        member = self._get_member(pk)
        if not member:
            return _err("NOT_FOUND", "Member not found.", status.HTTP_404_NOT_FOUND)
        ser = NBECMemberAmendSerializer(member, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        member = services.amend_member(
            _actor(request),
            member,
            ser.validated_data,
            request_id=_rid(request),
            ip_address=_ip(request),
        )
        return _ok(NBECMemberSerializer(member).data, _rid(request))


class MemberActivateView(APIView):
    """``POST /api/v1/nbec/members/{id}/activate/`` — Activate a draft member."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Activate NBEC member",
        operation_id="nbec_member_activate",
        responses={200: NBECMemberSerializer},
    )
    def post(self, request, pk):
        try:
            member = NBECMember.objects.get(pk=pk)
        except NBECMember.DoesNotExist:
            return _err("NOT_FOUND", "Member not found.", status.HTTP_404_NOT_FOUND)
        try:
            member = services.activate_member(
                _actor(request),
                member,
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except ValueError as exc:
            return _err("VALIDATION_ERROR", str(exc), status.HTTP_400_BAD_REQUEST)
        return _ok(NBECMemberSerializer(member).data, _rid(request))


# ── ConflictDeclaration ───────────────────────────────────────────────────────


class COICreateView(APIView):
    """``POST /api/v1/nbec/coi/`` — Member declares a COI."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Declare conflict of interest",
        operation_id="nbec_coi_declare",
        request=COIDeclareSerializer,
        responses={201: ConflictDeclarationSerializer},
    )
    def post(self, request):
        ser = COIDeclareSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        coi = services.declare_coi(
            _actor(request),
            ser.validated_data,
            request_id=_rid(request),
            ip_address=_ip(request),
        )
        return _ok(
            ConflictDeclarationSerializer(coi).data,
            _rid(request),
            status.HTTP_201_CREATED,
        )


class COIReviewView(APIView):
    """``POST /api/v1/nbec/coi/{id}/review/`` — Approve or dismiss a COI."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Review COI declaration",
        operation_id="nbec_coi_review",
        request=COIReviewSerializer,
        responses={200: ConflictDeclarationSerializer},
    )
    def post(self, request, pk):
        try:
            coi = ConflictDeclaration.objects.get(pk=pk)
        except ConflictDeclaration.DoesNotExist:
            return _err(
                "NOT_FOUND", "COI declaration not found.", status.HTTP_404_NOT_FOUND
            )
        ser = COIReviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            coi = services.review_coi(
                _actor(request),
                coi,
                approve=ser.validated_data["approved"],
                review_date=ser.validated_data.get("review_date"),
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except ValueError as exc:
            return _err("VALIDATION_ERROR", str(exc), status.HTTP_400_BAD_REQUEST)
        return _ok(ConflictDeclarationSerializer(coi).data, _rid(request))


# ── Meeting ───────────────────────────────────────────────────────────────────


class MeetingCreateView(APIView):
    """``POST /api/v1/nbec/meetings/`` — Schedule a new meeting."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Schedule meeting",
        operation_id="nbec_meeting_schedule",
        request=MeetingCreateSerializer,
        responses={201: MeetingSerializer},
    )
    def post(self, request):
        ser = MeetingCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        meeting = services.schedule_meeting(
            _actor(request),
            ser.validated_data,
            request_id=_rid(request),
            ip_address=_ip(request),
        )
        return _ok(
            MeetingSerializer(meeting).data, _rid(request), status.HTTP_201_CREATED
        )


def _get_meeting(pk):
    try:
        return Meeting.objects.get(pk=pk)
    except Meeting.DoesNotExist:
        return None


class MeetingAgendaView(APIView):
    """``POST /api/v1/nbec/meetings/{id}/agenda/`` — Publish agenda version."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Publish meeting agenda",
        operation_id="nbec_meeting_agenda",
        request=AgendaPublishSerializer,
        responses={201: AgendaSerializer},
    )
    def post(self, request, pk):
        meeting = _get_meeting(pk)
        if not meeting:
            return _err("NOT_FOUND", "Meeting not found.", status.HTTP_404_NOT_FOUND)
        ser = AgendaPublishSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        agenda = services.publish_agenda(
            _actor(request),
            meeting,
            items=ser.validated_data["items"],
            document_ref=ser.validated_data.get("document_ref", ""),
            request_id=_rid(request),
            ip_address=_ip(request),
        )
        return _ok(
            AgendaSerializer(agenda).data, _rid(request), status.HTTP_201_CREATED
        )


class MeetingAttendanceView(APIView):
    """``POST /api/v1/nbec/meetings/{id}/attendance/`` — Record attendance."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Record meeting attendance",
        operation_id="nbec_meeting_attendance",
        request=AttendanceSerializer,
        responses={200: MeetingSerializer},
    )
    def post(self, request, pk):
        meeting = _get_meeting(pk)
        if not meeting:
            return _err("NOT_FOUND", "Meeting not found.", status.HTTP_404_NOT_FOUND)
        ser = AttendanceSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        meeting = services.record_attendance(
            _actor(request),
            meeting,
            attendee_ids=ser.validated_data["attendee_ids"],
            request_id=_rid(request),
            ip_address=_ip(request),
        )
        return _ok(MeetingSerializer(meeting).data, _rid(request))


class MeetingConveneView(APIView):
    """``POST /api/v1/nbec/meetings/{id}/convene/`` — Convene meeting."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Convene meeting",
        operation_id="nbec_meeting_convene",
        responses={200: MeetingSerializer},
    )
    def post(self, request, pk):
        meeting = _get_meeting(pk)
        if not meeting:
            return _err("NOT_FOUND", "Meeting not found.", status.HTTP_404_NOT_FOUND)
        try:
            meeting = services.convene_meeting(
                _actor(request),
                meeting,
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except ValueError as exc:
            return _err("VALIDATION_ERROR", str(exc), status.HTTP_400_BAD_REQUEST)
        return _ok(MeetingSerializer(meeting).data, _rid(request))


class MeetingAdjournView(APIView):
    """``POST /api/v1/nbec/meetings/{id}/adjourn/`` — Adjourn meeting."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Adjourn meeting",
        operation_id="nbec_meeting_adjourn",
        responses={200: MeetingSerializer},
    )
    def post(self, request, pk):
        meeting = _get_meeting(pk)
        if not meeting:
            return _err("NOT_FOUND", "Meeting not found.", status.HTTP_404_NOT_FOUND)
        try:
            meeting, minutes = services.adjourn_meeting(
                _actor(request),
                meeting,
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except ValueError as exc:
            return _err("VALIDATION_ERROR", str(exc), status.HTTP_400_BAD_REQUEST)
        data = MeetingSerializer(meeting).data
        data["minutes_id"] = str(minutes.id)
        return _ok(data, _rid(request))


# ── Minutes ───────────────────────────────────────────────────────────────────


def _get_minutes(pk):
    try:
        return Minutes.objects.select_related("meeting").get(pk=pk)
    except Minutes.DoesNotExist:
        return None


class MinutesSignView(APIView):
    """``POST /api/v1/nbec/minutes/{id}/sign/`` — Chair signs and seals minutes."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Sign meeting minutes",
        operation_id="nbec_minutes_sign",
        request=MinutesSignSerializer,
        responses={200: MinutesSerializer},
    )
    def post(self, request, pk):
        minutes = _get_minutes(pk)
        if not minutes:
            return _err("NOT_FOUND", "Minutes not found.", status.HTTP_404_NOT_FOUND)
        ser = MinutesSignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            minutes = services.sign_minutes(
                _actor(request),
                minutes,
                signature_ref=ser.validated_data.get("signature_ref", ""),
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except ValueError as exc:
            return _err("VALIDATION_ERROR", str(exc), status.HTTP_400_BAD_REQUEST)
        return _ok(MinutesSerializer(minutes).data, _rid(request))


class MinutesAddendumView(APIView):
    """``POST /api/v1/nbec/minutes/{id}/addendum/`` — Chair issues addendum."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Issue addendum to minutes",
        operation_id="nbec_minutes_addendum",
        request=AddendumCreateSerializer,
        responses={201: MinutesAddendumSerializer},
    )
    def post(self, request, pk):
        minutes = _get_minutes(pk)
        if not minutes:
            return _err("NOT_FOUND", "Minutes not found.", status.HTTP_404_NOT_FOUND)
        ser = AddendumCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            addendum = services.issue_addendum(
                _actor(request),
                minutes,
                content=ser.validated_data["content"],
                document_ref=ser.validated_data.get("document_ref", ""),
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except ValueError as exc:
            return _err("VALIDATION_ERROR", str(exc), status.HTTP_400_BAD_REQUEST)
        return _ok(
            MinutesAddendumSerializer(addendum).data,
            _rid(request),
            status.HTTP_201_CREATED,
        )


# ── COI Policy (internal) ──────────────────────────────────────────────────────


class COIPolicyView(APIView):
    """``GET /api/v1/nbec/policy/coi/`` — Internal: check active COI for a member/entity.

    Consumed by itembank, marking, and other NBES apps before assigning work.
    Requires ``committee:manage`` or ``audit:export`` — service principals use
    their own JWT with the relevant permission.
    """

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up("committee:manage"),
    ]

    @extend_schema(
        tags=["NBEC Committee"],
        summary="Check active COI for a member",
        operation_id="nbec_policy_coi_check",
        parameters=[
            OpenApiParameter(
                "member_id",
                str,
                required=True,
                description="Keycloak sub UUID of the member",
            ),
            OpenApiParameter(
                "entity_type",
                str,
                required=False,
                description="Entity type to check (e.g. 'item', 'candidate')",
            ),
            OpenApiParameter(
                "entity_id", str, required=False, description="Entity UUID to check"
            ),
        ],
        responses={200: COIPolicyResponseSerializer},
    )
    def get(self, request):
        member_id = request.query_params.get("member_id")
        if not member_id:
            return _err(
                "VALIDATION_ERROR",
                "'member_id' is required.",
                status.HTTP_400_BAD_REQUEST,
            )
        entity_type = request.query_params.get("entity_type", "")
        entity_id = request.query_params.get("entity_id") or None

        result = services.check_coi(member_id, entity_type, entity_id)
        ser = COIPolicyResponseSerializer(result)
        return _ok(ser.data, _rid(request))
