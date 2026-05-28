"""Align NBECMember field names + designations to SRS Phase 2 (§2.5.1).

Renames:
  role               → designation
  appointment_date   → tenure_start
  tenure_end_date    → tenure_end
  email              → contact

Drops:
  is_active          (derivable from status; exposed as a property)
  is_voting_member   (not in SRS at all)

Adjusts the designation choice set: drops ``secretary`` (the "NBEC
Secretariat" is a Phase 1 platform role, not an NBEC member designation
per SRS §2.2.1). Any existing ``role='secretary'`` rows are coerced to
``member`` so the migration is non-destructive — operators should review
those members and reassign them to the correct Secretariat platform role
in IAM if necessary.
"""

from django.db import migrations, models


def coerce_secretary_to_member(apps, schema_editor):
    NBECMember = apps.get_model("committee", "NBECMember")
    NBECMember.objects.filter(role="secretary").update(role="member")


def noop_reverse(apps, schema_editor):
    # Forward coercion is non-reversible without external context.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("committee", "0004_alter_conflictdeclaration_subject_type"),
    ]

    operations = [
        # Drop the unique-active-Chair constraint first so we can rename the
        # field it references; re-added below against the new column name.
        migrations.RemoveConstraint(
            model_name="nbecmember",
            name="unique_active_chair",
        ),

        # Coerce any orphan secretary rows before tightening the choice set.
        migrations.RunPython(coerce_secretary_to_member, reverse_code=noop_reverse),

        # Rename to SRS field names.
        migrations.RenameField(
            model_name="nbecmember",
            old_name="role",
            new_name="designation",
        ),
        migrations.RenameField(
            model_name="nbecmember",
            old_name="appointment_date",
            new_name="tenure_start",
        ),
        migrations.RenameField(
            model_name="nbecmember",
            old_name="tenure_end_date",
            new_name="tenure_end",
        ),
        migrations.RenameField(
            model_name="nbecmember",
            old_name="email",
            new_name="contact",
        ),

        # Tighten the designation choice set per SRS §2.2.1.
        migrations.AlterField(
            model_name="nbecmember",
            name="designation",
            field=models.CharField(
                choices=[
                    ("chair", "Chair"),
                    ("deputy_chair", "Deputy Chair"),
                    ("member", "Member"),
                ],
                default="member",
                max_length=20,
            ),
        ),

        # Drop fields that are not in the SRS data model.
        migrations.RemoveField(
            model_name="nbecmember",
            name="is_active",
        ),
        migrations.RemoveField(
            model_name="nbecmember",
            name="is_voting_member",
        ),

        # Re-add the unique-active-Chair constraint against the new column.
        migrations.AddConstraint(
            model_name="nbecmember",
            constraint=models.UniqueConstraint(
                condition=models.Q(designation="chair", status="active"),
                fields=["designation"],
                name="unique_active_chair",
            ),
        ),
    ]
