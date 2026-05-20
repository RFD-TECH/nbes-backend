"""apps/audit/tests/test_views.py — AuditSearchView and AuditChainView."""
import uuid
import jwt
import pytest
from django.conf import settings
from rest_framework.test import APIClient

from apps.audit.models import AuditEvent


def _token(roles=None):
    payload = {
        "sub": str(uuid.uuid4()),
        "email": "auditor@example.com",
        "realm_access": {"roles": roles or ["auditor"]},
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm="HS256")


def _client(roles=None):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {_token(roles)}")
    return c


@pytest.fixture(autouse=True)
def seed_audit_permission(db):
    """Ensure the auditor role has audit:export permission."""
    from apps.users.models import Role, Permission, RolePermission
    role, _ = Role.objects.get_or_create(
        name="auditor",
        defaults={"display_name": "Auditor", "is_active": True},
    )
    perm, _ = Permission.objects.get_or_create(
        codename="audit:export",
        defaults={"display_name": "Export Audit Trail"},
    )
    RolePermission.objects.get_or_create(role=role, permission=perm)


@pytest.fixture
def audit_events(db):
    """Create a small chain of audit events for search/chain tests."""
    events = []
    for i in range(3):
        evt = AuditEvent.record(
            actor_id=uuid.uuid4(),
            action=f"TEST_ACTION_{i}",
            entity_type="test_entity",
            entity_id=uuid.uuid4(),
        )
        events.append(evt)
    return events


# ── GET /api/v1/audit/search/ ─────────────────────────────────────────────────

@pytest.mark.django_db
class TestAuditSearch:
    url = "/api/v1/audit/search/"

    def test_unauthenticated_returns_401(self):
        resp = APIClient().get(self.url)
        assert resp.status_code == 401

    def test_returns_paginated_results(self, audit_events):
        resp = _client().get(self.url)
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body or body.get("success") is True

    def test_filter_by_action(self, audit_events):
        resp = _client().get(self.url, {"action": "TEST_ACTION_0"})
        assert resp.status_code == 200

    def test_filter_by_entity_type(self, audit_events):
        resp = _client().get(self.url, {"entity_type": "test_entity"})
        assert resp.status_code == 200

    def test_filter_by_date_from(self, audit_events):
        resp = _client().get(self.url, {"date_from": "2026-01-01"})
        assert resp.status_code == 200

    def test_filter_by_date_to(self, audit_events):
        resp = _client().get(self.url, {"date_to": "2026-12-31"})
        assert resp.status_code == 200

    def test_free_text_search(self, audit_events):
        resp = _client().get(self.url, {"q": "TEST_ACTION"})
        assert resp.status_code == 200


# ── GET /api/v1/audit/chain/{date}/ ──────────────────────────────────────────

@pytest.mark.django_db
class TestAuditChain:
    def test_no_events_returns_null_anchor(self, db):
        resp = _client().get("/api/v1/audit/chain/2020-01-01/")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["event_count"] == 0
        assert data["anchor_hash"] is None
        assert data["chain_valid"] is True

    def test_returns_anchor_hash_for_date_with_events(self, audit_events):
        from django.utils import timezone
        today = timezone.now().date().isoformat()
        resp = _client().get(f"/api/v1/audit/chain/{today}/")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["event_count"] >= 3
        assert data["anchor_hash"] is not None
        assert data["chain_valid"] is True

    def test_invalid_date_returns_400(self, db):
        resp = _client().get("/api/v1/audit/chain/not-a-date/")
        assert resp.status_code == 400

    def test_unauthenticated_returns_401(self):
        resp = APIClient().get("/api/v1/audit/chain/2026-05-01/")
        assert resp.status_code == 401
