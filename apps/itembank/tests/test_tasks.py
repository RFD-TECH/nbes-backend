from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.audit.models import AuditEvent
from apps.itembank.models import ItemUsage
from apps.itembank.tests.test_services import _make_item, _make_user


class FlagLowQualityItemsCommandTests(TestCase):
    @override_settings(DISCRIMINATION_THRESHOLD=None)
    def test_threshold_must_be_configured(self):
        with self.assertRaisesMessage(CommandError, "Threshold Not Configured"):
            call_command("flag_low_quality_items", stdout=StringIO())

    @override_settings(DISCRIMINATION_THRESHOLD="0.25")
    def test_flags_item_and_records_single_audit_event(self):
        author = _make_user()
        item = _make_item(author=author)
        ItemUsage.objects.create(
            item_id=item,
            sitting_ref="BAR-2026-01",
            discrimination_index=Decimal("0.1000"),
        )
        ItemUsage.objects.create(
            item_id=item,
            sitting_ref="BAR-2026-02",
            discrimination_index=Decimal("0.2000"),
        )

        call_command("flag_low_quality_items", stdout=StringIO())
        call_command("flag_low_quality_items", stdout=StringIO())

        item.refresh_from_db()
        self.assertTrue(item.quality_flagged)
        self.assertEqual(
            AuditEvent.objects.filter(action="ITEM_QUALITY_FLAGGED").count(),
            1,
        )
