"""Adds the audit:search and audit:verify codenames + grants for Auditor
and Administrator. ``audit:export`` already exists from 0001_initial.

Blueprint §1.4 names these three endpoints under "Auditor / DG /
Administrator" gating. NBES doesn't model DG separately — they are an
``auditor`` for these purposes.
"""
from django.db import migrations


NEW_PERMISSIONS = [
    ("audit:search", "Search the audit trail"),
    ("audit:verify", "Verify the daily audit hash chain"),
]

GRANTS = {
    "audit:search": ["auditor", "system_administrator"],
    "audit:verify": ["auditor", "system_administrator"],
}


def add_codenames(apps, schema_editor):
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


def drop_codenames(apps, schema_editor):
    Permission = apps.get_model("users", "Permission")
    Permission.objects.filter(
        codename__in=[c for c, _ in NEW_PERMISSIONS]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0004_role_is_custom"),
    ]

    operations = [
        migrations.RunPython(add_codenames, reverse_code=drop_codenames),
    ]
