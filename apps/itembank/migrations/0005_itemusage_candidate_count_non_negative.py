from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [
        ("itembank", "0004_phase3_4_indexes_and_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="itemusage",
            name="candidate_count",
            field=models.IntegerField(
                default=0,
                help_text="Number of candidates who saw this item in the referenced sitting.",
                validators=[django.core.validators.MinValueValidator(0)],
            ),
        ),
        migrations.AddConstraint(
            model_name="itemusage",
            constraint=models.CheckConstraint(
                condition=models.Q(candidate_count__gte=0),
                name="itemusage_candidate_count_non_negative",
            ),
        ),
    ]
