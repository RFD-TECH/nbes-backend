from datetime import datetime, timezone as py_timezone
from unittest.mock import patch

from django.test import TestCase

from apps.audit.models import AuditEvent
from apps.audit.views import _audit_export_query


class AuditExportQueryTests(TestCase):
    def test_unbounded_export_defaults_to_yesterday_utc(self):
        start = datetime(2026, 5, 19, 0, 0, tzinfo=py_timezone.utc)
        end = datetime(2026, 5, 19, 23, 59, 59, 999999, tzinfo=py_timezone.utc)

        yesterday = AuditEvent.objects.create(
            action="IN_DEFAULT_WINDOW",
            chain_hash="1" * 64,
            created_at=datetime(2026, 5, 19, 12, tzinfo=py_timezone.utc),
        )
        AuditEvent.objects.create(
            action="OUTSIDE_DEFAULT_WINDOW",
            chain_hash="2" * 64,
            created_at=datetime(2026, 5, 18, 12, tzinfo=py_timezone.utc),
        )

        with patch("apps.audit.views._yesterday_utc_bounds", return_value=(start, end)):
            query = _audit_export_query({})

        self.assertEqual(list(AuditEvent.objects.filter(query)), [yesterday])

    def test_explicit_export_window_is_not_overridden(self):
        target = AuditEvent.objects.create(
            action="EXPLICIT_WINDOW",
            chain_hash="1" * 64,
            created_at=datetime(2026, 5, 18, 12, tzinfo=py_timezone.utc),
        )

        query = _audit_export_query({"from": "2026-05-18", "to": "2026-05-18"})

        self.assertEqual(list(AuditEvent.objects.filter(query)), [target])
