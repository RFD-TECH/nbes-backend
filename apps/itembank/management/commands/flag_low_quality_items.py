"""Management command for flagging low-quality items.

Implements NBE-F02-09: any item whose last two recorded
``ItemUsage`` rows both have a ``discrimination_index`` below the
configured threshold is flagged for moderator review. The threshold
defaults to ``0.25`` and can be overridden via the
``DISCRIMINATION_THRESHOLD`` Django setting.

Run periodically (Celery Beat or cron):

    python manage.py flag_low_quality_items
"""

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.audit.models import AuditEvent
from apps.itembank.models import Item


class Command(BaseCommand):
    help = "Flag items that have underperformed across two consecutive sittings."

    def handle(self, *args, **options):
        raw_threshold = getattr(settings, "DISCRIMINATION_THRESHOLD", None)
        if raw_threshold in (None, ""):
            # SRS-NBE-F02-09 error state.
            raise CommandError(
                "Threshold Not Configured: Item quality thresholds are not "
                "configured; please contact the Administrator."
            )
        try:
            threshold = float(Decimal(str(raw_threshold)))
        except (InvalidOperation, ValueError) as exc:
            raise CommandError(
                f"DISCRIMINATION_THRESHOLD is not a valid number: {raw_threshold!r}"
            ) from exc

        candidate_items = Item.objects.filter(
            usage_history__discrimination_index__isnull=False
        ).distinct()

        flagged_count = 0
        for item in candidate_items:
            recent_usages = []
            seen_sitting_refs = set()
            for usage in item.usage_history.filter(
                discrimination_index__isnull=False
            ).order_by("-recorded_at"):
                if usage.sitting_ref in seen_sitting_refs:
                    continue
                recent_usages.append(usage)
                seen_sitting_refs.add(usage.sitting_ref)
                if len(recent_usages) == 2:
                    break
            if len(recent_usages) < 2:
                continue
            if all(
                float(usage.discrimination_index) < threshold for usage in recent_usages
            ):
                if not item.quality_flagged:
                    with transaction.atomic():
                        item.quality_flagged = True
                        item.save(update_fields=["quality_flagged"])
                        AuditEvent.record(
                            actor_id=None,
                            action="ITEM_QUALITY_FLAGGED",
                            entity_type="item",
                            entity_id=item.id,
                            new_state={
                                "discrimination_threshold": threshold,
                                "discrimination_indices": [
                                    str(u.discrimination_index) for u in recent_usages
                                ],
                            },
                        )
                    flagged_count += 1
                    self.stdout.write(f"Flagged item {item.id} for moderator review.")

        self.stdout.write(
            self.style.SUCCESS(
                f"Quality flagging complete. Items flagged this run: {flagged_count}."
            )
        )
