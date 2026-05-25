import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("nbes")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "outbox-poller": {
        "task": "apps.audit.tasks.poll_outbox",
        "schedule": 5.0,
        "options": {"queue": "outbox"},
    },
    "sla-check": {
        "task": "apps.sla.tasks.check_all_slas",
        "schedule": 60.0 * 15,
        "options": {"queue": "sla-monitor"},
    },
    "vault-integrity-check": {
        "task": "apps.itembank.tasks.check_vault_integrity",
        "schedule": crontab(hour=3, minute=0),
        "options": {"queue": "vault-integrity"},
    },
    "cert-trigger-sla": {
        "task": "apps.cert_trigger.tasks.check_cert_trigger_sla",
        "schedule": 60.0 * 10,
        "options": {"queue": "sla-monitor"},
    },
    "cleanup-draft-registrations": {
        "task": "apps.registration.tasks.expire_stale_drafts",
        "schedule": crontab(hour=2, minute=0),
        "options": {"queue": "sla-monitor"},
    },
    "daily-hash-anchor": {
        "task": "apps.audit.tasks.daily_hash_anchor",
        "schedule": crontab(hour=1, minute=0),
        "options": {"queue": "outbox"},
    },
    "daily-security-summary": {
        "task": "apps.audit.tasks.daily_security_summary",
        "schedule": crontab(hour=6, minute=0),
        "options": {"queue": "sla-monitor"},
    },
    "cleanup-security-events": {
        "task": "apps.audit.tasks.cleanup_security_events",
        "schedule": crontab(hour=2, minute=30),
        "options": {"queue": "sla-monitor"},
    },
    "committee-tenure-monitor": {
        "task": "apps.committee.tasks.monitor_tenure_expiry",
        "schedule": crontab(hour=0, minute=30),
        "options": {"queue": "sla-monitor"},
    },
    "committee-overdue-actions": {
        "task": "apps.committee.tasks.escalate_overdue_actions",
        "schedule": crontab(hour=1, minute=30),
        "options": {"queue": "sla-monitor"},
    },
    # SRS §2.2.5 — daily integrity checksum vs System 05 archive copy.
    "committee-archive-integrity": {
        "task": "apps.committee.tasks.verify_archive_integrity",
        "schedule": crontab(hour=2, minute=15),
        "options": {"queue": "sla-monitor"},
    },
    # SRS §2.2.4 — annual COI refresh reminder.
    "committee-coi-refresh": {
        "task": "apps.committee.tasks.monitor_coi_refresh_due",
        "schedule": crontab(hour=4, minute=0),
        "options": {"queue": "sla-monitor"},
    },
}
