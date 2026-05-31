"""Tests for §1.2.2  

Covers:
Mutual-exclusion enforcement at role-assignment time.
role.version increments when the permission matrix changes.
Two-administrator approval for high-privilege roles.
Full permission codename catalog (≥ 25 codenames seeded).
"""

import uuid

import jwt
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.users.models import (
    Permission,
    Role,
    RoleAssignmentApproval,
    RoleChangeEvent,
    RoleMutualExclusion,
    UserProfile,
    UserRole,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_token(sub, email, super_admin=True, roles=None):
    payload = {"sub": str(sub), "email": email}
    if super_admin:
        payload["realm_access"] = {"roles": ["super_admin"]}
    elif roles:
        payload["resource_access"] = {"nbes-api": {"roles": roles}}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm="HS256")


def _make_admin(email="admin@example.com"):
    """Create an active admin UserProfile + token."""
    sub = uuid.uuid4()
    role, _ = Role.objects.get_or_create(
        name="system_administrator",
        defaults={"is_active": True, "is_custom": False, "is_internal": True},
    )
    user = UserProfile.objects.create(
        keycloak_sub=sub,
        email=email,
        first_name="Admin",
        last_name="User",
        status="active",
    )
    UserRole.objects.create(user=user, role=role, effective_from=timezone.now().date())
    token = _make_token(sub, email, super_admin=True)
    return user, token


def _get_or_create_role(name, is_internal=True):
    role, _ = Role.objects.get_or_create(
        name=name,
        defaults={"is_active": True, "is_custom": False, "is_internal": is_internal},
    )
    return role


# ── Mutual-Exclusion Model ──────────────────────────────────────────


class MutualExclusionModelTests(TestCase):
    """Unit tests for RoleMutualExclusion.check_conflict()."""

    def setUp(self):
        self.item_writer = _get_or_create_role("item_writer")
        self.moderator = _get_or_create_role("moderator")
        self.examiner = _get_or_create_role("examiner")
        self.user = UserProfile.objects.create(
            email="testuser@example.com", status="active"
        )
        # Ensure the canonical exclusion exists (from migration 0010)
        # Use sorted names to respect the unique_together constraint
        a, b = sorted([self.item_writer, self.moderator], key=lambda r: r.name)
        RoleMutualExclusion.objects.get_or_create(
            role_a=a,
            role_b=b,
            defaults={"reason": "Conflict of interest"},
        )
        # Assign item_writer to user
        UserRole.objects.create(
            user=self.user,
            role=self.item_writer,
            effective_from=timezone.now().date(),
        )

    def test_check_conflict_returns_exclusion_when_conflict_exists(self):
        """check_conflict should find item_writer ↔ moderator conflict."""
        conflict = RoleMutualExclusion.check_conflict(self.user, self.moderator)
        self.assertIsNotNone(conflict)

    def test_check_conflict_returns_none_for_compatible_roles(self):
        """Examiner is not excluded from item_writer — no conflict expected."""
        conflict = RoleMutualExclusion.check_conflict(self.user, self.examiner)
        self.assertIsNone(conflict)

    def test_check_conflict_both_orderings(self):
        """check_conflict must work regardless of which role is 'incoming'."""
        user2 = UserProfile.objects.create(email="user2@example.com", status="active")
        UserRole.objects.create(
            user=user2,
            role=self.moderator,
            effective_from=timezone.now().date(),
        )
        conflict = RoleMutualExclusion.check_conflict(user2, self.item_writer)
        self.assertIsNotNone(
            conflict, "check_conflict should detect conflict in reverse direction"
        )

    def test_revoked_role_not_counted_as_conflict(self):
        """A revoked item_writer assignment should not block moderator."""
        user3 = UserProfile.objects.create(email="user3@example.com", status="active")
        UserRole.objects.create(
            user=user3,
            role=self.item_writer,
            effective_from=timezone.now().date(),
            revoked_at=timezone.now(),
        )
        conflict = RoleMutualExclusion.check_conflict(user3, self.moderator)
        self.assertIsNone(conflict, "Revoked role should not count as a conflict")


