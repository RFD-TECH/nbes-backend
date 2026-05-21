from django.db import migrations


SEEDED_ROLE_NAMES = [
    "nbec_member",
    "nbec_secretariat",
    "item_writer",
    "moderator",
    "examiner",
    "clet_registrar",
    "candidate",
    "auditor",
    "system_administrator",
]


def mark_seeded_roles(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    Role.objects.filter(name__in=SEEDED_ROLE_NAMES).update(is_custom=False)


def mark_all_roles_custom(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    Role.objects.all().update(is_custom=True)


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0003_normalize_keycloak_role_names"),
    ]

    operations = [
        # is_custom is already created in 0001_initial.py; only run the data step.
        migrations.RunPython(mark_seeded_roles, mark_all_roles_custom),
    ]
