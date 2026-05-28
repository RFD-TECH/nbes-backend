"""Enforce SRS §2.7 "tenure_end > tenure_start" at the database.

Serializers validate this on the API path, but ``services.create_member`` /
``amend_member`` persist directly via the ORM without calling
``full_clean()``. A DB-level check makes the invariant unbypassable.

Preflight: previous write paths bypassed validation, so an existing
production DB *may* contain rows where ``tenure_end <= tenure_start``.
``AddConstraint`` would fail at deploy time on those rows with an opaque
``CheckViolation``. The data migration below scans for offenders first
and raises a clear error naming the bad PKs so the operator can either
fix them in advance (preferred — governance data should not be silently
mutated) or, with explicit consent via the ``COMMITTEE_REPAIR_BAD_TENURE``
env var, null out the corrupt ``tenure_end`` so the constraint can apply.
"""
import os

from django.db import migrations, models


def assert_no_invalid_tenures(apps, schema_editor):
    NBECMember = apps.get_model("committee", "NBECMember")
    bad = list(
        NBECMember.objects.filter(
            tenure_end__isnull=False,
            tenure_end__lte=models.F("tenure_start"),
        ).values_list("pk", "full_name", "tenure_start", "tenure_end")
    )
    if not bad:
        return

    repair = os.environ.get("COMMITTEE_REPAIR_BAD_TENURE", "").lower() in {"1", "true", "yes"}
    if repair:
        ids = [row[0] for row in bad]
        NBECMember.objects.filter(pk__in=ids).update(tenure_end=None)
        return

    summary = "; ".join(
        f"pk={pk} name={name!r} tenure_start={ts} tenure_end={te}"
        for pk, name, ts, te in bad
    )
    raise RuntimeError(
        "Cannot add tenure_end_after_start constraint: "
        f"{len(bad)} NBECMember row(s) violate tenure_end > tenure_start. "
        "Fix them in production data first, or re-run this migration with "
        "COMMITTEE_REPAIR_BAD_TENURE=true to null out the offending "
        f"tenure_end values. Offenders: {summary}"
    )


def noop_reverse(apps, schema_editor):
    # Forward preflight is non-reversible — the constraint addition itself
    # is reversed by RemoveConstraint via Django.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("committee", "0005_align_nbecmember_to_srs"),
    ]

    operations = [
        migrations.RunPython(assert_no_invalid_tenures, reverse_code=noop_reverse),
        migrations.AddConstraint(
            model_name="nbecmember",
            constraint=models.CheckConstraint(
                condition=models.Q(tenure_end__isnull=True)
                | models.Q(tenure_end__gt=models.F("tenure_start")),
                name="tenure_end_after_start",
            ),
        ),
    ]
