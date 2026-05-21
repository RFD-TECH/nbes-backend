"""apps/sitting/urls.py — Phase 4 URL routing.

Mounted in ``config/urls.py`` at ``/api/v1/sitting/``.
"""
from django.urls import path

from .views import (
    BlueprintVersionListPublishView,
    SittingActivateView,
    SittingAmendCriticalView,
    SittingAmendNonCriticalView,
    SittingApproveView,
    SittingCloseView,
    SittingConfigureView,
    SittingDetailView,
    SittingListCreateView,
    SittingLockEventListView,
    SittingLockView,
    SittingSnapshotView,
    SubjectPaperDeleteView,
    SubjectPaperUpsertView,
    VariantGenerateView,
    VariantListView,
)

app_name = "sitting"

urlpatterns = [
    # ── Sitting collection / detail ────────────────────────────────────────
    path("sittings/", SittingListCreateView.as_view(), name="sitting-list"),
    path("sittings/<str:ref>/", SittingDetailView.as_view(), name="sitting-detail"),
    path(
        "sittings/<str:ref>/snapshot/",
        SittingSnapshotView.as_view(), name="sitting-snapshot",
    ),

    # ── Lifecycle transitions ──────────────────────────────────────────────
    path(
        "sittings/<str:ref>/configure/",
        SittingConfigureView.as_view(), name="sitting-configure",
    ),
    path(
        "sittings/<str:ref>/approve/",
        SittingApproveView.as_view(), name="sitting-approve",
    ),
    path(
        "sittings/<str:ref>/lock/",
        SittingLockView.as_view(), name="sitting-lock",
    ),
    path(
        "sittings/<str:ref>/activate/",
        SittingActivateView.as_view(), name="sitting-activate",
    ),
    path(
        "sittings/<str:ref>/close/",
        SittingCloseView.as_view(), name="sitting-close",
    ),

    # ── Amendments ─────────────────────────────────────────────────────────
    path(
        "sittings/<str:ref>/amend/",
        SittingAmendNonCriticalView.as_view(), name="sitting-amend",
    ),
    path(
        "sittings/<str:ref>/amend-critical/",
        SittingAmendCriticalView.as_view(), name="sitting-amend-critical",
    ),
    path(
        "sittings/<str:ref>/lock-events/",
        SittingLockEventListView.as_view(), name="sitting-lock-events",
    ),

    # ── SubjectPaper ───────────────────────────────────────────────────────
    path(
        "sittings/<str:ref>/papers/",
        SubjectPaperUpsertView.as_view(), name="sitting-paper-upsert",
    ),
    path(
        "sittings/<str:ref>/papers/<uuid:paper_id>/",
        SubjectPaperDeleteView.as_view(), name="sitting-paper-delete",
    ),

    # ── Variants ───────────────────────────────────────────────────────────
    path(
        "papers/<uuid:paper_id>/variants/",
        VariantListView.as_view(), name="variant-list",
    ),
    path(
        "papers/<uuid:paper_id>/variants/generate/",
        VariantGenerateView.as_view(), name="variant-generate",
    ),

    # ── BlueprintVersion ───────────────────────────────────────────────────
    path(
        "blueprint-versions/<str:subject_code>/",
        BlueprintVersionListPublishView.as_view(), name="blueprint-version",
    ),
]
