"""apps/dashboards/urls.py — Role dashboard skeleton routes.

Mounted at ``/api/v1/dashboard/`` by ``config/urls.py``.
"""
from django.urls import path

from .views import MyDashboardView, PanelDetailView


app_name = "dashboards"

urlpatterns = [
    path("me", MyDashboardView.as_view(), name="dashboard-me"),
    path("panels/<str:panel_key>", PanelDetailView.as_view(), name="dashboard-panel"),
]
