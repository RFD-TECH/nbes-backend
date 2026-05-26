"""Migration 0010 — RoleMutualExclusion + RoleAssignmentApproval models.

Creates two new tables and seeds the canonical SRS §1.2.2 exclusion pairs.

RoleMutualExclusion — two roles that cannot coexist on one profile.
RoleAssignmentApproval — pending two-admin approval for high-privilege roles.
"""
import uuid

import django.db.models.deletion
from django.db import migrations, models


# Canonical exclusion pairs from SRS §1.2.2.
# Each tuple is (role_a_name, role_b_name, reason).
# role_a_name MUST sort before role_b_name (enforced by RoleMutualExclusionCreateSerializer).
EXCLUSION_PAIRS = [
    ("item_writer", "moderator", "Conflict of interest: item authors cannot moderate their own items (SRS §1.2.2)"),
    ("candidate", "director_general", "Executive roles cannot be candidates (SRS §1.2.2)"),
    ("candidate", "invigilator", "Cannot invigilate an exam you are sitting (SRS §1.2.2)"),
    ("candidate", "nbec_member", "Board members cannot be candidates (SRS §1.2.2)"),
    ("candidate", "system_administrator", "Administrators cannot be candidates (SRS §1.2.2)"),
]


def seed_exclusions(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    RoleMutualExclusion = apps.get_model("users", "RoleMutualExclusion")

    missing = []
    for role_a_name, role_b_name, reason in EXCLUSION_PAIRS:
        try:
            role_a = Role.objects.get(name=role_a_name)
        except Role.DoesNotExist:
            missing.append(role_a_name)
            continue
        try:
            role_b = Role.objects.get(name=role_b_name)
        except Role.DoesNotExist:
            missing.append(role_b_name)
            continue
        RoleMutualExclusion.objects.get_or_create(
            role_a=role_a,
            role_b=role_b,
            defaults={"reason": reason},
        )

    if missing:
        raise RuntimeError(
            f"Cannot seed exclusion pairs — roles not found: {sorted(set(missing))}"
        )


def remove_exclusions(apps, schema_editor):
    """No-op: the table is dropped in the forward migration's DeleteModel."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0009_remove_userprofile_role_role_is_internal_and_more"),
    ]

    operations = [
        # ── RoleMutualExclusion ───────────────────────────────────────────────
        migrations.CreateModel(
            name="RoleMutualExclusion",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "reason",
                    models.CharField(
                        blank=True,
                        max_length=255,
                        help_text="Human-readable explanation for the exclusion rule.",
                    ),
                ),
                (
                    "created_by",
                    models.UUIDField(
                        blank=True, null=True,
                        help_text="Actor UUID who defined this exclusion.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "role_a",
                    models.ForeignKey(
                        help_text="The role whose name sorts first alphabetically in the pair.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="exclusions_as_a",
                        to="users.role",
                    ),
                ),
                (
                    "role_b",
                    models.ForeignKey(
                        help_text="The role whose name sorts second alphabetically in the pair.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="exclusions_as_b",
                        to="users.role",
                    ),
                ),
            ],
            options={"db_table": "users_rolemutualexclusion", "ordering": ["role_a__name", "role_b__name"]},
        ),
        migrations.AlterUniqueTogether(
            name="rolemutualexclusion",
            unique_together={("role_a", "role_b")},
        ),
        # ── RoleAssignmentApproval ────────────────────────────────────────────
        migrations.CreateModel(
            name="RoleAssignmentApproval",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("effective_from", models.DateField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                            ("expired", "Expired"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=10,
                    ),
                ),
                (
                    "reason",
                    models.TextField(
                        blank=True,
                        help_text="Reason provided by the requesting administrator.",
                    ),
                ),
                (
                    "review_note",
                    models.TextField(
                        blank=True,
                        help_text="Note added by the reviewing administrator.",
                    ),
                ),
                (
                    "expires_at",
                    models.DateTimeField(
                        help_text="48 hours after creation. Celery marks expired records automatically.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "requested_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="role_approval_requests",
                        to="users.userprofile",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="role_approval_reviews",
                        to="users.userprofile",
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="users.role",
                    ),
                ),
                (
                    "target_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pending_role_approvals",
                        to="users.userprofile",
                    ),
                ),
            ],
            options={"db_table": "users_roleassignmentapproval", "ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="roleassignmentapproval",
            index=models.Index(fields=["status", "expires_at"], name="users_roleap_status_exp_idx"),
        ),
        migrations.AddIndex(
            model_name="roleassignmentapproval",
            index=models.Index(fields=["target_user", "status"], name="users_roleap_target_sta_idx"),
        ),
        # ── Seed canonical exclusion pairs ────────────────────────────────────
        migrations.RunPython(seed_exclusions, reverse_code=remove_exclusions),
    ]
