"""apps/committee/tests/test_tasks.py — monitor_tenure_expiry and escalate_overdue_actions."""
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
        email="tenure@example.com",
        role=NBECMember.Role.MEMBER,
        status=NBECMember.Status.ACTIVE,
        appointment_date=datetime.date(2024, 1, 1),
        tenure_end_date=datetime.date(2024, 12, 31),  # already past
    )


@pytest.fixture
def active_chair(db):
    return NBECMember.objects.create(
        keycloak_sub=uuid.uuid4(),
        full_name="Chair Test",
        email="chair@example.com",
        role=NBECMember.Role.CHAIR,
        status=NBECMember.Status.ACTIVE,
        appointment_date=datetime.date(2024, 1, 1),
        tenure_end_date=datetime.date(2024, 12, 31),
    )


@pytest.fixture
def active_secretary(db):
    return NBECMember.objects.create(
        keycloak_sub=uuid.uuid4(),
        full_name="Secretary Test",
        email="secretary@example.com",
        role=NBECMember.Role.SECRETARY,
        status=NBECMember.Status.ACTIVE,
        appointment_date=datetime.date(2024, 1, 1),
        tenure_end_date=datetime.date(2024, 12, 31),
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

    @patch("shared.keycloak_admin.revoke_realm_role")
    def test_expires_member_and_revokes_keycloak(self, mock_revoke, active_member):
        result = monitor_tenure_expiry()

        active_member.refresh_from_db()
        assert active_member.status == NBECMember.Status.EXPIRED
        assert active_member.is_active is False
        assert result["expired"] == 1
        mock_revoke.assert_called_once_with(str(active_member.keycloak_sub), "nbec_member")

    @patch("shared.keycloak_admin.revoke_realm_role")
    def test_secretary_role_revokes_nbec_secretariat(self, mock_revoke, active_secretary):
        monitor_tenure_expiry()

        active_secretary.refresh_from_db()
        assert active_secretary.status == NBECMember.Status.EXPIRED
        mock_revoke.assert_called_once_with(str(active_secretary.keycloak_sub), "nbec_secretariat")

    @patch("shared.keycloak_admin.revoke_realm_role")
    def test_chair_role_revokes_nbec_member(self, mock_revoke, active_chair):
        monitor_tenure_expiry()

        active_chair.refresh_from_db()
        assert active_chair.status == NBECMember.Status.EXPIRED
        mock_revoke.assert_called_once_with(str(active_chair.keycloak_sub), "nbec_member")

    @patch("shared.keycloak_admin.revoke_realm_role")
    def test_keycloak_failure_does_not_block_db_expiry(self, mock_revoke, active_member):
        mock_revoke.side_effect = Exception("Keycloak unreachable")

        result = monitor_tenure_expiry()

        # DB expiry must still happen even when Keycloak call fails
        active_member.refresh_from_db()
        assert active_member.status == NBECMember.Status.EXPIRED
        assert result["expired"] == 1

    @patch("shared.keycloak_admin.revoke_realm_role")
    def test_skips_members_not_yet_expired(self, mock_revoke, db):
        NBECMember.objects.create(
            keycloak_sub=uuid.uuid4(),
            full_name="Future Expiry",
            email="future@example.com",
            role=NBECMember.Role.MEMBER,
            status=NBECMember.Status.ACTIVE,
            appointment_date=datetime.date(2026, 1, 1),
            tenure_end_date=datetime.date(2099, 12, 31),
        )
        result = monitor_tenure_expiry()

        assert result["expired"] == 0
        mock_revoke.assert_not_called()

    @patch("shared.keycloak_admin.revoke_realm_role")
    def test_skips_already_expired_members(self, mock_revoke, db):
        NBECMember.objects.create(
            keycloak_sub=uuid.uuid4(),
            full_name="Already Gone",
            email="gone@example.com",
            role=NBECMember.Role.MEMBER,
            status=NBECMember.Status.EXPIRED,
            appointment_date=datetime.date(2024, 1, 1),
            tenure_end_date=datetime.date(2024, 6, 1),
        )
        result = monitor_tenure_expiry()

        assert result["expired"] == 0
        mock_revoke.assert_not_called()

    @patch("shared.keycloak_admin.revoke_realm_role")
    def test_audit_event_recorded(self, mock_revoke, active_member):
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
