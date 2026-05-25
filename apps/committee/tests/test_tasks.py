"""apps/committee/tests/test_tasks.py — monitor_tenure_expiry and escalate_overdue_actions.

NBES does NOT call Keycloak/IAM directly on tenure expiry. Identity revocation
is IAM's responsibility, triggered by the ``MemberExpired`` event NBES publishes.
These tests assert the local DB transition, the audit entry, and the event.
"""
import datetime
import uuid
from unittest.mock import patch

import pytest

from apps.committee.models import ActionItem, Meeting, NBECMember
from apps.committee.tasks import monitor_tenure_expiry, escalate_overdue_actions


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def active_member(db):
    return NBECMember.objects.create(
        keycloak_sub=uuid.uuid4(),
        full_name="Tenure Test",
        contact="tenure@example.com",
        designation=NBECMember.Designation.MEMBER,
        status=NBECMember.Status.ACTIVE,
        tenure_start=datetime.date(2024, 1, 1),
        tenure_end=datetime.date(2024, 12, 31),  # already past
    )


@pytest.fixture
def active_chair(db):
    return NBECMember.objects.create(
        keycloak_sub=uuid.uuid4(),
        full_name="Chair Test",
        contact="chair@example.com",
        designation=NBECMember.Designation.CHAIR,
        status=NBECMember.Status.ACTIVE,
        tenure_start=datetime.date(2024, 1, 1),
        tenure_end=datetime.date(2024, 12, 31),
    )


@pytest.fixture
def meeting_with_overdue_action(db):
    meeting = Meeting.objects.create(
        reference="MTG-TASK-001",
        meeting_type=Meeting.MeetingType.ORDINARY,
        scheduled_date="2026-01-01T10:00:00Z",
        quorum_required=2,
    )
    item = ActionItem.objects.create(
        meeting=meeting,
        description="Should have been done",
        assigned_to_id=uuid.uuid4(),
        due_date=datetime.date(2024, 1, 1),
        status=ActionItem.Status.OPEN,
    )
    return meeting, item


# ── monitor_tenure_expiry ─────────────────────────────────────────────────────

@pytest.mark.django_db
class TestMonitorTenureExpiry:

    def test_expires_member_locally(self, active_member):
        result = monitor_tenure_expiry()

        active_member.refresh_from_db()
        assert active_member.status == NBECMember.Status.EXPIRED
        assert active_member.is_active is False
        assert result["expired"] == 1

    @patch("apps.committee.tasks.publish")
    def test_publishes_member_expired_event_for_iam(self, mock_publish, active_member):
        monitor_tenure_expiry()

        mock_publish.assert_called_once()
        event_name, payload = mock_publish.call_args[0]
        assert event_name == "MemberExpired"
        assert payload["keycloak_sub"] == str(active_member.keycloak_sub)
        assert payload["member_id"] == str(active_member.id)
        assert payload["designation"] == NBECMember.Designation.MEMBER

    def test_expires_chair(self, active_chair):
        monitor_tenure_expiry()

        active_chair.refresh_from_db()
        assert active_chair.status == NBECMember.Status.EXPIRED

    def test_skips_members_not_yet_expired(self, db):
        NBECMember.objects.create(
            keycloak_sub=uuid.uuid4(),
            full_name="Future Expiry",
            contact="future@example.com",
            designation=NBECMember.Designation.MEMBER,
            status=NBECMember.Status.ACTIVE,
            tenure_start=datetime.date(2026, 1, 1),
            tenure_end=datetime.date(2099, 12, 31),
        )
        result = monitor_tenure_expiry()

        assert result["expired"] == 0

    def test_skips_already_expired_members(self, db):
        NBECMember.objects.create(
            keycloak_sub=uuid.uuid4(),
            full_name="Already Gone",
            contact="gone@example.com",
            designation=NBECMember.Designation.MEMBER,
            status=NBECMember.Status.EXPIRED,
            tenure_start=datetime.date(2024, 1, 1),
            tenure_end=datetime.date(2024, 6, 1),
        )
        result = monitor_tenure_expiry()

        assert result["expired"] == 0

    def test_audit_event_recorded(self, active_member):
        from apps.audit.models import AuditEvent

        before = AuditEvent.objects.count()
        monitor_tenure_expiry()
        assert AuditEvent.objects.count() > before


# ── escalate_overdue_actions ──────────────────────────────────────────────────

@pytest.mark.django_db
class TestEscalateOverdueActions:

    def test_marks_open_item_overdue(self, meeting_with_overdue_action):
        _, item = meeting_with_overdue_action
        result = escalate_overdue_actions()

        item.refresh_from_db()
        assert item.status == ActionItem.Status.OVERDUE
        assert item.last_escalated_at is not None
        assert result["escalated"] == 1

    def test_marks_in_progress_item_overdue(self, db):
        meeting = Meeting.objects.create(
            reference="MTG-TASK-002",
            meeting_type=Meeting.MeetingType.ORDINARY,
            scheduled_date="2026-01-01T10:00:00Z",
            quorum_required=2,
        )
        item = ActionItem.objects.create(
            meeting=meeting,
            description="Started but not finished",
            assigned_to_id=uuid.uuid4(),
            due_date=datetime.date(2024, 1, 1),
            status=ActionItem.Status.IN_PROGRESS,
        )
        escalate_overdue_actions()

        item.refresh_from_db()
        assert item.status == ActionItem.Status.OVERDUE

    def test_skips_completed_items(self, db):
        meeting = Meeting.objects.create(
            reference="MTG-TASK-003",
            meeting_type=Meeting.MeetingType.ORDINARY,
            scheduled_date="2026-01-01T10:00:00Z",
            quorum_required=2,
        )
        ActionItem.objects.create(
            meeting=meeting,
            description="Already done",
            assigned_to_id=uuid.uuid4(),
            due_date=datetime.date(2024, 1, 1),
            status=ActionItem.Status.COMPLETE,
        )
        result = escalate_overdue_actions()
        assert result["escalated"] == 0

    def test_audit_event_recorded(self, meeting_with_overdue_action):
        from apps.audit.models import AuditEvent

        before = AuditEvent.objects.count()
        escalate_overdue_actions()
        assert AuditEvent.objects.count() > before
