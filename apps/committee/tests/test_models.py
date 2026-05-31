"""apps/committee/tests/test_models.py — NBECMember, Meeting, Minutes, etc."""
import datetime
import pytest
from django.utils import timezone

from apps.committee.models import (
    ActionItem,
    Agenda,
    ConflictDeclaration,
    Meeting,
    Minutes,
    MinutesAddendum,
    NBECMember,
)


@pytest.fixture
def member(db):
    return NBECMember.objects.create(
        keycloak_sub="00000000-0000-0000-0000-000000000001",
        full_name="Test Member",
        email="member@example.com",
        role=NBECMember.Role.MEMBER,
        status=NBECMember.Status.DRAFT,
        appointment_date=datetime.date(2026, 1, 1),
    )


@pytest.fixture
def meeting(db):
    return Meeting.objects.create(
        reference="MTG-2026-001",
        meeting_type=Meeting.MeetingType.ORDINARY,
        scheduled_date=timezone.now() + datetime.timedelta(days=7),
        venue="Boardroom A",
        quorum_required=3,
    )


@pytest.fixture
def minutes_obj(db, meeting):
    meeting.status = Meeting.Status.ADJOURNED
    meeting.save()
    return Minutes.objects.create(meeting=meeting, content="Draft minutes content.")


# ── NBECMember ────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestNBECMember:
    def test_create_defaults(self, member):
        assert member.status == NBECMember.Status.DRAFT
        assert member.is_active is False
        assert member.is_voting_member is True

    def test_activate_from_draft(self, member):
        member.activate()
        assert member.status == NBECMember.Status.ACTIVE
        assert member.is_active is True

    def test_activate_already_active_raises(self, member):
        member.activate()
        with pytest.raises(ValueError):
            member.activate()

    def test_expire(self, member):
        member.activate()
        member.expire()
        assert member.status == NBECMember.Status.EXPIRED
        assert member.is_active is False

    def test_str(self, member):
        assert "Test Member" in str(member)

    def test_optional_fields_blank(self, db):
        m = NBECMember.objects.create(
            keycloak_sub="00000000-0000-0000-0000-000000000099",
            full_name="Plain Member",
            email="plain@example.com",
            role=NBECMember.Role.MEMBER,
            appointment_date=datetime.date(2026, 1, 1),
        )
        assert m.title == ""
        assert m.post_nominals == ""
        assert m.photo_ref == ""
        assert m.instrument_ref is None


# ── Meeting ───────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestMeeting:
    def test_create_defaults(self, meeting):
        assert meeting.status == Meeting.Status.DRAFT
        assert meeting.meeting_type == Meeting.MeetingType.ORDINARY

    def test_quorum_not_met_when_no_attendees(self, meeting):
        assert meeting.quorum_met is False

    def test_quorum_met(self, meeting):
        meeting.attendees = ["uuid1", "uuid2", "uuid3"]
        assert meeting.quorum_met is True

    def test_str(self, meeting):
        assert "MTG-2026-001" in str(meeting)


# ── Minutes ───────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestMinutes:
    def test_sign_seals_minutes(self, minutes_obj):
        minutes_obj.sign(chair_id="00000000-0000-0000-0000-000000000001")
        assert minutes_obj.approved is True
        assert minutes_obj.immutable_at is not None
        assert minutes_obj.approved_by_id is not None

    def test_sign_twice_raises(self, minutes_obj):
        minutes_obj.sign(chair_id="00000000-0000-0000-0000-000000000001")
        with pytest.raises(ValueError):
            minutes_obj.sign(chair_id="00000000-0000-0000-0000-000000000001")

    def test_sign_stores_signature_ref(self, minutes_obj):
        minutes_obj.sign(chair_id="00000000-0000-0000-0000-000000000001",
                         signature_ref="minio/signatures/abc.sig")
        assert minutes_obj.signature_ref == "minio/signatures/abc.sig"


# ── MinutesAddendum ───────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestMinutesAddendum:
    def test_create(self, minutes_obj):
        minutes_obj.sign(chair_id="00000000-0000-0000-0000-000000000001")
        addendum = MinutesAddendum.objects.create(
            minutes=minutes_obj,
            content="Correction to item 3.",
            issued_by_id="00000000-0000-0000-0000-000000000001",
        )
        assert addendum.minutes == minutes_obj
        assert addendum.content == "Correction to item 3."


# ── ConflictDeclaration ───────────────────────────────────────────────────────

@pytest.mark.django_db
class TestConflictDeclaration:
    def test_create_defaults_to_pending(self, member):
        coi = ConflictDeclaration.objects.create(
            member=member,
            subject_description="Financial interest in supplier X",
            subject_type=ConflictDeclaration.SubjectType.SUPPLIER,
            nature=ConflictDeclaration.Nature.FINANCIAL,
        )
        assert coi.status == ConflictDeclaration.Status.PENDING

    def test_str(self, member):
        coi = ConflictDeclaration.objects.create(
            member=member,
            subject_description="conflict",
            affected_entity_type="item",
        )
        assert "Conflict" in str(coi)


# ── ActionItem ────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestActionItem:
    def test_create(self, meeting):
        item = ActionItem.objects.create(
            meeting=meeting,
            description="Prepare exam report",
            assigned_to_id="00000000-0000-0000-0000-000000000001",
            due_date=datetime.date(2026, 6, 1),
        )
        assert item.status == ActionItem.Status.OPEN
        assert item.minutes is None

    def test_verified_status_exists(self):
        assert hasattr(ActionItem.Status, "VERIFIED")

    def test_link_to_minutes(self, minutes_obj, meeting):
        minutes_obj.sign(chair_id="00000000-0000-0000-0000-000000000001")
        item = ActionItem.objects.create(
            meeting=meeting,
            minutes=minutes_obj,
            description="Follow-up on exam schedule",
            assigned_to_id="00000000-0000-0000-0000-000000000001",
            due_date=datetime.date(2026, 6, 1),
        )
        assert item.minutes == minutes_obj


# ── Agenda ────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestAgenda:
    def test_create(self, meeting):
        agenda = Agenda.objects.create(
            meeting=meeting,
            version=1,
            items=[{"order": 1, "title": "Opening"}],
            created_by_id="00000000-0000-0000-0000-000000000001",
        )
        assert agenda.version == 1
        assert len(agenda.items) == 1

    def test_version_unique_per_meeting(self, meeting):
        from django.db import IntegrityError
        Agenda.objects.create(
            meeting=meeting, version=1, items=[],
            created_by_id="00000000-0000-0000-0000-000000000001",
        )
        with pytest.raises(IntegrityError):
            Agenda.objects.create(
                meeting=meeting, version=1, items=[],
                created_by_id="00000000-0000-0000-0000-000000000001",
            )
