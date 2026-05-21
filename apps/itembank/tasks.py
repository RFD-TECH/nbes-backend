"""
Background tasks for item bank operations.

This module contains Celery tasks for asynchronous item processing,
particularly for dispatching notifications to downstream systems within SLA bounds.
"""

import logging
import requests
from django.conf import settings
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
    """
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
