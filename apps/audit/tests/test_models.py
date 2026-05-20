"""apps/audit/tests/test_models.py — AuditEvent chain hash and DailyHashAnchor."""
import uuid
import pytest
from apps.audit.models import AuditEvent, DailyHashAnchor, OutboxEvent


@pytest.mark.django_db
class TestAuditEventRecord:
    def test_creates_event(self):
        evt = AuditEvent.record(
            actor_id=uuid.uuid4(),
            action="TEST_ACTION",
            entity_type="test",
            entity_id=uuid.uuid4(),
        )
        assert evt.pk is not None
        assert evt.chain_hash != ""
        assert len(evt.chain_hash) == 64

    def test_chain_hash_links_events(self):
        e1 = AuditEvent.record(action="FIRST", entity_type="test")
        e2 = AuditEvent.record(action="SECOND", entity_type="test")
        assert e1.chain_hash != e2.chain_hash
        assert e2.chain_hash != "0" * 64

    def test_first_event_has_valid_hash(self):
        # With empty table the genesis hash is 64 zeros (prev), then SHA-256 of that + payload
        AuditEvent.objects.all().delete()
        evt = AuditEvent.record(action="GENESIS", entity_type="test")
        assert len(evt.chain_hash) == 64

    def test_outbox_event_created(self):
        before = OutboxEvent.objects.count()
        AuditEvent.record(action="OUTBOX_TEST", entity_type="test")
        assert OutboxEvent.objects.count() > before

    def test_event_id_is_uuid(self):
        evt = AuditEvent.record(action="UUID_TEST", entity_type="test")
        assert evt.event_id is not None

    def test_str(self):
        evt = AuditEvent.record(action="STR_TEST", entity_type="thing")
        assert "STR_TEST" in str(evt)


@pytest.mark.django_db
class TestDailyHashAnchor:
    def test_create(self):
        import datetime
        anchor = DailyHashAnchor.objects.create(
            date=datetime.date(2026, 5, 19),
            head_hash="a" * 64,
            event_count=10,
        )
        assert anchor.exported_to_s22_at is None
        assert "2026-05-19" in str(anchor)

    def test_unique_per_date(self):
        import datetime
        from django.db import IntegrityError
        DailyHashAnchor.objects.create(
            date=datetime.date(2026, 5, 18),
            head_hash="b" * 64,
        )
        with pytest.raises(IntegrityError):
            DailyHashAnchor.objects.create(
                date=datetime.date(2026, 5, 18),
                head_hash="c" * 64,
            )
