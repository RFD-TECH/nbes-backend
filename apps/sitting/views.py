from __future__ import annotations

"""
Endpoints (mounted at ``/api/v1/sittings/`` — see ``urls.py``):

* ``GET    /``                                       — list sittings
* ``POST   /``                                       — create draft sitting
* ``GET    /{ref}/``                                 — retrieve
* ``PATCH  /{ref}/``                                 — edit draft
* ``DELETE /{ref}/``                                 — delete draft (forbidden once locked)
* ``GET    /{ref}/snapshot/``                        — frozen read-only snapshot
* ``POST   /{ref}/papers/``                          — add/update SubjectPaper by subject_code
* ``DELETE /{ref}/papers/{paper_id}/``               — remove paper (pre-lock)
* ``POST   /{ref}/configure/``                       — DRAFT → CONFIGURED (validation)
* ``POST   /{ref}/approve/``                         — record NBEC meeting approval
* ``POST   /{ref}/lock/``                            — manual lock (override path)
* ``POST   /{ref}/amend/``                           — Chair non-critical amendment
* ``POST   /{ref}/amend-critical/``                  — NBEC-resolution amendment
* ``POST   /{ref}/activate/``                        — LOCKED → ACTIVE
* ``POST   /{ref}/close/``                           — ACTIVE → CLOSED
* ``GET    /{ref}/lock-events/``                     — append-only amendment history
* ``POST   /papers/{paper_id}/variants/generate/``   — deterministic variant generator
* ``GET    /papers/{paper_id}/variants/``            — list paper variants
* ``GET    /blueprint-versions/{subject_code}/``     — list versions for a subject
* ``POST   /blueprint-versions/{subject_code}/``     — publish a new version
"""
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from shared.pagination import StandardResultsPagination
from shared.permissions import has_permission, has_permission_with_step_up

from . import services
from .models import (
    BlueprintVersion,
    Sitting,
    SittingLockEvent,
    SubjectPaper,
    Variant,
)
from .permissions import PERM_SITTING_CONFIGURE, PERM_SITTING_LOCK_OVERRIDE
from .serializers import (
    BlueprintVersionPublishSerializer,
    BlueprintVersionSerializer,
    SittingAmendCriticalSerializer,
    SittingAmendNonCriticalSerializer,
    SittingApproveSerializer,
    SittingCreateSerializer,
    SittingLockEventSerializer,
    SittingSerializer,
    SittingSnapshotSerializer,
    SittingUpdateSerializer,
    SubjectPaperSerializer,
    SubjectPaperUpsertSerializer,
    VariantGenerateSerializer,
    VariantSerializer,
)
from .variants import generate_variants


# ── Envelope helpers ───────────────────────────────────────────────────────


def _ok(data, request_id="", http_status=status.HTTP_200_OK):
    return Response(
        {"success": True, "data": data, "meta": {"request_id": str(request_id)}},
        status=http_status,
    )


def _err(code, message, http_status, *, details=None, request_id=""):
    body = {"code": code, "message": message}
    if details:
        body["details"] = details
    return Response(
        {"success": False, "error": body, "meta": {"request_id": str(request_id)}},
        status=http_status,
    )


def _actor(request):
    return (request.auth or {}).get("sub") if request.auth else None


def _rid(request):
    return getattr(request, "request_id", "")


def _ip(request):
    return getattr(request, "ip_address", None)


def _handle_validation_error(exc: services.SittingValidationError, request_id):
    return _err(
        exc.code,
        exc.message,
        status.HTTP_400_BAD_REQUEST,
        details=exc.details,
        request_id=request_id,
    )


def _get_sitting(ref: str) -> Sitting | None:
    try:
        return Sitting.objects.get(pk=ref)
    except Sitting.DoesNotExist:
        return None


def _get_paper(paper_id) -> SubjectPaper | None:
    try:
        return SubjectPaper.objects.select_related("sitting").get(pk=paper_id)
    except SubjectPaper.DoesNotExist:
        return None


# ── Sitting list / create ──────────────────────────────────────────────────


