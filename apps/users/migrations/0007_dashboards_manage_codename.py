"""Adds the dashboards:manage codename + grant to system_administrator.

Powers ``PATCH /api/v1/dashboard/panels/{panel_key}`` so an admin can
reorder, hide, or tweak the ``default_config`` of any panel without a
redeploy. The seed in apps/dashboards/migrations/0001_initial.py provides
the rows themselves; this migration only governs who may edit them.
"""
from django.db import migrations


NEW_PERMISSIONS = [
    ("dashboards:manage", "Manage role dashboard panel configuration"),
]

GRANTS = {
    "dashboards:manage": ["system_administrator"],
}


def add(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    Permission = apps.get_model("users", "Permission")
    RolePermission = apps.get_model("users", "RolePermission")

    for codename, description in NEW_PERMISSIONS:
        Permission.objects.get_or_create(
            codename=codename,
            defaults={"description": description},
        )

    for codename, role_names in GRANTS.items():
        try:
            permission = Permission.objects.get(codename=codename)
        except Permission.DoesNotExist:
            continue
        for role_name in role_names:
            try:
                role = Role.objects.get(name=role_name)
            except Role.DoesNotExist:
                continue
            RolePermission.objects.get_or_create(role=role, permission=permission)


def drop(apps, schema_editor):
    Permission = apps.get_model("users", "Permission")
    Permission.objects.filter(
        codename__in=[c for c, _ in NEW_PERMISSIONS]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0006_secops_role_and_codename"),
    ]

    operations = [
        migrations.RunPython(add, reverse_code=drop),
    ]
