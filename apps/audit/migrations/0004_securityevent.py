"""Adds SecurityEvent — the NBES SIEM-aligned record of rejected requests
and edge throttle/block events. See apps/audit/models.py docstring."""
import uuid

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0003_dailyhashanchor_and_trigger"),
    ]

    operations = [
        migrations.CreateModel(
            name="SecurityEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("event_id", models.UUIDField(default=uuid.uuid4, unique=True)),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("auth_token_invalid", "auth_token_invalid"),
                            ("auth_token_expired", "auth_token_expired"),
                            ("auth_audience_mismatch", "auth_audience_mismatch"),
                            ("authz_denied", "authz_denied"),
                            ("throttle_applied", "throttle_applied"),
                            ("ip_blocked", "ip_blocked"),
                            ("anomaly_detected", "anomaly_detected"),
                        ],
                        db_index=True,
                        max_length=40,
                    ),
                ),
                (
                    "severity",
                    models.CharField(
                        choices=[
                            ("info", "info"),
                            ("warning", "warning"),
                            ("high", "high"),
                        ],
                        default="warning",
                        max_length=10,
                    ),
                ),
                ("indicators", models.JSONField(default=dict)),
                ("ip_address", models.GenericIPAddressField(blank=True, db_index=True, null=True)),
                ("actor_id", models.UUIDField(blank=True, db_index=True, null=True)),
                ("request_id", models.UUIDField(blank=True, null=True)),
                (
                    "occurred_at",
                    models.DateTimeField(default=django.utils.timezone.now, db_index=True),
                ),
            ],
            options={
                "verbose_name": "Security Event",
                "db_table": "audit_securityevent",
                "ordering": ["-occurred_at"],
                "indexes": [
                    models.Index(
                        fields=["category", "occurred_at"],
                        name="audit_secev_cat_occ_idx",
                    ),
                    models.Index(
                        fields=["ip_address", "occurred_at"],
                        name="audit_secev_ip_occ_idx",
                    ),
                ],
            },
        ),
    ]