# ── API Enforcement ───────────────────────────────────────────────────


class RoleAssignMutualExclusionAPITests(TestCase):
    """Integration tests — POST /admin/rbac/users/{id}/roles/ mutual-exclusion gating."""

    def setUp(self):
        self.client = APIClient()
        self.admin_user, self.admin_token = _make_admin()
        self.item_writer = _get_or_create_role("item_writer")
        self.moderator = _get_or_create_role("moderator")
        self.examiner = _get_or_create_role("examiner")

        # Create target user and assign item_writer
        self.target = UserProfile.objects.create(
            email="target@example.com", status="active"
        )
        UserRole.objects.create(
            user=self.target,
            role=self.item_writer,
            effective_from=timezone.now().date(),
        )
        # Ensure exclusion rule exists
        a, b = sorted([self.item_writer, self.moderator], key=lambda r: r.name)
        RoleMutualExclusion.objects.get_or_create(
            role_a=a,
            role_b=b,
            defaults={"reason": "Conflict of interest"},
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.admin_token}")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_mutual_exclusion_blocks_assignment(self):
        """Assigning moderator to an item_writer should return 409."""
        response = self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "assign", "role": "moderator"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        data = response.json()
        self.assertEqual(data["errorCode"], "ROLE_CONFLICT")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_mutual_exclusion_allows_valid_assignment(self):
        """Assigning examiner to an item_writer should succeed (no exclusion rule)."""
        response = self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "assign", "role": "examiner"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            UserRole.objects.filter(
                user=self.target, role=self.examiner, revoked_at__isnull=True
            ).exists()
        )

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_role_already_assigned_returns_409(self):
        """Assigning item_writer again (already held) should return 409."""
        response = self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "assign", "role": "item_writer"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json()["errorCode"], "ROLE_ALREADY_ASSIGNED")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_revoke_role_success(self):
        """Revoking item_writer should mark the UserRole as revoked."""
        response = self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "revoke", "role": "item_writer", "reason": "Test revocation"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ur = UserRole.objects.get(user=self.target, role=self.item_writer)
        self.assertIsNotNone(ur.revoked_at)
        self.assertEqual(ur.revoke_reason, "Test revocation")
        self.assertTrue(
            RoleChangeEvent.objects.filter(
                user=self.target, role=self.item_writer, change_type="revoke"
            ).exists()
        )

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_revoke_role_not_held_returns_404(self):
        """Revoking a role the user doesn't hold should return 404."""
        response = self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "revoke", "role": "examiner"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ── Two-Administrator Approval ───────────────────────────────────────


