"""apps/audit/urls.py — Auditor-facing endpoints.

Mounted under ``/api/v1/audit/`` by ``config/urls.py``.
"""
from django.urls import path

from .views import AuditChainView, AuditExportView, AuditSearchView


app_name = "audit"

urlpatterns = [
    path("search", AuditSearchView.as_view(), name="audit-search"),
    path("chain/<str:date>", AuditChainView.as_view(), name="audit-chain"),
    path("export", AuditExportView.as_view(), name="audit-export"),
]
