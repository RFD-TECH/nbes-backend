"""Adds the security_officer role and the secops:view codename.

Blueprint §1.12 names this role and the dashboard that's gated by the
codename. ``system_administrator`` also gets the grant so on-call admins
can see the SOC view without a role switch.
"""
from django.db import migrations


NEW_ROLES = [
    ("security_officer", "Security Operations Officer"),
]

NEW_PERMISSIONS = [
    ("secops:view", "View the Security Operations Console"),
]

GRANTS = {
    "secops:view": ["security_officer", "system_administrator"],
}


def add_codename_and_role(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    Permission = apps.get_model("users", "Permission")
    RolePermission = apps.get_model("users", "RolePermission")

    for name, description in NEW_ROLES:
        Role.objects.get_or_create(
            name=name,
            defaults={"description": description, "is_custom": False},
        )

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


def drop_codename_and_role(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    Permission = apps.get_model("users", "Permission")
    Permission.objects.filter(
        codename__in=[c for c, _ in NEW_PERMISSIONS]
    ).delete()
    Role.objects.filter(
        name__in=[n for n, _ in NEW_ROLES]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0005_audit_search_codenames"),
    ]

    operations = [
        migrations.RunPython(add_codename_and_role, reverse_code=drop_codename_and_role),
    ]
