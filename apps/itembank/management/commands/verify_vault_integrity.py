"""Management command to verify vault replication integrity.

This module provides a Django management command that computes a
cryptographic checksum over approved Item records in the local
vault and compares that checksum to a remote/replica checksum to
detect cross-region replication drift.
"""

import hashlib
import os
from urllib.parse import urlparse

import requests
from django.core.management.base import BaseCommand, CommandError

from apps.itembank.models import Item


class Command(BaseCommand):
    """Django management command to sweep vault state and detect drift.

    The command computes a deterministic SHA-256 fingerprint of the
    current set of approved items by concatenating each item's id
    and audit_hash in id order. It then compares that fingerprint to
    a replica/remote fingerprint to detect replication inconsistencies.
    """

    help = (
        "Runs daily checksum sweep over vault instances and checks "
        "cross-region replication health."
    )

    def handle(self, *args, **options):
        """Execute the integrity verification sweep.

        Args:
            *args: Positional arguments passed by Django; not used.
            **options: Command options; not used.

        The function computes a local checksum and compares it to a
        remote checksum. If they differ, a critical alarm is written
        to stdout and (optionally) an on-call pager workflow may be
        triggered.
        """

        # Mark args/options as used to satisfy linters.
        _ = args
        _ = options

        # Compute local snapshot state fingerprint from approved items.
        vault_items = Item.objects.filter(status=Item.Status.LOCKED_FOR_USE).order_by(
            "id"
        )
        hasher = hashlib.sha256()

        for item in vault_items:
            # include both id and audit hash to reflect item content and
            # ordering deterministically in the fingerprint input
            hasher.update(f"{item.id}:{item.audit_hash}".encode("utf-8"))

        local_checksum = hasher.hexdigest()

        remote_checksum = self.fetch_secondary_region_vault_checksum()

        if local_checksum != remote_checksum:
            # Critical mismatch: report and (optionally) escalate.
            self.stdout.write(
                self.style.ERROR(
                    "CRITICAL ALARM: Cryptographic drift detected between "
                    "regional vaults!"
                )
            )
            # trigger_on_call_pager_infrastructure(
            #     severity="CRITICAL", component="VaultReplication"
            # )
            raise CommandError(
                "CRITICAL ALARM: Cryptographic drift detected between regional vaults!"
            )
        else:
            self.stdout.write("Vault replication sweep complete. 0 variances detected.")

    def fetch_secondary_region_vault_checksum(self):
        """Retrieve the remote vault checksum with retries and a timeout."""

        checksum_url = os.environ.get("VAULT_SECONDARY_REGION_CHECKSUM_URL")
        if not checksum_url:
            raise CommandError(
                "VAULT_SECONDARY_REGION_CHECKSUM_URL is not configured; unable to verify remote vault checksum."
            )
        parsed = urlparse(checksum_url)
        if parsed.scheme.lower() != "https":
            raise CommandError("VAULT_SECONDARY_REGION_CHECKSUM_URL must use HTTPS.")
        if not parsed.netloc:
            raise CommandError(
                "VAULT_SECONDARY_REGION_CHECKSUM_URL is invalid; host is missing."
            )

        last_error = None
        for attempt in range(1, 4):
            try:
                response = requests.get(checksum_url, timeout=5)
                response.raise_for_status()

                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text.strip()

                if isinstance(payload, dict):
                    remote_checksum = payload.get("checksum")
                else:
                    remote_checksum = payload

                remote_checksum = str(remote_checksum or "").strip()
                if not remote_checksum:
                    raise CommandError(
                        "Remote vault checksum response did not include a checksum value."
                    )

                return remote_checksum
            except (requests.RequestException, CommandError) as exc:
                last_error = exc
                if attempt == 3:
                    break

        raise CommandError(
            f"Remote vault checksum retrieval failed after 3 attempts: {last_error}"
        )
