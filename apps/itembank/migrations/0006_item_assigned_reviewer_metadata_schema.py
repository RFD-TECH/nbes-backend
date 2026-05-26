import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("itembank", "0005_itemusage_candidate_count_non_negative"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Add assigned_reviewer_id FK to Item (SRS-NBE-F02-04).
        migrations.AddField(
            model_name="item",
            name="assigned_reviewer_id",
            field=django.db.models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="assigned_review_items",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Add MetadataSchema model (SRS-NBE-F02-02).
        migrations.CreateModel(
            name="MetadataSchema",
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
                ("version", models.IntegerField(unique=True)),
                ("schema_json", models.JSONField()),
                ("is_active", models.BooleanField(db_index=True, default=False)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="metadataschema",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_active=True),
                fields=["is_active"],
                name="only_one_active_metadata_schema",
            ),
        ),
    ]
