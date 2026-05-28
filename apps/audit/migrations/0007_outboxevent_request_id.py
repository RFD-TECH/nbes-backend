from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0006_alter_auditevent_entity_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="outboxevent",
            name="request_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
    ]
