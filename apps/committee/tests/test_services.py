"""apps/committee/tests/test_services.py — committee service layer tests."""
import datetime
import uuid
import pytest
from django.utils import timezone

from apps.committee.models import (
    ConflictDeclaration,
    Meeting,
    Minutes,
    NBECMember,
)
from apps.committee import services

ACTOR = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def member(db):
    return NBECMember.objects.create(
        keycloak_sub=ACTOR,
        full_name="Alice Chair",
        email="alice@example.com",
        role=NBECMember.Role.MEMBER,
        appointment_date=datetime.date(2026, 1, 1),
    )


@pytest.fixture
def meeting(db):
    return Meeting.objects.create(
        reference="MTG-SVC-001",
        meeting_type=Meeting.MeetingType.ORDINARY,
        scheduled_date=timezone.now() + datetime.timedelta(days=3),
        quorum_required=2,
    )


@pytest.fixture
def adjourned_meeting(db):
    m = Meeting.objects.create(
        reference="MTG-SVC-002",
        meeting_type=Meeting.MeetingType.ORDINARY,
        scheduled_date=timezone.now(),
        quorum_required=2,
        status=Meeting.Status.ADJOURNED,
        attendees=[str(uuid.uuid4()), str(uuid.uuid4())],
    )
    return m


@pytest.fixture
def signed_minutes(adjourned_meeting):
    mins = Minutes.objects.create(meeting=adjourned_meeting, content="Official minutes.")
    mins.sign(chair_id=ACTOR)
    return mins


# ── create_member ─────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestCreateMember:
    def test_creates_member_in_draft(self, db):
        member = services.create_member(ACTOR, {
            "keycloak_sub": "10000000-0000-0000-0000-000000000001",
            "full_name": "Bob Smith",
            "email": "bob@example.com",
            "role": NBECMember.Role.MEMBER,
            "appointment_date": datetime.date(2026, 3, 1),
        })
        assert member.status == NBECMember.Status.DRAFT
        assert NBECMember.objects.filter(pk=member.pk).exists()

    def test_audit_event_created(self, db):
        from apps.audit.models import AuditEvent
        before = AuditEvent.objects.count()
        services.create_member(ACTOR, {
            "keycloak_sub": "20000000-0000-0000-0000-000000000001",
            "full_name": "Carol Jones",
            "email": "carol@example.com",
            "role": NBECMember.Role.MEMBER,
            "appointment_date": datetime.date(2026, 3, 1),
        })
        assert AuditEvent.objects.count() > before


# ── amend_member ──────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestAmendMember:
    def test_amends_fields(self, member):
        member = services.amend_member(ACTOR, member, {"full_name": "Alice Updated"})
        assert member.full_name == "Alice Updated"

    def test_audit_event_on_amend(self, member):
        from apps.audit.models import AuditEvent
        before = AuditEvent.objects.count()
        services.amend_member(ACTOR, member, {"email": "newemail@example.com"})
        assert AuditEvent.objects.count() > before


# ── declare_coi / review_coi ──────────────────────────────────────────────────

@pytest.mark.django_db
class TestCOI:
    def test_declare_creates_pending_coi(self, member):
        coi = services.declare_coi(ACTOR, {
            "member": member,
            "subject_description": "I know the supplier",
            "subject_type": ConflictDeclaration.SubjectType.SUPPLIER,
            "nature": ConflictDeclaration.Nature.FINANCIAL,
        })
        assert coi.status == ConflictDeclaration.Status.PENDING

    def test_approve_coi(self, member):
        coi = services.declare_coi(ACTOR, {
            "member": member,
            "subject_description": "conflict",
            "subject_type": ConflictDeclaration.SubjectType.OTHER,
        })
        coi = services.review_coi(ACTOR, coi, approve=True)
        assert coi.status == ConflictDeclaration.Status.APPROVED
        assert coi.reviewed_by_id is not None

    def test_dismiss_coi(self, member):
        coi = services.declare_coi(ACTOR, {
            "member": member,
            "subject_description": "conflict",
            "subject_type": ConflictDeclaration.SubjectType.OTHER,
        })
        coi = services.review_coi(ACTOR, coi, approve=False)
        assert coi.status == ConflictDeclaration.Status.DISMISSED

    def test_double_review_raises(self, member):
        coi = services.declare_coi(ACTOR, {
            "member": member,
            "subject_description": "conflict",
            "subject_type": ConflictDeclaration.SubjectType.OTHER,
        })
        services.review_coi(ACTOR, coi, approve=True)
        with pytest.raises(ValueError):
            services.review_coi(ACTOR, coi, approve=False)

    def test_check_coi_returns_active(self, member):
        coi = services.declare_coi(ACTOR, {
            "member": member,
            "subject_description": "conflict",
            "subject_type": ConflictDeclaration.SubjectType.CANDIDATE,
            "affected_entity_type": "candidate",
        })
        services.review_coi(ACTOR, coi, approve=True)
        result = services.check_coi(member.id, "candidate")
        assert result["has_active_conflict"] is True

    def test_check_coi_no_conflict(self, member):
        result = services.check_coi(member.id, "item")
        assert result["has_active_conflict"] is False


