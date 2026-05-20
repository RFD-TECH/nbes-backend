from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("committee", "0002_phase2_nbec_portal"),
    ]

    operations = [
        migrations.AlterField(
            model_name="nbecmember",
            name="is_active",
            field=models.BooleanField(default=False),
        ),
    ]
