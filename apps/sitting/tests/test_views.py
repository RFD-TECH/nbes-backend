"""apps/sitting/tests/test_views.py — Phase 4 API integration tests.

Focuses on:

* RBAC — endpoints require the right permission codename.
* §71 enforcement at the API boundary.
* Error envelope consistency.
* Snapshot stability over HTTP.
"""
import datetime
import uuid

import jwt
import pytest
from django.conf import settings
from django.urls import reverse
from rest_framework.test import APIClient



SECRETARIAT_SUB = "11111111-1111-1111-1111-111111111111"
SUBJECTS = ["CIV", "CRIM", "EVID", "ETHICS", "PROP"]


def _token(roles: list[str], sub: str = SECRETARIAT_SUB) -> str:
    """Mint a dev HS256 token mimicking the Keycloak claim shape."""
    return jwt.encode(
        {
            "sub": sub,
            "email": "sec@example.gh",
            "realm_access": {"roles": roles},
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def _client(roles: list[str], sub: str = SECRETARIAT_SUB) -> APIClient:
    """APIClient with Bearer + Idempotency-Key headers pre-set.

    ``IdempotencyKeyMiddleware`` rejects state-mutating verbs without an
    Idempotency-Key, so we mint a fresh one per test client. Tests that
    need to replay a key (e.g. for retry/dedup behaviour) can override via
    ``client.credentials(HTTP_IDEMPOTENCY_KEY=...)``.
    """
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {_token(roles, sub)}",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    return client


@pytest.fixture
def nbec_client(db):
    return _client(["nbec_member"])


@pytest.fixture
def unauth_client(db):
    return APIClient()


def _payload(date_offset_days: int = 60) -> dict:
    today = datetime.date.today()
    start = today + datetime.timedelta(days=date_offset_days)
    return {
        "ref": "BAR-2028-01",
        "sitting_date": start.isoformat(),
        "sitting_end_date": (start + datetime.timedelta(days=4)).isoformat(),
        "pass_mark": "50.00",
        "pass_rule": "all_pass",
    }


# ── Create / RBAC ──────────────────────────────────────────────────────────


def test_unauthenticated_sitting_create_rejected(unauth_client):
    """Without auth or idempotency-key, the request must be rejected somewhere
    in the middleware/permission chain — not silently succeed."""
    resp = unauth_client.post(
        reverse("sitting:sitting-list"),
        data=_payload(),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    # Auth middleware returns 401; permission_classes can also return 403.
    assert resp.status_code in (401, 403)


def test_create_sitting_success(nbec_client):
    resp = nbec_client.post(
        reverse("sitting:sitting-list"), data=_payload(), format="json",
    )
    assert resp.status_code == 201, resp.data
    body = resp.data
    assert body["success"] is True
    assert body["data"]["ref"] == "BAR-2028-01"
    assert body["data"]["status"] == "draft"


# ── §71 enforcement ────────────────────────────────────────────────────────


def test_configure_rejects_when_fewer_than_five_papers(nbec_client, db):
    """§71 — sitting cannot be configured with fewer than five papers."""
    nbec_client.post(
        reverse("sitting:sitting-list"), data=_payload(), format="json",
    )
    # Configure should fail with no papers attached.
    resp = nbec_client.post(reverse("sitting:sitting-configure", args=["BAR-2028-01"]))
    assert resp.status_code == 400, resp.data
    assert resp.data["success"] is False
    assert resp.data["error"]["code"] == "SITTING_NOT_READY"


# ── Snapshot endpoint ──────────────────────────────────────────────────────


def test_snapshot_endpoint_returns_live_for_draft(nbec_client, db):
    nbec_client.post(
        reverse("sitting:sitting-list"), data=_payload(), format="json",
    )
    resp = nbec_client.get(reverse("sitting:sitting-snapshot", args=["BAR-2028-01"]))
    assert resp.status_code == 200, resp.data
    assert resp.data["data"]["ref"] == "BAR-2028-01"
    assert resp.data["data"]["status"] == "draft"


def test_snapshot_endpoint_404_on_unknown_ref(nbec_client, db):
    resp = nbec_client.get(reverse("sitting:sitting-snapshot", args=["BAR-9999-12"]))
    assert resp.status_code == 404, resp.data


# ── Error envelope ─────────────────────────────────────────────────────────


def test_error_envelope_includes_code_and_details(nbec_client, db):
    """Validation failures must surface as our standard {code, message, details}."""
    # Section marks don't sum — should fail with INCONSISTENT_MARKS_ALLOCATION.
    nbec_client.post(
        reverse("sitting:sitting-list"), data=_payload(), format="json",
    )
    resp = nbec_client.post(
        reverse("sitting:sitting-paper-upsert", args=["BAR-2028-01"]),
        data={
            "subject_code": "CIV",
            "subject_name": "Civil",
            "mode": "cbt",
            "total_marks": 100,
            "pass_mark": "50.00",
            "duration_minutes": 180,
            "sections": [
                {"name": "A", "marks": 40},
                {"name": "B", "marks": 50},
            ],
        },
        format="json",
    )
    assert resp.status_code == 400, resp.data
    assert resp.data["error"]["code"] == "INCONSISTENT_MARKS_ALLOCATION"
