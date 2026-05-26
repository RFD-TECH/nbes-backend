"""Migration 0011 — Full permission codename catalog.

Seeds the complete 25+ codename set required by SRS §1.2.2 and §2.3.
Existing codenames are left untouched (idempotent get_or_create).

Permission codename catalog is incomplete (only ~12 exist).
Proctor/Invigilator codenames not seeded.

Previously seeded (safe to ignore):
  rbac:manage, audit:search, audit:verify, audit:export,
  secops:view, dashboards:manage, paper:construct, item:search,
  search:manage, vault:operate
"""

from django.db import migrations

# New codenames to add. Format: (codename, description)
NEW_PERMISSIONS = [
    # User management
    ("users:manage", "Provision and manage NBES user profiles"),
    ("users:import", "Bulk-import users from CSV/XLSX via IAM"),
    # Item bank
    ("item:write", "Create and edit examination items"),
    ("item:approve", "Approve items for paper construction"),
    # Results
    ("results:publish", "Publish and release examination results"),
    ("results:view", "View own examination results (candidate self-service)"),
    # Candidate
    ("candidate:register", "Register as a candidate for an examination"),
    ("candidate:verify_identity", "Verify candidate identity at check-in"),
    # Examination centres
    ("centre:manage", "Manage examination centre configurations"),
    ("centre:invigilate", "Operate as an invigilator at a centre"),
    ("centre:checkin", "Process candidate check-in at a centre"),
    # Proctoring 
    ("proctoring:remote", "Conduct remote proctoring sessions"),
    ("proctoring:review_flags", "Review AI-flagged proctoring events"),
    # Support & operations
    ("helpdesk:support", "Access the service desk console"),
    ("dti:operate", "DTI Operations console access"),
    ("integrations:manage", "Manage integration settings and credentials"),
    # Director-General
    ("dg:overview", "Access the Director-General overview dashboard"),
    # Marking
    ("marking:score", "Score candidate scripts"),
    ("marking:moderate", "Moderate scored scripts"),
    # Committee / NBEC
    ("committee:approve", "Approve committee resolutions"),
    ("committee:chair", "Chair NBEC committee sessions"),
]

# Map codename → list of role names that receive the grant.
GRANTS: dict[str, list[str]] = {
    "users:manage": ["system_administrator"],
    "users:import": ["system_administrator"],
    "item:write": ["item_writer"],
    "item:approve": ["moderator"],
    "results:publish": ["clet_registrar", "nbec_secretariat"],
    "results:view": ["candidate"],
    "candidate:register": ["candidate"],
    "candidate:verify_identity": ["invigilator", "remote_proctor"],
    "centre:manage": ["centre_coordinator", "system_administrator"],
    "centre:invigilate": ["invigilator"],
    "centre:checkin": ["invigilator", "centre_coordinator"],
    "proctoring:remote": ["remote_proctor"],
    "proctoring:review_flags": ["remote_proctor", "system_administrator"],
    "helpdesk:support": ["service_desk_agent"],
    "dti:operate": ["dti_operations"],
    "integrations:manage": ["system_administrator"],
    "dg:overview": ["director_general"],
    "marking:score": ["examiner"],
    "marking:moderate": ["moderator", "examiner"],
    "committee:approve": ["nbec_member"],
    "committee:chair": ["nbec_member"],
}


def add_codenames(apps, _schema_editor):
    Role = apps.get_model("users", "Role")
    Permission = apps.get_model("users", "Permission")
    RolePermission = apps.get_model("users", "RolePermission")

    missing_roles: set[str] = set()

    # Seed permissions
    for codename, description in NEW_PERMISSIONS:
        Permission.objects.get_or_create(
            codename=codename,
            defaults={"description": description},
        )

    # Wire grants
    for codename, role_names in GRANTS.items():
        try:
            permission = Permission.objects.get(codename=codename)
        except Permission.DoesNotExist:
            continue  # should not happen since we just created them above
        for role_name in role_names:
            try:
                role = Role.objects.get(name=role_name)
            except Role.DoesNotExist:
                missing_roles.add(role_name)
                continue
            RolePermission.objects.get_or_create(role=role, permission=permission)

    if missing_roles:
        raise RuntimeError(
            f"Permission catalog migration incomplete — roles not found: {sorted(missing_roles)}"
        )


def drop_codenames(apps, _schema_editor):
    Permission = apps.get_model("users", "Permission")
    RolePermission = apps.get_model("users", "RolePermission")
    Role = apps.get_model("users", "Role")

    # Remove only the grants this migration wired so we don't disturb
    # any grants that existed before this migration ran.
    for codename, role_names in GRANTS.items():
        try:
            perm = Permission.objects.get(codename=codename)
        except Permission.DoesNotExist:
            continue
        for role_name in role_names:
            try:
                role = Role.objects.get(name=role_name)
            except Role.DoesNotExist:
                continue
            RolePermission.objects.filter(role=role, permission=perm).delete()
    # Delete only permissions that have no remaining grants. A pre-existing
    # permission would retain grants we didn't create and must be left alone.
    for codename, _ in NEW_PERMISSIONS:
        try:
            perm = Permission.objects.get(codename=codename)
            if not RolePermission.objects.filter(permission=perm).exists():
                perm.delete()
        except Permission.DoesNotExist:
            pass


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0010_role_mutual_exclusion_and_approvals"),
    ]

    operations = [
        migrations.RunPython(add_codenames, reverse_code=drop_codenames),
    ]
