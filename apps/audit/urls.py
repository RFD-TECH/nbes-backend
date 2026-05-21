"""apps/audit/urls.py — Auditor-facing endpoints."""
from django.urls import path

from .views import AuditChainView, AuditExportView, AuditSearchView


app_name = "audit"

urlpatterns = [
    path("search", AuditSearchView.as_view(), name="audit-search"),
    path("search/", AuditSearchView.as_view(), name="audit-search-slash"),
    path("chain/<str:date>", AuditChainView.as_view(), name="audit-chain"),
    path("chain/<str:date>/", AuditChainView.as_view(), name="audit-chain-slash"),
    path("export", AuditExportView.as_view(), name="audit-export"),
    path("export/", AuditExportView.as_view(), name="audit-export-slash"),
]
