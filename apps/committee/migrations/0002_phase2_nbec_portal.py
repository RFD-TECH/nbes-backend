# Generated for Phase 2 — NBEC Management Portal

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("committee", "0001_initial"),
    ]

    operations = [
        # ── NBECMember additions ───────────────────────────────────────────────
        migrations.AddField(
            model_name="nbecmember",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("active", "Active"),
                    ("expired", "Expired"),
                    ("renewed", "Renewed"),
                ],
                default="draft",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="nbecmember",
            name="title",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="nbecmember",
            name="post_nominals",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="nbecmember",
            name="photo_ref",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="nbecmember",
            name="instrument_ref",
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="nbecmember",
            name="role",
            field=models.CharField(
                choices=[
                    ("chair", "Chair"),
                    ("deputy_chair", "Deputy Chair"),
                    ("member", "Member"),
                    ("secretary", "Secretary"),
                ],
                default="member",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="nbecmember",
            constraint=models.UniqueConstraint(
                condition=models.Q(role="chair", status="active"),
                fields=["role"],
                name="unique_active_chair",
            ),
        ),

        # ── Meeting additions ─────────────────────────────────────────────────
        migrations.AddField(
            model_name="meeting",
            name="meeting_type",
            field=models.CharField(
                choices=[
                    ("ordinary", "Ordinary"),
                    ("extraordinary", "Extraordinary"),
                    ("closed", "Closed"),
                ],
                default="ordinary",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="meeting",
            name="chair_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="meeting",
            name="secretariat_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="meeting",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("agenda_issued", "Agenda Issued"),
                    ("scheduled", "Scheduled"),
                    ("convened", "Convened"),
                    ("adjourned", "Adjourned"),
                    ("minuted", "Minuted"),
                ],
                default="draft",
                max_length=20,
            ),
        ),

        # ── Minutes additions ─────────────────────────────────────────────────
        migrations.AddField(
            model_name="minutes",
            name="immutable_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="minutes",
            name="signature_ref",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="minutes",
            name="archive_ref",
            field=models.CharField(blank=True, max_length=200),
        ),

        # ── ConflictDeclaration additions ──────────────────────────────────────
        migrations.AddField(
            model_name="conflictdeclaration",
            name="subject_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("candidate", "Candidate"),
                    ("item_writer", "Item Writer"),
                    ("examiner", "Examiner"),
                    ("supplier", "Supplier"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="conflictdeclaration",
            name="nature",
            field=models.CharField(
                blank=True,
                choices=[
                    ("financial", "Financial"),
                    ("personal", "Personal"),
                    ("professional", "Professional"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="conflictdeclaration",
            name="effective_from",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="conflictdeclaration",
            name="review_date",
            field=models.DateField(blank=True, null=True),
        ),

        # ── ActionItem additions ───────────────────────────────────────────────
        migrations.AlterField(
            model_name="actionitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"),
                    ("in_progress", "In Progress"),
                    ("complete", "Complete"),
                    ("verified", "Verified"),
                    ("overdue", "Overdue"),
                ],
                default="open",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="actionitem",
            name="last_escalated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="actionitem",
            name="minutes",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="action_items",
                to="committee.minutes",
            ),
        ),

        # ── New model: Agenda ──────────────────────────────────────────────────
        migrations.CreateModel(
            name="Agenda",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("version", models.PositiveSmallIntegerField(default=1)),
                ("items", models.JSONField(default=list)),
                ("document_ref", models.TextField(blank=True)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_by_id", models.UUIDField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "meeting",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="agendas",
                        to="committee.meeting",
                    ),
                ),
            ],
            options={
                "db_table": "committee_agenda",
                "ordering": ["meeting", "-version"],
            },
        ),
        migrations.AlterUniqueTogether(
            name="agenda",
            unique_together={("meeting", "version")},
        ),

        # ── New model: MinutesAddendum ─────────────────────────────────────────
        migrations.CreateModel(
            name="MinutesAddendum",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("content", models.TextField()),
                ("issued_by_id", models.UUIDField()),
                ("issued_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("document_ref", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "minutes",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="addenda",
                        to="committee.minutes",
                    ),
                ),
            ],
            options={
                "db_table": "committee_minutesaddendum",
                "ordering": ["minutes", "issued_at"],
            },
        ),
    ]
