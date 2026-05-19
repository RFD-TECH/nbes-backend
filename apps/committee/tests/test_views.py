"""apps/committee/tests/test_views.py — NBEC Committee API view tests."""
import datetime
import json
import uuid
import jwt
import pytest
from django.conf import settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.committee.models import (
    ConflictDeclaration,
    Meeting,
    Minutes,
    NBECMember,
)


# ── JWT helper ────────────────────────────────────────────────────────────────

def _token(roles=None, sub=None):
    """Return a dev HS256 JWT with the given roles."""
    payload = {
        "sub": str(sub or "00000000-0000-0000-0000-000000000001"),
        "email": "test@example.com",
        "realm_access": {"roles": roles or ["nbec-secretariat"]},
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm="HS256")


def _client(roles=None, sub=None):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {_token(roles, sub)}")
    return c


def _secretariat_client():
    return _client(roles=["nbec-secretariat", "system-administrator"])


ACTOR_SUB = "00000000-0000-0000-0000-000000000001"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def member(db):
    from apps.users.models import Role, Permission, RolePermission
    # Ensure the committee:manage permission exists for tests
    role, _ = Role.objects.get_or_create(
        name="system-administrator",
        defaults={"display_name": "System Administrator", "is_active": True},
    )
    perm, _ = Permission.objects.get_or_create(
        codename="committee:manage",
        defaults={"display_name": "Manage Committee"},
    )
    RolePermission.objects.get_or_create(role=role, permission=perm)

    return NBECMember.objects.create(
        keycloak_sub=ACTOR_SUB,
        full_name="View Test Member",
        email="view@example.com",
        role=NBECMember.Role.MEMBER,
        appointment_date=datetime.date(2026, 1, 1),
    )


@pytest.fixture
def meeting(db):
    return Meeting.objects.create(
        reference="MTG-VIEW-001",
        meeting_type=Meeting.MeetingType.ORDINARY,
        scheduled_date="2026-09-01T10:00:00Z",
        venue="Conference Room",
        quorum_required=2,
    )


@pytest.fixture
def adjourned_meeting(db):
    return Meeting.objects.create(
        reference="MTG-VIEW-ADJ",
        meeting_type=Meeting.MeetingType.ORDINARY,
        scheduled_date="2026-09-01T10:00:00Z",
        quorum_required=2,
        status=Meeting.Status.ADJOURNED,
        attendees=[str(uuid.uuid4()), str(uuid.uuid4())],
    )


@pytest.fixture
def unsigned_minutes(adjourned_meeting):
    return Minutes.objects.create(
        meeting=adjourned_meeting,
        content="Draft minutes for signing.",
    )


@pytest.fixture
def signed_minutes(adjourned_meeting):
    mins = Minutes.objects.create(
        meeting=adjourned_meeting,
        content="Signed minutes.",
    )
    mins.sign(chair_id=ACTOR_SUB)
    return mins


# ── POST /api/v1/nbec/members/ ────────────────────────────────────────────────

@pytest.mark.django_db
class TestMemberCreate:
    url = "/api/v1/nbec/members/"

    def test_create_member_success(self, db):
        client = _secretariat_client()
        resp = client.post(self.url, data={
            "keycloak_sub": "aaaaaaaa-0000-0000-0000-000000000001",
            "full_name": "New Member",
            "email": "new@example.com",
            "role": "member",
            "appointment_date": "2026-01-01",
            "is_voting_member": True,
        }, format="json")
        assert resp.status_code == 201
        assert resp.json()["success"] is True
        assert resp.json()["data"]["full_name"] == "New Member"

    def test_unauthenticated_rejected(self):
        resp = APIClient().post(self.url, data={}, format="json")
        assert resp.status_code == 401

    def test_missing_required_fields_rejected(self, db):
        client = _secretariat_client()
        resp = client.post(self.url, data={"full_name": "No email"}, format="json")
        assert resp.status_code == 400


# ── PATCH /api/v1/nbec/members/{id}/ ─────────────────────────────────────────

