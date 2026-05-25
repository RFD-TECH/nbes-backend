"""Enforce SRS §2.7 "tenure_end > tenure_start" at the database.

Serializers validate this on the API path, but ``services.create_member`` /
``amend_member`` persist directly via the ORM without calling
``full_clean()``. A DB-level check makes the invariant unbypassable.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("committee", "0004_align_nbecmember_to_srs"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="nbecmember",
            constraint=models.CheckConstraint(
                condition=models.Q(tenure_end__isnull=True)
                | models.Q(tenure_end__gt=models.F("tenure_start")),
                name="tenure_end_after_start",
            ),
        ),
    ]
