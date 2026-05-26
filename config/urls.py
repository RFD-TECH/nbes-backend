from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.permissions import AllowAny

from apps.users.urls import admin_urlpatterns as rbac_admin_urls
from apps.users.urls import me_urlpatterns as me_urls

api_patterns = [
    path("v1/admin/rbac/", include((rbac_admin_urls, "rbac-admin"))),
    path("v1/me/", include((me_urls, "me"))),
    path("v1/nbec/", include("apps.committee.urls")),
    path("v1/itembank/", include("apps.itembank.urls")),
    path("v1/sitting/", include("apps.sitting.urls")),
    path("v1/registration/", include("apps.registration.urls")),
    path("v1/marking/", include("apps.marking.urls")),
    path("v1/results/", include("apps.results.urls")),
    path("v1/resit/", include("apps.resit.urls")),
    path("v1/cert-trigger/", include("apps.cert_trigger.urls")),
    path("v1/notifications/", include("apps.notifications.urls")),
    path("v1/audit/", include("apps.audit.urls")),
    path("v1/secops/", include("apps.audit.secops_urls")),
    path("v1/dashboard/", include("apps.dashboards.urls")),
    path("v1/sla/", include("apps.sla.urls")),
    path("v1/reporting/", include("apps.reporting.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    # API docs (with and without api/ prefix)
    path(
        "schema/",
        SpectacularAPIView.as_view(permission_classes=[AllowAny]),
        name="schema",
    ),
    path(
        "docs/",
        SpectacularSwaggerView.as_view(
            url_name="schema", permission_classes=[AllowAny]
        ),
        name="swagger-ui",
    ),
    path(
        "redoc/",
        SpectacularRedocView.as_view(url_name="schema", permission_classes=[AllowAny]),
        name="redoc",
    ),
    path("api/schema/", SpectacularAPIView.as_view(permission_classes=[AllowAny])),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(
            url_name="schema", permission_classes=[AllowAny]
        ),
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema", permission_classes=[AllowAny]),
    ),
    # Include API patterns natively and with api/ prefix for testing backwards compatibility
    path("", include(api_patterns)),
    path("api/", include(api_patterns)),
]