class SittingListCreateView(APIView):
    """``GET /sittings/`` — list. ``POST`` — create draft."""

    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="List sittings",
        operation_id="sitting_list",
        responses={200: SittingSerializer(many=True)},
    )
    def get(self, request):
        qs = Sitting.objects.all().order_by("-sitting_date")
        paginator = StandardResultsPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(SittingSerializer(page, many=True).data)

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Create draft sitting",
        operation_id="sitting_create",
        request=SittingCreateSerializer,
        responses={201: SittingSerializer},
    )
    def post(self, request):
        ser = SittingCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            sitting = services.create_sitting(
                _actor(request),
                ser.validated_data,
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except services.SittingValidationError as exc:
            return _handle_validation_error(exc, _rid(request))
        return _ok(
            SittingSerializer(sitting).data, _rid(request), status.HTTP_201_CREATED
        )


# ── Sitting detail ─────────────────────────────────────────────────────────


class SittingDetailView(APIView):
    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Retrieve sitting",
        operation_id="sitting_retrieve",
        responses={200: SittingSerializer},
    )
    def get(self, request, ref):
        sitting = _get_sitting(ref)
        if not sitting:
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        return _ok(SittingSerializer(sitting).data, _rid(request))

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Edit draft sitting",
        operation_id="sitting_update_draft",
        request=SittingUpdateSerializer,
        responses={200: SittingSerializer},
    )
    def patch(self, request, ref):
        sitting = _get_sitting(ref)
        if not sitting:
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        ser = SittingUpdateSerializer(sitting, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        try:
            sitting = services.update_sitting_draft(
                _actor(request),
                sitting,
                ser.validated_data,
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except services.SittingValidationError as exc:
            return _handle_validation_error(exc, _rid(request))
        return _ok(SittingSerializer(sitting).data, _rid(request))

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Delete draft sitting",
        operation_id="sitting_delete",
    )
    def delete(self, request, ref):
        sitting = _get_sitting(ref)
        if not sitting:
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        if sitting.is_locked:
            return _err(
                "CONFIGURATION_LOCKED",
                "Locked sittings cannot be deleted.",
                status.HTTP_400_BAD_REQUEST,
            )
        sitting.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Snapshot (downstream consumer view) ────────────────────────────────────


class SittingSnapshotView(APIView):
    """Frozen read-only view consumed by Phases 5, 6, 9, 10."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Get sitting snapshot",
        operation_id="sitting_snapshot",
        responses={200: SittingSnapshotSerializer},
    )
    def get(self, request, ref):
        if not _get_sitting(ref):
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        snapshot = services.get_sitting_snapshot(ref)
        return _ok(snapshot, _rid(request))


# ── SubjectPaper ───────────────────────────────────────────────────────────


class SubjectPaperUpsertView(APIView):
    """``POST /sittings/{ref}/papers/`` — add or update a SubjectPaper."""

    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Add or update subject paper",
        operation_id="sitting_paper_upsert",
        request=SubjectPaperUpsertSerializer,
        responses={200: SubjectPaperSerializer},
    )
    def post(self, request, ref):
        sitting = _get_sitting(ref)
        if not sitting:
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        ser = SubjectPaperUpsertSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            paper = services.add_or_update_paper(
                _actor(request),
                sitting,
                ser.validated_data,
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except services.SittingValidationError as exc:
            return _handle_validation_error(exc, _rid(request))
        return _ok(SubjectPaperSerializer(paper).data, _rid(request))


class SubjectPaperDeleteView(APIView):
    """``DELETE /sittings/{ref}/papers/{paper_id}/`` — remove a paper."""

    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Remove subject paper",
        operation_id="sitting_paper_delete",
    )
    def delete(self, request, ref, paper_id):
        paper = _get_paper(paper_id)
        if not paper or str(paper.sitting_id) != ref:
            return _err(
                "NOT_FOUND",
                "Paper not found in this sitting.",
                status.HTTP_404_NOT_FOUND,
            )
        try:
            services.remove_paper(
                _actor(request),
                paper,
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except services.SittingValidationError as exc:
            return _handle_validation_error(exc, _rid(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Lifecycle transitions ──────────────────────────────────────────────────


class _TransitionView(APIView):
    """Common shape for state-transition endpoints."""

    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]
    transition: str = ""

    def _do(self, request, ref, fn, **kwargs):
        sitting = _get_sitting(ref)
        if not sitting:
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        try:
            sitting = fn(
                _actor(request),
                sitting,
                request_id=_rid(request),
                ip_address=_ip(request),
                **kwargs,
            )
        except services.SittingValidationError as exc:
            return _handle_validation_error(exc, _rid(request))
        return _ok(SittingSerializer(sitting).data, _rid(request))


class SittingConfigureView(_TransitionView):
    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Configure sitting (DRAFT → CONFIGURED)",
        operation_id="sitting_configure",
        request=None,
        responses={200: SittingSerializer},
    )
    def post(self, request, ref):
        return self._do(request, ref, services.configure_sitting)


class SittingApproveView(_TransitionView):
    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Record NBEC approval (CONFIGURED, pre-lock)",
        operation_id="sitting_approve",
        request=SittingApproveSerializer,
        responses={200: SittingSerializer},
    )
    def post(self, request, ref):
        ser = SittingApproveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return self._do(
            request,
            ref,
            services.approve_sitting,
            meeting_id=ser.validated_data["meeting_id"],
        )


class SittingLockView(APIView):
    """``POST /sittings/{ref}/lock/`` — manual lock (requires override permission)."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up(PERM_SITTING_LOCK_OVERRIDE),
    ]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Manually lock sitting (override path)",
        operation_id="sitting_lock_manual",
        request=None,
        responses={200: SittingSerializer},
    )
    def post(self, request, ref):
        sitting = _get_sitting(ref)
        if not sitting:
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        justification = request.data.get("justification", "")
        try:
            # A manual lock IS the lock event (not an amendment), so the
            # SittingLockEvent must use the snapshot-bearing AUTO_LOCK kind —
            # otherwise get_sitting_snapshot would fall back to live state
            # and the manual-lock path would lose immutability. The "manual
            # override" provenance lives on SittingLock.override and the
            # audit trail, not on the event kind.
            sitting = services.lock_sitting(
                _actor(request),
                sitting,
                kind=SittingLockEvent.Kind.AUTO_LOCK,
                justification=justification or "Manual lock by authorised override.",
                override=True,
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except services.SittingValidationError as exc:
            return _handle_validation_error(exc, _rid(request))
        return _ok(SittingSerializer(sitting).data, _rid(request))


class SittingActivateView(_TransitionView):
    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Activate sitting (LOCKED → ACTIVE)",
        operation_id="sitting_activate",
        request=None,
        responses={200: SittingSerializer},
    )
    def post(self, request, ref):
        return self._do(request, ref, services.activate_sitting)