@pytest.mark.django_db
class TestMemberAmend:
    def test_amend_member(self, member):
        client = _secretariat_client()
        url = f"/api/v1/nbec/members/{member.id}/"
        resp = client.patch(url, data={"full_name": "Amended Name"}, format="json")
        assert resp.status_code == 200
        assert resp.json()["data"]["full_name"] == "Amended Name"

    def test_amend_nonexistent_returns_404(self, db):
        client = _secretariat_client()
        url = f"/api/v1/nbec/members/{uuid.uuid4()}/"
        resp = client.patch(url, data={"full_name": "X"}, format="json")
        assert resp.status_code == 404


# ── POST /api/v1/nbec/members/{id}/activate/ ──────────────────────────────────

@pytest.mark.django_db
class TestMemberActivate:
    def test_activate_member(self, member):
        client = _secretariat_client()
        url = f"/api/v1/nbec/members/{member.id}/activate/"
        resp = client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "active"


# ── POST /api/v1/nbec/coi/ ───────────────────────────────────────────────────

@pytest.mark.django_db
class TestCOIDeclare:
    url = "/api/v1/nbec/coi/"

    def test_declare_coi(self, member):
        client = _client(roles=["nbec-member"])
        resp = client.post(self.url, data={
            "member": str(member.id),
            "subject_type": "supplier",
            "subject_description": "I know the director of this company",
            "nature": "financial",
        }, format="json")
        assert resp.status_code == 201
        assert resp.json()["data"]["status"] == "pending"

    def test_unauthenticated_rejected(self):
        resp = APIClient().post(self.url, data={}, format="json")
        assert resp.status_code == 401


# ── POST /api/v1/nbec/coi/{id}/review/ ───────────────────────────────────────

@pytest.mark.django_db
class TestCOIReview:
    def test_approve_coi(self, member):
        coi = ConflictDeclaration.objects.create(
            member=member,
            subject_description="COI to review",
            subject_type="other",
        )
        client = _secretariat_client()
        url = f"/api/v1/nbec/coi/{coi.id}/review/"
        resp = client.post(url, data={"approved": True}, format="json")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "approved"

    def test_dismiss_coi(self, member):
        coi = ConflictDeclaration.objects.create(
            member=member,
            subject_description="COI to dismiss",
            subject_type="other",
        )
        client = _secretariat_client()
        url = f"/api/v1/nbec/coi/{coi.id}/review/"
        resp = client.post(url, data={"approved": False}, format="json")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "dismissed"

    def test_nonexistent_coi_returns_404(self, db):
        client = _secretariat_client()
        url = f"/api/v1/nbec/coi/{uuid.uuid4()}/review/"
        resp = client.post(url, data={"approved": True}, format="json")
        assert resp.status_code == 404


# ── POST /api/v1/nbec/meetings/ ──────────────────────────────────────────────

@pytest.mark.django_db
class TestMeetingCreate:
    url = "/api/v1/nbec/meetings/"

    def test_schedule_meeting(self, db):
        client = _secretariat_client()
        resp = client.post(self.url, data={
            "reference": "MTG-API-001",
            "meeting_type": "ordinary",
            "scheduled_date": "2026-10-01T10:00:00Z",
            "venue": "Board Room",
            "quorum_required": 5,
        }, format="json")
        assert resp.status_code == 201
        assert resp.json()["data"]["status"] == "draft"


# ── POST /api/v1/nbec/meetings/{id}/agenda/ ───────────────────────────────────

@pytest.mark.django_db
class TestMeetingAgenda:
    def test_publish_agenda(self, meeting):
        client = _secretariat_client()
        url = f"/api/v1/nbec/meetings/{meeting.id}/agenda/"
        resp = client.post(url, data={
            "items": [{"order": 1, "title": "Opening"}],
        }, format="json")
        assert resp.status_code == 201
        assert resp.json()["data"]["version"] == 1
        meeting.refresh_from_db()
        assert meeting.status == Meeting.Status.AGENDA_ISSUED

    def test_nonexistent_meeting_returns_404(self, db):
        client = _secretariat_client()
        url = f"/api/v1/nbec/meetings/{uuid.uuid4()}/agenda/"
        resp = client.post(url, data={"items": []}, format="json")
        assert resp.status_code == 404


# ── POST /api/v1/nbec/meetings/{id}/attendance/ ───────────────────────────────

@pytest.mark.django_db
class TestMeetingAttendance:
    def test_record_attendance(self, meeting):
        client = _secretariat_client()
        url = f"/api/v1/nbec/meetings/{meeting.id}/attendance/"
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        resp = client.post(url, data={"attendee_ids": ids}, format="json")
        assert resp.status_code == 200
        meeting.refresh_from_db()
        assert len(meeting.attendees) == 2


