from datetime import datetime, timezone as py_timezone

from django.test import TestCase

from apps.audit.models import AuditEvent, DailyHashAnchor
from apps.audit.tasks import daily_hash_anchor


class DailyHashAnchorTests(TestCase):
    def test_empty_day_carries_forward_previous_chain_head(self):
        AuditEvent.objects.create(
            action="PREVIOUS_DAY_EVENT",
            chain_hash="a" * 64,
            created_at=datetime(2026, 5, 18, 12, tzinfo=py_timezone.utc),
        )

        result = daily_hash_anchor.run("2026-05-19")

        anchor = DailyHashAnchor.objects.get(date="2026-05-19")
        self.assertEqual(anchor.event_count, 0)
        self.assertIsNone(anchor.head_event_id)
        self.assertEqual(anchor.head_hash, "a" * 64)
        self.assertEqual(result["head_hash"], "a" * 64)

    def test_empty_chain_still_uses_genesis_hash(self):
        daily_hash_anchor.run("2026-05-19")

        anchor = DailyHashAnchor.objects.get(date="2026-05-19")
        self.assertEqual(anchor.event_count, 0)
        self.assertEqual(anchor.head_hash, "0" * 64)
