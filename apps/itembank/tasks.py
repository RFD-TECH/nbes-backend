"""
Background tasks for item bank operations.

This module contains Celery tasks for asynchronous item processing,
particularly for dispatching notifications to downstream systems within SLA bounds.
"""

import logging
import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from celery import shared_task

logger = logging.getLogger(__name__)


# Retry up to 3 times, wait 60 seconds between tries.
# This guarantees delivery within the 5-minute SLA even if the network blips.
@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(
        requests.exceptions.RequestException,
    ),  # Automatically catch timeouts/500s
)
def dispatch_item_status_notification(self, item_id, author_id, new_status, rationales=None):
    """
    Background worker task to dispatch SLA-bound notifications to System 14.

    ``SYSTEM_14_SERVICE_TOKEN`` is validated here (call-site) rather than at
    settings-import time, so ``collectstatic`` / ``migrate`` / unit tests
    don't require the production secret to be present.
    """
    if not settings.SYSTEM_14_SERVICE_TOKEN:
        raise ImproperlyConfigured(
            "SYSTEM_14_SERVICE_TOKEN must be set before dispatching notifications "
            "to System 14."
        )

    logger.info(
        "Preparing %s notification for Author %s (Item %s)",
        new_status,
        author_id,
        item_id,
    )

    # Construct a standardized JSON payload that System 14 expects
    payload = {
        "recipient_id": str(author_id),
        "notification_type": "ITEM_WORKFLOW_EVENT",
        "urgency": "HIGH",  # Ensures System 14 prioritizes this for the 5-min SLA
        "template_data": {
            "item_id": str(item_id),
            "new_status": new_status,
            "consolidated_feedback": rationales if rationales else [],
        },
    }

    idempotency_key = getattr(self.request, "id", None) or f"{item_id}:{author_id}:{new_status}"

    # Configure the secure connection to System 14
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.SYSTEM_14_SERVICE_TOKEN}",
        "Idempotency-Key": idempotency_key,
    }

    endpoint = f"{settings.SYSTEM_14_BASE_URL}/api/v1/dispatch/"

    # Fire the request
    try:
        response = requests.post(endpoint, json=payload, headers=headers, timeout=5.0)

        # If System 14 returns a 500 Server Error, this raises an exception
        # which triggers the Celery autoretry_for logic!
        response.raise_for_status()

        logger.info(
            "Successfully handed off notification to System 14. Ref: %s",
            response.json().get("dispatch_id"),
        )
        return True

    except requests.exceptions.RequestException as e:
        logger.error("System 14 integration failed: %s. Celery will retry.", str(e))
        raise  # Pass the error up so Celery knows it needs to retry


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(requests.exceptions.RequestException,),
)
def dispatch_vault_export_alert(self, request_id: str, requester_sub: str, scope: str):
    """Immediately alert the NBEC Chair and DG when a vault export is initiated.

    SRS-NBE-F02-07 / NBE-N01: "Privileged actions trigger immediate alerts
    to the Chair and DG on any export operation."

    Recipients:
      - Active NBEC Chair  — resolved from the committee app by designation.
      - DG                 — configured via settings.NBEC_DG_KEYCLOAK_SUB.
    """
    from apps.committee.models import NBECMember

    recipients = []
    try:
        chair = NBECMember.objects.filter(
            designation=NBECMember.Designation.CHAIR, is_active=True
        ).first()
        if chair:
            recipients.append(str(chair.keycloak_sub))
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not resolve NBEC Chair for vault alert: %s", exc)

    dg_sub = getattr(settings, "NBEC_DG_KEYCLOAK_SUB", None)
    if dg_sub and dg_sub not in recipients:
        recipients.append(str(dg_sub))

    if not recipients:
        logger.warning("No vault alert recipients configured; skipping alert for request %s", request_id)
        return False

    idempotency_key = getattr(self.request, "id", None) or f"vault-alert:{request_id}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.SYSTEM_14_SERVICE_TOKEN}",
        "Idempotency-Key": str(idempotency_key),
    }
    endpoint = f"{settings.SYSTEM_14_BASE_URL}/api/v1/dispatch/"

    all_ok = True
    for recipient_sub in recipients:
        payload = {
            "recipient_id": recipient_sub,
            "notification_type": "VAULT_EXPORT_INITIATED",
            "urgency": "CRITICAL",
            "template_data": {
                "request_id": request_id,
                "requester_id": requester_sub,
                "scope": scope,
            },
        }
        try:
            response = requests.post(endpoint, json=payload, headers=headers, timeout=5.0)
            response.raise_for_status()
            logger.info("Vault export alert sent to %s. Ref: %s", recipient_sub, response.json().get("dispatch_id"))
        except requests.exceptions.RequestException as exc:
            logger.error("Vault export alert failed for %s: %s. Celery will retry.", recipient_sub, exc)
            all_ok = False
            raise  # triggers autoretry

    return all_ok


@shared_task(name="apps.itembank.tasks.flag_low_quality_items_task")
def flag_low_quality_items_task():
    """Celery wrapper around the ``flag_low_quality_items`` mgmt command.

    Scheduled by Celery Beat per SRS-NBE-F02-09 so the monthly quality
    job runs without operator intervention.
    """
    from django.core.management import call_command

    call_command("flag_low_quality_items")
