"""Adds the paper:construct, item:search, search:manage and vault:operate
codenames + grants for NBEC Secretariat / Moderator / Item Writer roles.

SRS references:
- ``paper:construct`` — SRS-NBE-F02-08 (paper construction is NBEC Secretariat)
- ``item:search``    — SRS-NBE-F02-10 (advanced search; Moderator / Secretariat
  / Item Writer all need read access scoped via RBAC)
- ``search:manage``  — SRS-NBE-F02-10 (saved searches per user, shareable
  with NBEC Secretariat)
- ``vault:operate``  — SRS-NBE-F02-07 (vault export-request / cosign flows;
  NBEC Member only)
"""
from django.db import migrations


NEW_PERMISSIONS = [
    ("paper:construct", "Construct examination papers (manual & rule-based)"),
    ("item:search", "Advanced search and retrieval over the item bank"),
    ("search:manage", "Create and share saved searches"),
    ("vault:operate", "Operate the content vault (initiate/cosign exports)"),
]

GRANTS = {
    "paper:construct": ["nbec_secretariat"],
    "item:search": ["moderator", "nbec_secretariat", "item_writer"],
    "search:manage": ["moderator", "nbec_secretariat", "item_writer"],
    "vault:operate": ["nbec_member"],
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
        ("users", "0007_dashboards_manage_codename"),
    ]

    operations = [
        migrations.RunPython(add_codenames, reverse_code=drop_codenames),
    ]
