"""Initial RBAC schema + seed of the NBES role/permission matrix.

The seed mirrors the matrix in nbes-backend SKILL.md §3 so day-one behaviour
matches the previous hardcoded ROLE_PERMISSION_MAP. Edits after the seed are
made via the admin API and do not require a migration.
"""
import uuid
from django.db import migrations, models


# (codename, description)
PERMISSIONS = [
    ("item:create",                       "Author exam items"),
    ("item:approve",                      "Approve items into the bank"),
    ("item:vault:export",                 "Export vault content"),
    ("sitting:configure",                 "Configure sitting cycles"),
    ("sitting:lock:override",             "Override T-30 sitting lock"),
    ("registration:eligibility:override", "Override candidate eligibility decision"),
    ("registration:self",                 "Register self as candidate"),
    ("marking:moderate",                  "Moderate marked scripts"),
    ("marking:second_mark",               "Perform second marking"),
    ("marking:arbitrate",                 "Arbitrate borderline scripts"),
    ("results:ratify",                    "Ratify results at Board level"),
    ("results:publish:approve",           "Approve results for publication"),
    ("results:view:own",                  "View own results"),
    ("resit:register",                    "Register a resit attempt"),
    ("resit:exception:grant",             "Grant a resit exception"),
    ("cert:trigger",                      "Trigger certificate issuance"),
    ("audit:export",                      "Export audit trail"),
    ("committee:manage",                  "Manage committee operations"),
    ("sla:view",                          "View SLA dashboards"),
    ("reporting:view",                    "View reporting dashboards"),
    ("rbac:manage",                       "Manage NBES role-permission matrix"),
]

# (role_name, description)
ROLES = [
    ("nbec_member",          "NBEC Member"),
    ("nbec_secretariat",     "NBEC Secretariat"),
    ("item_writer",          "Item Writer"),
    ("moderator",            "Moderator"),
    ("examiner",             "Examiner"),
    ("clet_registrar",       "CLET Registrar"),
    ("candidate",            "Candidate"),
    ("auditor",              "Auditor"),
    ("system_administrator", "NBES System Administrator"),
]

# permission codename -> list of role names that hold it.
# Source of truth: nbes-backend SKILL.md §3 RBAC Matrix.
MATRIX = {
    "item:create":                       ["item_writer"],
    "item:approve":                      ["nbec_member", "moderator"],
    "item:vault:export":                 ["nbec_member"],
    "sitting:configure":                 ["nbec_member"],
    "sitting:lock:override":             ["nbec_member"],
    "registration:eligibility:override": ["clet_registrar"],
    "registration:self":                 ["candidate"],
    "marking:moderate":                  ["moderator"],
    "marking:second_mark":               ["examiner"],
    "marking:arbitrate":                 ["nbec_member"],
    "results:ratify":                    ["nbec_member"],
    "results:publish:approve":           ["clet_registrar"],
    "results:view:own":                  ["candidate"],
    "resit:register":                    ["candidate"],
    "resit:exception:grant":             ["nbec_member"],
    "cert:trigger":                      ["clet_registrar"],
    "audit:export":                      ["nbec_member", "auditor"],
    "committee:manage":                  ["nbec_member", "nbec_secretariat"],
    "sla:view":                          ["nbec_member", "nbec_secretariat", "clet_registrar"],
    "reporting:view":                    ["nbec_member", "nbec_secretariat"],
    "rbac:manage":                       ["system_administrator"],
}


def seed_matrix(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    Permission = apps.get_model("users", "Permission")
    RolePermission = apps.get_model("users", "RolePermission")

    for codename, description in PERMISSIONS:
        Permission.objects.get_or_create(
            codename=codename,
            defaults={"description": description},
        )

    for name, description in ROLES:
        Role.objects.get_or_create(
            name=name,
            defaults={"description": description, "is_custom": False},
        )

    for codename, role_names in MATRIX.items():
        permission = Permission.objects.get(codename=codename)
        for role_name in role_names:
            role = Role.objects.get(name=role_name)
            RolePermission.objects.get_or_create(role=role, permission=permission)


def unseed_matrix(apps, schema_editor):
    apps.get_model("users", "RolePermission").objects.all().delete()
    apps.get_model("users", "Role").objects.all().delete()
    apps.get_model("users", "Permission").objects.all().delete()


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="UserProfile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("keycloak_sub", models.UUIDField(db_index=True, unique=True)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("role", models.CharField(blank=True, max_length=50)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "User Profile",
                "db_table": "users_userprofile",
            },
        ),
        migrations.CreateModel(
            name="Permission",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("codename", models.CharField(db_index=True, max_length=100, unique=True)),
                ("description", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "users_permission",
                "ordering": ["codename"],
            },
        ),
        migrations.CreateModel(
            name="Role",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(db_index=True, max_length=100, unique=True)),
                ("description", models.CharField(blank=True, max_length=255)),
                ("is_active", models.BooleanField(default=True)),
                ("is_custom", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "users_role",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="RolePermission",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("granted_by", models.UUIDField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("permission", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="grants", to="users.permission")),
                ("role", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="grants", to="users.role")),
            ],
            options={
                "db_table": "users_rolepermission",
                "ordering": ["role__name", "permission__codename"],
                "unique_together": {("role", "permission")},
            },
        ),
        migrations.RunPython(seed_matrix, reverse_code=unseed_matrix),
    ]
