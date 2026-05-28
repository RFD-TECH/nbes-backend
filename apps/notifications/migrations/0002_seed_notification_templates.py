from django.db import migrations


def seed_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")

    # 1. user.profile_ready
    NotificationTemplate.objects.get_or_create(
        event_name="user.profile_ready",
        defaults={
            "subject": "Your NBES profile is ready",
            "body_template": (
                "Hello {{ first_name }},\n\n"
                "Your NBES profile has been provisioned. You can now log in using your IAM credentials.\n"
                "Your username is your email: {{ email }}.\n\n"
                "Best regards,\n"
                "NBES Administration"
            ),
            "channel": "email",
            "is_active": True,
        }
    )

    # 2. user.bulk_import_complete
    NotificationTemplate.objects.get_or_create(
        event_name="user.bulk_import_complete",
        defaults={
            "subject": "Bulk import of user profiles complete",
            "body_template": (
                "Hello,\n\n"
                "The bulk import of user profiles from file '{{ filename }}' has completed.\n"
                "Total records: {{ total_rows }}\n"
                "Successfully imported: {{ success_count }}\n"
                "Failed records: {{ failure_count }}\n\n"
                "Best regards,\n"
                "NBES Administration"
            ),
            "channel": "email",
            "is_active": True,
        }
    )


def rollback_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    NotificationTemplate.objects.filter(event_name__in=["user.profile_ready", "user.bulk_import_complete"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_templates, rollback_templates),
    ]
