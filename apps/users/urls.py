"""RBAC admin and current-user routes.

Mounted twice in config/urls.py: once under ``/api/v1/admin/rbac/`` for
the matrix admin surface, once under ``/api/v1/me/`` for introspection.
The user administration routes (``/api/v1/admin/users/``) are mounted
separately in config/urls.py.
"""
from django.urls import path

from .views import (
    AdminUserDetailView,
    AdminUserListCreateView,
    AdminUserRolesView,
    BulkRoleAssignView,
    BulkUserImportView,
    DashboardView,
    MyPermissionsView,
    MyProfileView,
    PermissionListView,
    RoleAssignmentApprovalActionView,
    RoleAssignmentApprovalListView,
    RoleDetailView,
    RoleListCreateView,
    RoleMutualExclusionDetailView,
    RoleMutualExclusionListCreateView,
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
    # Mutual-exclusion rules 
    path("exclusions/", RoleMutualExclusionListCreateView.as_view(), name="rbac-exclusions-list-create"),
    path("exclusions/<uuid:pk>/", RoleMutualExclusionDetailView.as_view(), name="rbac-exclusions-detail"),
    # Two-admin approval workflow 
    path("approvals/", RoleAssignmentApprovalListView.as_view(), name="rbac-approvals-list"),
    path(
        "approvals/<uuid:pk>/<str:action>/",
        RoleAssignmentApprovalActionView.as_view(),
        name="rbac-approvals-action",
    ),
    # User profile admin
    path("users/", AdminUserListCreateView.as_view(), name="admin-users-list-create"),
    path("users/import/", BulkUserImportView.as_view(), name="admin-users-bulk-import"),
    path("users/bulk-roles/", BulkRoleAssignView.as_view(), name="admin-users-bulk-roles"),
    path("users/<uuid:pk>/", AdminUserDetailView.as_view(), name="admin-user-detail"),
    path("users/<uuid:pk>/roles/", AdminUserRolesView.as_view(), name="admin-user-roles"),
]

me_urlpatterns = [
    path("", MyProfileView.as_view(), name="me-profile"),
    path("permissions/", MyPermissionsView.as_view(), name="me-permissions"),
    path("dashboard/", DashboardView.as_view(), name="me-dashboard"),
]

# Default export — mounted at /api/v1/admin/rbac/ in config/urls.py
urlpatterns = admin_urlpatterns
