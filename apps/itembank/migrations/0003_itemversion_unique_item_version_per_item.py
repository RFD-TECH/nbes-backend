from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('itembank', '0002_alter_item_cognitive_level_alter_item_difficulty_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RenameField(
                    model_name="itemversion",
                    old_name="item",
                    new_name="item_id",
                ),
                migrations.RenameField(
                    model_name="itemversion",
                    old_name="version",
                    new_name="version_no",
                ),
                migrations.AlterField(
                    model_name="itemversion",
                    name="item_id",
                    field=models.ForeignKey(
                        db_column="item_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="versions",
                        to="itembank.item",
                    ),
                ),
                migrations.AlterModelOptions(
                    name="itemversion",
                    options={
                        "db_table": "itembank_itemversion",
                        "ordering": ["-version_no"],
                    },
                ),
                migrations.AlterUniqueTogether(
                    name="itemversion",
                    unique_together=set(),
                ),
                migrations.AddConstraint(
                    model_name="itemversion",
                    constraint=models.UniqueConstraint(
                        fields=("item_id", "version_no"),
                        name="unique_item_version_per_item",
                    ),
                ),
            ],
        ),
    ]
