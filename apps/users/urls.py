"""apps/users/urls.py — RBAC admin and current-user routes.

Mounted twice in config/urls.py: once under ``/api/v1/admin/rbac/`` for
the matrix admin surface, once under ``/api/v1/me/`` for introspection.
"""
from django.urls import path

from .views import (
    DashboardView,
    MyPermissionsView,
    PermissionListView,
    RoleDetailView,
    RoleListCreateView,
    RolePermissionsView,
)

admin_urlpatterns = [
    path("permissions/", PermissionListView.as_view(), name="rbac-permissions-list"),
    path("roles/", RoleListCreateView.as_view(), name="rbac-roles-list-create"),
    path("roles/<uuid:pk>/", RoleDetailView.as_view(), name="rbac-role-detail"),
    path(
        "roles/<uuid:pk>/permissions/",
        RolePermissionsView.as_view(),
        name="rbac-role-permissions",
    ),
]

me_urlpatterns = [
    path("permissions/", MyPermissionsView.as_view(), name="me-permissions"),
    path("dashboard/", DashboardView.as_view(), name="me-dashboard"),
]

# Default export — mounted at /api/v1/admin/rbac/ in config/urls.py
urlpatterns = admin_urlpatterns
