from datetime import datetime, timezone as py_timezone
from unittest.mock import patch

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


class PartitionTaskTests(TestCase):
    @patch("django.db.connection.vendor", "sqlite")
    def test_precreate_audit_partitions_sqlite_noop(self):
        from apps.audit.tasks import precreate_audit_partitions

        result = precreate_audit_partitions.run()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not postgresql")

    @patch("django.db.connection.vendor", "postgresql")
    @patch("django.db.connection.cursor")
    def test_precreate_audit_partitions_postgresql_runs_sql(self, mock_cursor):
        from unittest.mock import patch
        from apps.audit.tasks import precreate_audit_partitions
        from datetime import datetime, timezone as py_timezone

        current_year = datetime.now(py_timezone.utc).year
        next_year = current_year + 1

        result = precreate_audit_partitions.run()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["partition"], f"audit_auditevent_y{next_year}")
        mock_cursor.assert_called_once()
        mock_cursor.return_value.__enter__.return_value.execute.assert_called_once()
