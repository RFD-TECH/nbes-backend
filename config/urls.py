from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),

    # API docs
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),

    # ── API v1 ────────────────────────────────────────────────────────────────
    path("api/v1/committee/",    include("apps.committee.urls")),
    path("api/v1/itembank/",     include("apps.itembank.urls")),
    path("api/v1/sitting/",      include("apps.sitting.urls")),
    path("api/v1/registration/", include("apps.registration.urls")),
    path("api/v1/marking/",      include("apps.marking.urls")),
    path("api/v1/results/",      include("apps.results.urls")),
    path("api/v1/resit/",        include("apps.resit.urls")),
    path("api/v1/cert-trigger/", include("apps.cert_trigger.urls")),
    path("api/v1/notifications/",include("apps.notifications.urls")),
    path("api/v1/audit/",        include("apps.audit.urls")),
    path("api/v1/sla/",          include("apps.sla.urls")),
    path("api/v1/reporting/",    include("apps.reporting.urls")),
]
