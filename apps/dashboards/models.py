"""apps/dashboards/models.py — Role-specific dashboard skeleton config.

One row per (role, panel) pairing — the role's home page is the union
of panels sharing its role name. Defaults are seeded from blueprint
§1.2.9; admins can edit ``default_config`` and ``display_order`` at
runtime through ``PATCH /api/v1/dashboard/panels/{panel_key}``.

The frontend renders. The backend just serves the contract.
"""
import uuid

from django.db import models


class DashboardPanel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    panel_key = models.CharField(
        max_length=80, unique=True, db_index=True,
        help_text="Stable identifier the frontend keys on, e.g. examiner.marking_queue.",
    )
    panel_name = models.CharField(max_length=120)
    role_codename = models.CharField(
        max_length=80, db_index=True,
        help_text=(
            "Name of the NBES Role this panel belongs to (matches "
            "``users.Role.name``)."
        ),
    )
    display_order = models.PositiveIntegerField(default=100)
    is_active = models.BooleanField(default=True)
    default_config = models.JSONField(
        default=dict, blank=True,
        help_text="Frontend-only — initial filters, layout hints, etc.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dashboards_panel"
        ordering = ["role_codename", "display_order", "panel_name"]
        verbose_name = "Dashboard Panel"

    def __str__(self):
        return f"{self.role_codename}/{self.panel_key}"