class SittingCloseView(_TransitionView):
    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Close sitting (ACTIVE → CLOSED)",
        operation_id="sitting_close",
        request=None,
        responses={200: SittingSerializer},
    )
    def post(self, request, ref):
        return self._do(request, ref, services.close_sitting)


# ── Amendments ─────────────────────────────────────────────────────────────


class SittingAmendNonCriticalView(APIView):
    """``POST /sittings/{ref}/amend/`` — Chair non-critical amendment."""

    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Chair non-critical post-lock amendment",
        operation_id="sitting_amend_non_critical",
        request=SittingAmendNonCriticalSerializer,
        responses={200: SittingSerializer},
    )
    def post(self, request, ref):
        sitting = _get_sitting(ref)
        if not sitting:
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        ser = SittingAmendNonCriticalSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            sitting = services.amend_non_critical(
                _actor(request),
                sitting,
                changes=ser.validated_data["changes"],
                justification=ser.validated_data["justification"],
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except services.SittingValidationError as exc:
            return _handle_validation_error(exc, _rid(request))
        return _ok(SittingSerializer(sitting).data, _rid(request))


class SittingAmendCriticalView(APIView):
    """``POST /sittings/{ref}/amend-critical/`` — NBEC-resolution amendment."""

    permission_classes = [
        IsAuthenticated,
        has_permission_with_step_up(PERM_SITTING_LOCK_OVERRIDE),
    ]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="NBEC-resolution post-lock amendment",
        operation_id="sitting_amend_critical",
        request=SittingAmendCriticalSerializer,
        responses={200: SittingSerializer},
    )
    def post(self, request, ref):
        sitting = _get_sitting(ref)
        if not sitting:
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        ser = SittingAmendCriticalSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            sitting = services.amend_critical_with_resolution(
                _actor(request),
                sitting,
                changes=ser.validated_data["changes"],
                resolution_ref=ser.validated_data["resolution_ref"],
                justification=ser.validated_data["justification"],
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except services.SittingValidationError as exc:
            return _handle_validation_error(exc, _rid(request))
        return _ok(SittingSerializer(sitting).data, _rid(request))


# ── Lock event history ────────────────────────────────────────────────────-


class SittingLockEventListView(APIView):
    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="List lock + amendment events",
        operation_id="sitting_lock_events",
        responses={200: SittingLockEventSerializer(many=True)},
    )
    def get(self, request, ref):
        if not _get_sitting(ref):
            return _err("NOT_FOUND", "Sitting not found.", status.HTTP_404_NOT_FOUND)
        events = SittingLockEvent.objects.filter(sitting_id=ref).order_by(
            "-occurred_at"
        )
        return _ok(SittingLockEventSerializer(events, many=True).data, _rid(request))


