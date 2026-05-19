from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('audit', '0002_alter_auditevent_entity_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='DailyHashAnchor',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(unique=True)),
                ('head_hash', models.CharField(max_length=64)),
                ('event_count', models.PositiveIntegerField(default=0)),
                ('exported_to_s22_at', models.DateTimeField(blank=True, null=True)),
                ('anchor_ref', models.CharField(blank=True, max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'audit_dailyhashanchor',
                'ordering': ['-date'],
            },
        ),
    ]