# ── schedule_meeting ──────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestScheduleMeeting:
    def test_creates_draft_meeting(self, db):
        m = services.schedule_meeting(ACTOR, {
            "reference": "MTG-NEW-001",
            "meeting_type": Meeting.MeetingType.ORDINARY,
            "scheduled_date": timezone.now() + datetime.timedelta(days=5),
            "venue": "Room 1",
            "quorum_required": 3,
        })
        assert m.status == Meeting.Status.DRAFT
        assert Meeting.objects.filter(pk=m.pk).exists()


# ── publish_agenda ────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestPublishAgenda:
    def test_creates_agenda_v1(self, meeting):
        agenda = services.publish_agenda(
            ACTOR, meeting,
            items=[{"order": 1, "title": "Welcome"}],
        )
        assert agenda.version == 1
        assert meeting.status == Meeting.Status.AGENDA_ISSUED

    def test_subsequent_agenda_increments_version(self, meeting):
        services.publish_agenda(ACTOR, meeting, items=[{"order": 1, "title": "v1"}])
        agenda2 = services.publish_agenda(ACTOR, meeting, items=[{"order": 1, "title": "v2"}])
        assert agenda2.version == 2


# ── record_attendance / convene / adjourn ──────────────────────────────────────

@pytest.mark.django_db
class TestMeetingLifecycle:
    def test_record_attendance(self, meeting):
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        meeting = services.record_attendance(ACTOR, meeting, ids)
        assert len(meeting.attendees) == 2

    def test_convene_quorum_met(self, meeting):
        ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
        services.record_attendance(ACTOR, meeting, ids)
        meeting.refresh_from_db()
        meeting = services.convene_meeting(ACTOR, meeting)
        assert meeting.status == Meeting.Status.CONVENED
        assert meeting.convened_at is not None

    def test_convene_quorum_not_met_raises(self, meeting):
        with pytest.raises(ValueError, match="Quorum not met"):
            services.convene_meeting(ACTOR, meeting)

    def test_adjourn(self, meeting):
        ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
        services.record_attendance(ACTOR, meeting, ids)
        meeting.refresh_from_db()
        meeting = services.convene_meeting(ACTOR, meeting)
        meeting, minutes = services.adjourn_meeting(ACTOR, meeting)
        assert meeting.status == Meeting.Status.ADJOURNED
        assert meeting.adjourned_at is not None
        assert minutes.meeting_id == meeting.id

    def test_adjourn_not_convened_raises(self, meeting):
        with pytest.raises(ValueError):
            services.adjourn_meeting(ACTOR, meeting)


# ── sign_minutes / issue_addendum ─────────────────────────────────────────────

@pytest.mark.django_db
class TestMinutesServices:
    def test_sign_minutes(self, adjourned_meeting):
        mins = Minutes.objects.create(meeting=adjourned_meeting, content="Minutes.")
        mins = services.sign_minutes(ACTOR, mins)
        assert mins.approved is True
        adjourned_meeting.refresh_from_db()
        assert adjourned_meeting.status == Meeting.Status.MINUTED

    def test_sign_already_signed_raises(self, signed_minutes):
        with pytest.raises(ValueError):
            services.sign_minutes(ACTOR, signed_minutes)

    def test_issue_addendum(self, signed_minutes):
        addendum = services.issue_addendum(ACTOR, signed_minutes, content="Addendum text here.")
        assert addendum.issued_by_id is not None
        assert addendum.content == "Addendum text here."

    def test_addendum_on_unsigned_minutes_raises(self, adjourned_meeting):
        mins = Minutes.objects.create(meeting=adjourned_meeting, content="unsigned")
        with pytest.raises(ValueError):
            services.issue_addendum(ACTOR, mins, content="Cannot add this.")
