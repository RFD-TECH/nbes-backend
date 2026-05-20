"""Smoke-test the System 17 integration substrate.

    python manage.py nbes_system17_smoke

Posts a benign signed payload to ``SYSTEM_17_URL/v1/health`` (override via
``--endpoint``) and prints the normalised response. Use this after every
deploy to confirm the HMAC secret, mTLS chain, and clock skew are within
spec — *before* the outbox starts publishing real events.
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from shared.integrations import call_system_17


class Command(BaseCommand):
    help = "Send a signed smoke request to System 17 and report the response."

    def add_arguments(self, parser):
        parser.add_argument(
            "--endpoint",
            default="/v1/health",
            help="Path to call (default /v1/health).",
        )
        parser.add_argument(
            "--method",
            default="POST",
            help="HTTP verb (default POST).",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "SYSTEM_17_URL", ""):
            raise CommandError("SYSTEM_17_URL is not configured.")
        if not getattr(settings, "SYSTEM_17_HMAC_SECRET", ""):
            raise CommandError("SYSTEM_17_HMAC_SECRET is not configured.")

        idempotency_key = f"smoke-{secrets.token_hex(8)}"
        response = call_system_17(
            endpoint=options["endpoint"],
            payload={"smoke": True},
            idempotency_key=idempotency_key,
            method=options["method"],
        )

        if response.ok:
            self.stdout.write(self.style.SUCCESS(
                f"System 17 OK ({response.status_code}). "
                f"correlation_id={response.correlation_id}"
            ))
            self.stdout.write(f"data: {response.data}")
            return

        self.stdout.write(self.style.ERROR(
            f"System 17 FAIL status={response.status_code} "
            f"code={response.code} retryable={response.retryable}"
        ))
        self.stdout.write(f"message: {response.message}")
        raise CommandError("System 17 smoke failed.")
