from django.db import migrations


ROLE_RENAMES = {
    "nbec-member": "nbec_member",
    "nbec-secretariat": "nbec_secretariat",
    "item-writer": "item_writer",
    "clet-registrar": "clet_registrar",
    "system-administrator": "system_administrator",
}


def normalize_role_names(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    RolePermission = apps.get_model("users", "RolePermission")

    for old_name, new_name in ROLE_RENAMES.items():
        old_role = Role.objects.filter(name=old_name).first()
        if not old_role:
            continue

        new_role = Role.objects.filter(name=new_name).first()
        if not new_role:
            old_role.name = new_name
            old_role.save(update_fields=["name"])
            continue

        for grant in RolePermission.objects.filter(role=old_role):
            RolePermission.objects.get_or_create(
                role=new_role,
                permission=grant.permission,
                defaults={"granted_by": grant.granted_by},
            )
        old_role.delete()


def restore_role_names(apps, schema_editor):
    Role = apps.get_model("users", "Role")

    for old_name, new_name in ROLE_RENAMES.items():
        role = Role.objects.filter(name=new_name).first()
        if role and not Role.objects.filter(name=old_name).exists():
            role.name = old_name
            role.save(update_fields=["name"])


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0002_repair_stale_rbac_schema"),
    ]

    operations = [
        migrations.RunPython(normalize_role_names, restore_role_names),
    ]
