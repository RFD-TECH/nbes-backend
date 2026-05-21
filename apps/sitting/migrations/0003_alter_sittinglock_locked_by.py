from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sitting", "0002_phase4_blueprint_variant_lockevent"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sittinglock",
            name="locked_by",
            field=models.CharField(default="system", max_length=64),
        ),
    ]
