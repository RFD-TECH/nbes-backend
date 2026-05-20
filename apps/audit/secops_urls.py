"""apps/audit/secops_urls.py — Security Operations Console routes.
Mounted under ``/api/v1/secops/`` by ``config/urls.py``.
"""
from django.urls import path

from .secops_views import (
    AnomaliesView,
    AuthFailuresView,
    DailySummaryView,
    ThrottledIPsView,
)

app_name = "secops"

urlpatterns = [
    path("auth-failures", AuthFailuresView.as_view(), name="auth-failures"),
    path("throttled-ips", ThrottledIPsView.as_view(), name="throttled-ips"),
    path("anomalies",     AnomaliesView.as_view(),     name="anomalies"),
    path("daily-summary", DailySummaryView.as_view(),  name="daily-summary"),
]
