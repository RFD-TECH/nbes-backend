"""apps/audit/urls.py — Audit trail endpoints."""
from django.urls import path

from .views import AuditChainView, AuditSearchView

urlpatterns = [
    path("search/", AuditSearchView.as_view(), name="audit-search"),
    path("chain/<str:date_str>/", AuditChainView.as_view(), name="audit-chain"),
]