# ── POST /api/v1/nbec/meetings/{id}/convene/ and /adjourn/ ────────────────────

@pytest.mark.django_db
class TestMeetingConveneAdjourn:
    def test_convene_with_quorum(self, meeting):
        from apps.committee import services
        ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
        services.record_attendance(ACTOR_SUB, meeting, ids)
        meeting.refresh_from_db()
        client = _secretariat_client()
        url = f"/api/v1/nbec/meetings/{meeting.id}/convene/"
        resp = client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "convened"

    def test_convene_without_quorum_returns_400(self, meeting):
        client = _secretariat_client()
        url = f"/api/v1/nbec/meetings/{meeting.id}/convene/"
        resp = client.post(url, format="json")
        assert resp.status_code == 400

    def test_adjourn_convened_meeting(self, meeting):
        from apps.committee import services
        ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
        services.record_attendance(ACTOR_SUB, meeting, ids)
        meeting.refresh_from_db()
        services.convene_meeting(ACTOR_SUB, meeting)
        meeting.refresh_from_db()
        client = _secretariat_client()
        url = f"/api/v1/nbec/meetings/{meeting.id}/adjourn/"
        resp = client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "adjourned"


# ── POST /api/v1/nbec/minutes/{id}/sign/ ─────────────────────────────────────

@pytest.mark.django_db
class TestMinutesSign:
    def test_sign_minutes(self, unsigned_minutes):
        client = _secretariat_client()
        url = f"/api/v1/nbec/minutes/{unsigned_minutes.id}/sign/"
        resp = client.post(url, data={"signature_ref": "sig/abc.sig"}, format="json")
        assert resp.status_code == 200
        assert resp.json()["data"]["approved"] is True

    def test_sign_already_signed_returns_400(self, signed_minutes):
        client = _secretariat_client()
        url = f"/api/v1/nbec/minutes/{signed_minutes.id}/sign/"
        resp = client.post(url, data={}, format="json")
        assert resp.status_code == 400

    def test_sign_nonexistent_returns_404(self, db):
        client = _secretariat_client()
        url = f"/api/v1/nbec/minutes/{uuid.uuid4()}/sign/"
        resp = client.post(url, data={}, format="json")
        assert resp.status_code == 404


# ── POST /api/v1/nbec/minutes/{id}/addendum/ ─────────────────────────────────

@pytest.mark.django_db
class TestMinutesAddendum:
    def test_issue_addendum(self, signed_minutes):
        client = _secretariat_client()
        url = f"/api/v1/nbec/minutes/{signed_minutes.id}/addendum/"
        resp = client.post(url, data={
            "content": "Correction to agenda item 2: the vote was unanimous."
        }, format="json")
        assert resp.status_code == 201
        assert "Correction" in resp.json()["data"]["content"]

    def test_addendum_on_unsigned_returns_400(self, unsigned_minutes):
        client = _secretariat_client()
        url = f"/api/v1/nbec/minutes/{unsigned_minutes.id}/addendum/"
        resp = client.post(url, data={"content": "Some addendum."}, format="json")
        assert resp.status_code == 400


# ── GET /api/v1/nbec/policy/coi/ ─────────────────────────────────────────────

@pytest.mark.django_db
class TestCOIPolicy:
    url = "/api/v1/nbec/policy/coi/"

    def test_no_conflict(self, member):
        client = _client()
        resp = client.get(self.url, {"member_id": str(member.id), "entity_type": "item"})
        assert resp.status_code == 200
        assert resp.json()["data"]["has_active_conflict"] is False

    def test_with_active_conflict(self, member):
        coi = ConflictDeclaration.objects.create(
            member=member,
            subject_description="conflict",
            subject_type="candidate",
            affected_entity_type="candidate",
            status=ConflictDeclaration.Status.APPROVED,
        )
        client = _client()
        resp = client.get(self.url, {
            "member_id": str(member.id),
            "entity_type": "candidate",
        })
        assert resp.status_code == 200
        assert resp.json()["data"]["has_active_conflict"] is True

    def test_missing_member_id_returns_400(self, db):
        client = _client()
        resp = client.get(self.url)
        assert resp.status_code == 400
