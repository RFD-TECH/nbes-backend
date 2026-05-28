"""Migration 0003 — Seed DashboardPanel rows from the canonical blueprint.

Eliminates the hardcoded ``_DASHBOARD_PANELS`` dict in
``apps/users/views.py`` by seeding all existing role panels into the DB.

Adds missing System 10B role panels (remote_proctor, dti_operations,
service_desk_agent, director_general) that had no representation in either
dashboard implementation.

panel_key format: ``<role_codename>.<panel_id>``
role_codename matches ``users.Role.name`` (underscores, not hyphens).
"""
from django.db import migrations

# (panel_key, panel_name, role_codename, display_order)
PANELS = [
    # nbec_member
    ("nbec_member.meeting_agenda", "Meeting Agenda", "nbec_member", 10),
    ("nbec_member.pending_approvals", "Pending Approvals", "nbec_member", 20),
    ("nbec_member.conflict_declarations", "Conflict Declarations", "nbec_member", 30),
    ("nbec_member.audit_trail_viewer", "Audit Trail", "nbec_member", 40),
    # nbec_secretariat
    ("nbec_secretariat.committee_operations", "Committee Operations", "nbec_secretariat", 10),
    ("nbec_secretariat.candidate_registration_desk", "Candidate Registration Desk", "nbec_secretariat", 20),
    ("nbec_secretariat.exception_queue", "Exception Queue", "nbec_secretariat", 30),
    # item_writer
    ("item_writer.my_items", "My Items", "item_writer", 10),
    ("item_writer.drafts", "Drafts", "item_writer", 20),
    ("item_writer.peer_review_feedback", "Peer Review Feedback", "item_writer", 30),
    # moderator
    ("moderator.review_queue", "Review Queue", "moderator", 10),
    ("moderator.panel_decisions", "Panel Decisions", "moderator", 20),
    ("moderator.item_search", "Item Search", "moderator", 30),
    # examiner
    ("examiner.marking_queue", "Marking Queue", "examiner", 10),
    ("examiner.borderline_review_queue", "Borderline Review Queue", "examiner", 20),
    # candidate
    ("candidate.registration", "Registration", "candidate", 10),
    ("candidate.payment", "Payment", "candidate", 20),
    ("candidate.slip", "Admission Slip", "candidate", 30),
    ("candidate.results", "Results", "candidate", 40),
    ("candidate.remarking", "Remarking", "candidate", 50),
    # clet_registrar
    ("clet_registrar.override_queue", "Override Queue", "clet_registrar", 10),
    ("clet_registrar.ratification_dashboard", "Ratification Dashboard", "clet_registrar", 20),
    ("clet_registrar.cert_trigger_panel", "Certificate Trigger Panel", "clet_registrar", 30),
    # invigilator
    ("invigilator.centre_operations", "Centre Operations", "invigilator", 10),
    ("invigilator.candidate_checkin", "Candidate Check-In", "invigilator", 20),
    ("invigilator.proctoring_queue", "Proctoring Queue", "invigilator", 30),
    # centre_coordinator
    ("centre_coordinator.centre_operations", "Centre Operations", "centre_coordinator", 10),
    ("centre_coordinator.candidate_checkin", "Candidate Check-In", "centre_coordinator", 20),
    ("centre_coordinator.proctoring_queue", "Proctoring Queue", "centre_coordinator", 30),
    # system_administrator
    ("system_administrator.users", "Users", "system_administrator", 10),
    ("system_administrator.roles", "Roles", "system_administrator", 20),
    ("system_administrator.integrations", "Integrations", "system_administrator", 30),
    ("system_administrator.audit", "Audit", "system_administrator", 40),
    ("system_administrator.system_health", "System Health", "system_administrator", 50),
    # auditor
    ("auditor.audit_trail_search", "Audit Trail Search", "auditor", 10),
    ("auditor.hash_chain_verifier", "Hash-Chain Verifier", "auditor", 20),
    ("auditor.export", "Export", "auditor", 30),
    # remote_proctor
    ("remote_proctor.live_session_monitor", "Live Session Monitor", "remote_proctor", 10),
    ("remote_proctor.flag_review_queue", "Flag Review Queue", "remote_proctor", 20),
    ("remote_proctor.candidate_checkin", "Candidate Check-In", "remote_proctor", 30),
    # dti_operations
    ("dti_operations.system_health", "System Health", "dti_operations", 10),
    ("dti_operations.integration_status", "Integration Status", "dti_operations", 20),
    ("dti_operations.deployment_logs", "Deployment Logs", "dti_operations", 30),
    # service_desk_agent
    ("service_desk_agent.support_queue", "Support Queue", "service_desk_agent", 10),
    ("service_desk_agent.candidate_lookup", "Candidate Lookup", "service_desk_agent", 20),
    ("service_desk_agent.escalation_log", "Escalation Log", "service_desk_agent", 30),
    # director_general
    ("director_general.executive_summary", "Executive Summary", "director_general", 10),
    ("director_general.examination_overview", "Examination Overview", "director_general", 20),
    ("director_general.compliance_status", "Compliance Status", "director_general", 30),
    ("director_general.audit_highlights", "Audit Highlights", "director_general", 40),
]


def seed_panels(apps, schema_editor):
    DashboardPanel = apps.get_model("dashboards", "DashboardPanel")
    for panel_key, panel_name, role_codename, display_order in PANELS:
        DashboardPanel.objects.update_or_create(
            panel_key=panel_key,
            defaults={
                "panel_name": panel_name,
                "role_codename": role_codename,
                "display_order": display_order,
                "is_active": True,
                "default_config": {},
            },
        )


def unseed_panels(apps, schema_editor):
    DashboardPanel = apps.get_model("dashboards", "DashboardPanel")
    DashboardPanel.objects.filter(
        panel_key__in=[p[0] for p in PANELS]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboards", "0002_alter_dashboardpanel_default_config_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_panels, reverse_code=unseed_panels),
    ]
