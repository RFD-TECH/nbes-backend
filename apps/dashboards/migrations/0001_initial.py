"""Initial DashboardPanel schema + seed of role-specific panels from
blueprint §1.2.9.

Roles covered:
    nbec_member, nbec_secretariat, item_writer, moderator, examiner,
    candidate, clet_registrar, system_administrator, auditor.

(Invigilator / centre coordinator is in the blueprint but is not yet a
modelled NBES role — added with an empty panel set if and when the role
is seeded.)
"""
import uuid

from django.db import migrations, models


# (role_codename, panel_key, panel_name, display_order)
PANELS = [
    # NBEC Member
    ("nbec_member", "nbec_member.meeting_agenda",        "Meeting agenda",        10),
    ("nbec_member", "nbec_member.pending_approvals",     "Pending approvals",     20),
    ("nbec_member", "nbec_member.conflict_declarations", "Conflict declarations", 30),
    ("nbec_member", "nbec_member.audit_trail_viewer",    "Audit-trail viewer",    40),
    # NBEC Secretariat
    ("nbec_secretariat", "nbec_secretariat.committee_ops",        "Committee operations",       10),
    ("nbec_secretariat", "nbec_secretariat.registration_desk",    "Candidate registration desk", 20),
    ("nbec_secretariat", "nbec_secretariat.exception_queue",      "Exception queue",             30),
    # Item Writer
    ("item_writer", "item_writer.my_items",         "My items",            10),
    ("item_writer", "item_writer.drafts",           "Drafts",              20),
    ("item_writer", "item_writer.review_feedback",  "Peer-review feedback", 30),
    # Moderator
    ("moderator", "moderator.review_queue",   "Review queue",  10),
    ("moderator", "moderator.panel_decisions", "Panel decisions", 20),
    ("moderator", "moderator.item_search",    "Item search",   30),
    # Examiner
    ("examiner", "examiner.marking_queue",     "Marking queue",          10),
    ("examiner", "examiner.borderline_review", "Borderline review queue", 20),
    # Candidate
    ("candidate", "candidate.registration", "Registration", 10),
    ("candidate", "candidate.payment",      "Payment",      20),
    ("candidate", "candidate.slip",         "Slip",         30),
    ("candidate", "candidate.results",      "Results",      40),
    ("candidate", "candidate.remarking",    "Remarking",    50),
    # CLET Registrar
    ("clet_registrar", "clet_registrar.override_queue",        "Override queue",         10),
    ("clet_registrar", "clet_registrar.ratification_dashboard", "Ratification dashboard", 20),
    ("clet_registrar", "clet_registrar.cert_trigger",           "Certificate trigger",    30),
    # System Administrator
    ("system_administrator", "admin.users",          "Users",         10),
    ("system_administrator", "admin.roles",          "Roles",         20),
    ("system_administrator", "admin.integrations",   "Integrations",  30),
    ("system_administrator", "admin.audit",          "Audit",         40),
    ("system_administrator", "admin.system_health",  "System health", 50),
    # Auditor
    ("auditor", "auditor.audit_search",  "Audit-trail search",     10),
    ("auditor", "auditor.chain_verify",  "Hash-chain verification", 20),
    ("auditor", "auditor.export",        "Export",                 30),
]


def seed_panels(apps, schema_editor):
    DashboardPanel = apps.get_model("dashboards", "DashboardPanel")
    for role, key, name, order in PANELS:
        DashboardPanel.objects.get_or_create(
            panel_key=key,
            defaults={
                "panel_name": name,
                "role_codename": role,
                "display_order": order,
                "is_active": True,
                "default_config": {},
            },
        )


def unseed_panels(apps, schema_editor):
    DashboardPanel = apps.get_model("dashboards", "DashboardPanel")
    DashboardPanel.objects.filter(
        panel_key__in=[key for _, key, _, _ in PANELS]
    ).delete()


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="DashboardPanel",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("panel_key", models.CharField(db_index=True, max_length=80, unique=True)),
                ("panel_name", models.CharField(max_length=120)),
                ("role_codename", models.CharField(db_index=True, max_length=80)),
                ("display_order", models.PositiveIntegerField(default=100)),
                ("is_active", models.BooleanField(default=True)),
                ("default_config", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Dashboard Panel",
                "db_table": "dashboards_panel",
                "ordering": ["role_codename", "display_order", "panel_name"],
            },
        ),
        migrations.RunPython(seed_panels, reverse_code=unseed_panels),
    ]