class TwoAdminApprovalAPITests(TestCase):
    """Integration tests for the two-admin approval workflow."""

    def setUp(self):
        self.client = APIClient()
        self.admin1, self.token1 = _make_admin("admin1@example.com")
        self.admin2, self.token2 = _make_admin("admin2@example.com")
        self.target = UserProfile.objects.create(
            email="target@example.com", status="active"
        )
        # Ensure high-privilege role exists
        self.dg_role = _get_or_create_role("director_general")

    def _auth1(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token1}")

    def _auth2(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token2}")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_high_privilege_role_returns_202(self):
        """Assigning director_general should return 202 and create a pending approval."""
        self._auth1()
        response = self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "assign", "role": "director_general", "reason": "Appointed"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        data = response.json()
        self.assertIn("approval_id", data["data"])

        approval = RoleAssignmentApproval.objects.get(pk=data["data"]["approval_id"])
        self.assertEqual(approval.status, "pending")
        self.assertEqual(approval.requested_by, self.admin1)
        # UserRole must NOT be created yet
        self.assertFalse(
            UserRole.objects.filter(
                user=self.target, role=self.dg_role, revoked_at__isnull=True
            ).exists()
        )

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_second_admin_can_approve(self):
        """A different admin can approve the pending request, creating the UserRole."""
        self._auth1()
        r = self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "assign", "role": "director_general"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        approval_id = r.json()["data"]["approval_id"]

        # Admin2 approves
        self._auth2()
        r2 = self.client.post(
            f"/api/v1/admin/rbac/approvals/{approval_id}/approve/",
            {"note": "Verified appointment."},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r2.json()["data"]["status"], "approved")

        # UserRole must now exist
        self.assertTrue(
            UserRole.objects.filter(
                user=self.target, role=self.dg_role, revoked_at__isnull=True
            ).exists()
        )
        # RoleChangeEvent must exist
        self.assertTrue(
            RoleChangeEvent.objects.filter(
                user=self.target, role=self.dg_role, change_type="assign"
            ).exists()
        )

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_same_admin_cannot_approve_own_request(self):
        """The requesting admin must NOT be able to approve their own request."""
        self._auth1()
        r = self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "assign", "role": "director_general"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        approval_id = r.json()["data"]["approval_id"]

        # Admin1 tries to approve their own request
        r2 = self.client.post(
            f"/api/v1/admin/rbac/approvals/{approval_id}/approve/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r2.status_code, status.HTTP_403_FORBIDDEN)
        # Approval still pending
        approval = RoleAssignmentApproval.objects.get(pk=approval_id)
        self.assertEqual(approval.status, "pending")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_second_admin_can_reject(self):
        """Admin2 can reject a pending approval request."""
        self._auth1()
        r = self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "assign", "role": "director_general"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        approval_id = r.json()["data"]["approval_id"]

        self._auth2()
        r2 = self.client.post(
            f"/api/v1/admin/rbac/approvals/{approval_id}/reject/",
            {"note": "Not appropriate at this time."},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r2.json()["data"]["status"], "rejected")
        # No UserRole created
        self.assertFalse(
            UserRole.objects.filter(
                user=self.target, role=self.dg_role, revoked_at__isnull=True
            ).exists()
        )

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_list_approvals_returns_pending(self):
        """GET /api/v1/admin/rbac/approvals/?status=pending returns the pending record."""
        self._auth1()
        self.client.post(
            f"/api/v1/admin/rbac/users/{self.target.id}/roles/",
            {"action": "assign", "role": "director_general"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        r = self.client.get("/api/v1/admin/rbac/approvals/", {"status": "pending"})
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["data"]["count"], 1)


# ── Mutual-exclusion admin CRUD ───────────────────────────────────


class MutualExclusionAdminAPITests(TestCase):
    """Tests for the exclusion rule CRUD API."""

    def setUp(self):
        self.client = APIClient()
        self.admin, self.token = _make_admin()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.examiner = _get_or_create_role("examiner")
        self.candidate = _get_or_create_role("candidate", is_internal=False)

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_list_exclusions(self):
        r = self.client.get("/api/v1/admin/rbac/exclusions/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        # At least the 5 seeded pairs from migration 0010
        self.assertGreaterEqual(r.json()["data"]["count"], 5)

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_create_exclusion(self):
        r = self.client.post(
            "/api/v1/admin/rbac/exclusions/",
            {"role_a": "examiner", "role_b": "candidate", "reason": "Test rule"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            RoleMutualExclusion.objects.filter(
                role_a__name__in=["candidate", "examiner"],
                role_b__name__in=["candidate", "examiner"],
            ).exists()
        )

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_create_exclusion_self_conflict_rejected(self):
        r = self.client.post(
            "/api/v1/admin/rbac/exclusions/",
            {"role_a": "examiner", "role_b": "examiner"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_delete_exclusion(self):
        a, b = sorted([self.examiner, self.candidate], key=lambda r: r.name)
        excl = RoleMutualExclusion.objects.create(
            role_a=a, role_b=b, reason="Temp rule"
        )
        r = self.client.delete(
            f"/api/v1/admin/rbac/exclusions/{excl.id}/",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertFalse(RoleMutualExclusion.objects.filter(pk=excl.id).exists())


# ── Role version increment ───────────────────────────────────────────


class RoleVersionIncrementTests(TestCase):
    """Verify that role.version increments each time the permission matrix changes."""

    def setUp(self):
        self.client = APIClient()
        self.admin, self.token = _make_admin()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
        # Use a custom isolated role so no seeded grants exist on it.
        # is_custom=True means it can be updated via the RBAC admin API.
        self.role, _ = Role.objects.get_or_create(
            name="test_version_role",
            defaults={"is_active": True, "is_custom": True, "is_internal": True},
        )
        self.perm, _ = Permission.objects.get_or_create(
            codename="marking:score",
            defaults={"description": "Score scripts"},
        )

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_version_increments_on_permission_change(self):
        initial_version = self.role.version or 1
        # PUT the permission set to add marking:score
        r = self.client.put(
            f"/api/v1/admin/rbac/roles/{self.role.id}/permissions/",
            {"codenames": ["marking:score"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.role.refresh_from_db()
        self.assertEqual(self.role.version, initial_version + 1)
        self.assertIn("version", r.json()["data"])

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_version_does_not_increment_when_no_change(self):
        """PUT with identical set should not bump version."""
        from apps.users.models import RolePermission

        RolePermission.objects.get_or_create(role=self.role, permission=self.perm)
        initial_version = self.role.version or 1

        r = self.client.put(
            f"/api/v1/admin/rbac/roles/{self.role.id}/permissions/",
            {"codenames": ["marking:score"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.role.refresh_from_db()
        self.assertEqual(
            self.role.version,
            initial_version,
            "Version should not change when set is unchanged",
        )


# ── Full permission catalog ──────────────────────────────────────────


class PermissionCatalogTests(TestCase):
    """Verify the seeded codename count meets the ≥25 codename requirement."""

    def test_permission_catalog_count(self):
        count = Permission.objects.count()
        self.assertGreaterEqual(
            count,
            25,
            f"Expected ≥ 25 codenames seeded, found {count}. "
            "Run migration 0011_full_permission_catalog to fix.",
        )

    def test_critical_codenames_present(self):
        critical = {
            "users:manage",
            "users:import",
            "item:write",
            "item:approve",
            "results:publish",
            "candidate:register",
            "candidate:verify_identity",
            "proctoring:remote",
            "proctoring:review_flags",
            "centre:checkin",
            "helpdesk:support",
            "dg:overview",
            "marking:score",
            "marking:moderate",
            "committee:approve",
        }
        existing = set(Permission.objects.values_list("codename", flat=True))
        missing = critical - existing
        self.assertFalse(missing, f"Missing critical codenames: {sorted(missing)}")


# ── Expiry task ───────────────────────────────────────────────────────────────


class ApprovalExpiryTaskTests(TestCase):
    """Unit test for the Celery expiry task."""

    def test_expire_pending_role_approvals(self):
        from apps.users.tasks import expire_pending_role_approvals

        dg = _get_or_create_role("director_general")
        target = UserProfile.objects.create(email="exp@example.com", status="active")
        admin, _ = _make_admin("expadmin@example.com")

        past = timezone.now() - timezone.timedelta(hours=49)
        future = timezone.now() + timezone.timedelta(hours=47)

        stale = RoleAssignmentApproval.objects.create(
            target_user=target,
            role=dg,
            effective_from=timezone.now().date(),
            requested_by=admin,
            expires_at=past,
        )
        live = RoleAssignmentApproval.objects.create(
            target_user=target,
            role=dg,
            effective_from=timezone.now().date(),
            requested_by=admin,
            expires_at=future,
        )

        result = expire_pending_role_approvals()
        self.assertEqual(result["expired"], 1)

        stale.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(stale.status, "expired")
        self.assertEqual(live.status, "pending")


# ── RoleSerializer exposes is_internal + version ─────────────────────────────


class RoleSerializerFieldsTests(TestCase):
    """Verify RoleSerializer now includes is_internal and version."""

    def setUp(self):
        self.client = APIClient()
        self.admin, self.token = _make_admin()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_role_list_includes_is_internal_and_version(self):
        r = self.client.get("/api/v1/admin/rbac/roles/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        roles = r.json()["data"]["roles"]
        self.assertTrue(len(roles) > 0)
        first_role = roles[0]
        self.assertIn("is_internal", first_role)
        self.assertIn("version", first_role)