# ── Variants ──────────────────────────────────────────────────────────────-


class VariantGenerateView(APIView):
    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Generate paper variants",
        operation_id="sitting_variant_generate",
        request=VariantGenerateSerializer,
        responses={200: VariantSerializer(many=True)},
    )
    def post(self, request, paper_id):
        paper = _get_paper(paper_id)
        if not paper:
            return _err("NOT_FOUND", "Paper not found.", status.HTTP_404_NOT_FOUND)
        ser = VariantGenerateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            result = generate_variants(
                _actor(request),
                paper,
                count=ser.validated_data["count"],
                seeds=ser.validated_data.get("seeds"),
                request_id=_rid(request),
                ip_address=_ip(request),
            )
        except ValueError as exc:
            return _err(
                "BAD_REQUEST",
                str(exc),
                status.HTTP_400_BAD_REQUEST,
                request_id=_rid(request),
            )
        return _ok(
            {
                "created": VariantSerializer(result.created, many=True).data,
                "rejected": VariantSerializer(result.rejected, many=True).data,
                "used_validator_stub": result.used_validator_stub,
            },
            _rid(request),
        )


class VariantListView(APIView):
    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="List paper variants",
        operation_id="sitting_variant_list",
        responses={200: VariantSerializer(many=True)},
    )
    def get(self, request, paper_id):
        if not _get_paper(paper_id):
            return _err("NOT_FOUND", "Paper not found.", status.HTTP_404_NOT_FOUND)
        variants = Variant.objects.filter(paper_id=paper_id).order_by("variant_no")
        return _ok(VariantSerializer(variants, many=True).data, _rid(request))


# ── BlueprintVersion ──────────────────────────────────────────────────────-


class BlueprintVersionListPublishView(APIView):
    permission_classes = [IsAuthenticated, has_permission(PERM_SITTING_CONFIGURE)]

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="List blueprint versions for a subject",
        operation_id="blueprint_version_list",
        responses={200: BlueprintVersionSerializer(many=True)},
    )
    def get(self, request, subject_code):
        versions = BlueprintVersion.objects.filter(
            subject_code=subject_code,
        ).order_by("-version_no")
        return _ok(BlueprintVersionSerializer(versions, many=True).data, _rid(request))

    @extend_schema(
        tags=["Phase 4 — Sitting"],
        summary="Publish a new blueprint version",
        operation_id="blueprint_version_publish",
        request=BlueprintVersionPublishSerializer,
        responses={201: BlueprintVersionSerializer},
    )
    def post(self, request, subject_code):
        ser = BlueprintVersionPublishSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        version = services.publish_blueprint_version(
            _actor(request),
            subject_code,
            ser.validated_data,
            request_id=_rid(request),
            ip_address=_ip(request),
        )
        return _ok(
            BlueprintVersionSerializer(version).data,
            _rid(request),
            status.HTTP_201_CREATED,
        )
