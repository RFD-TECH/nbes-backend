import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("nbes")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# ── Celery Beat Periodic Schedule ─────────────────────────────────────────────
app.conf.beat_schedule = {
    # Outbox poller — publishes OutboxEvents to Kafka every 5 seconds
    "outbox-poller": {
        "task": "apps.audit.tasks.poll_outbox",
        "schedule": 5.0,
        "options": {"queue": "outbox"},
    },
    # SLA monitor — checks all active SLA instances every 15 minutes
    "sla-check": {
        "task": "apps.sla.tasks.check_all_slas",
        "schedule": 60.0 * 15,
        "options": {"queue": "sla-monitor"},
    },
    # Vault integrity — daily SHA-256 check over all vault items at 03:00 Ghana time
    "vault-integrity-check": {
        "task": "apps.itembank.tasks.check_vault_integrity",
        "schedule": crontab(hour=3, minute=0),
        "options": {"queue": "vault-integrity"},
    },
    # Cert trigger SLA — checks 1-hour cert trigger SLA every 10 minutes
    "cert-trigger-sla": {
        "task": "apps.cert_trigger.tasks.check_cert_trigger_sla",
        "schedule": 60.0 * 10,
        "options": {"queue": "sla-monitor"},
    },
    # Expire draft registrations beyond inactivity window — daily at 02:00
    "cleanup-draft-registrations": {
        "task": "apps.registration.tasks.expire_stale_drafts",
        "schedule": crontab(hour=2, minute=0),
        "options": {"queue": "sla-monitor"},
    },
    # Daily audit-chain anchor — closes the previous UTC day at 01:00 UTC
    # and emits AuditChainAnchorReady to the outbox for System 22.
    # Blueprint §1.2.7 / acceptance F000-07.
    "daily-hash-anchor": {
        "task": "apps.audit.tasks.daily_hash_anchor",
        "schedule": crontab(hour=1, minute=0),
        "options": {"queue": "outbox"},
    },
    # Security Operations daily summary — 06:00 UTC. Feeds the SecOps
    # Console daily-summary endpoint and the notification bridge.
    "daily-security-summary": {
        "task": "apps.audit.tasks.daily_security_summary",
        "schedule": crontab(hour=6, minute=0),
        "options": {"queue": "sla-monitor"},
    },
    # Prune SecurityEvent rows past the hot retention window. Daily, 02:30 UTC.
    "cleanup-security-events": {
        "task": "apps.audit.tasks.cleanup_security_events",
        "schedule": crontab(hour=2, minute=30),
        "options": {"queue": "sla-monitor"},
    },
}
