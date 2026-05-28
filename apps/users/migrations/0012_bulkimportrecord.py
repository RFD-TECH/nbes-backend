"""Migration 0012 — BulkImportRecord model.

Tracks each bulk user import job (file hash, row-level errors, status).
Supports 7-year retention requirement from blueprint §1.2.4.

POST /api/v1/admin/users/import endpoint storage.
Partial-success / row-level error tracking.
"""
import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0011_full_permission_catalog"),
    ]

    operations = [
        migrations.CreateModel(
            name="BulkImportRecord",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="bulk_imports",
                        to="users.userprofile",
                    ),
                ),
                ("original_filename", models.CharField(max_length=255)),
                (
                    "file_hash",
                    models.CharField(
                        help_text="SHA-256 hex of the uploaded file.",
                        max_length=64,
                    ),
                ),
                (
                    "file_path",
                    models.CharField(
                        blank=True,
                        help_text="Storage path for the original file (retained 7 years).",
                        max_length=500,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processing", "Processing"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("total_rows", models.PositiveIntegerField(default=0)),
                ("success_count", models.PositiveIntegerField(default=0)),
                ("failure_count", models.PositiveIntegerField(default=0)),
                (
                    "row_errors",
                    models.JSONField(
                        default=list,
                        help_text="List of {row, email, errors} for each failed row.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "users_bulkimportrecord",
                "ordering": ["-created_at"],
            },
        ),
    ]
