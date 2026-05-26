import uuid
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from apps.users.models import UserProfile, Role, UserRole, RoleChangeEvent, Permission, RolePermission


class UserProfileModelTests(TestCase):
    def setUp(self):
        self.creator = UserProfile.objects.create(
            email="admin@example.com",
            first_name="Admin",
            last_name="User",
            status="active"
        )
        self.role, _ = Role.objects.get_or_create(
            name="examiner",
            defaults={
                "description": "Examiner role",
                "is_active": True,
                "is_custom": False,
                "is_internal": True
            }
        )

    def test_user_profile_creation(self):
        user = UserProfile.objects.create(
            keycloak_sub=uuid.uuid4(),
            email="user@example.com",
            first_name="John",
            last_name="Doe",
            status="pending_invite",
            metadata={"national_id": "GHA-12345"},
            created_by=self.creator
        )
        self.assertEqual(user.email, "user@example.com")
        self.assertEqual(user.first_name, "John")
        self.assertEqual(user.last_name, "Doe")
        self.assertEqual(user.status, "pending_invite")
        self.assertEqual(user.metadata.get("national_id"), "GHA-12345")
        self.assertEqual(user.created_by, self.creator)
        self.assertTrue(user.is_authenticated)
        self.assertFalse(user.is_anonymous)
        self.assertIn("user@example.com", str(user))

    def test_user_role_assignment(self):
        user = UserProfile.objects.create(
            email="examiner@example.com",
            status="active"
        )
        user_role = UserRole.objects.create(
            user=user,
            role=self.role,
            effective_from=timezone.now().date(),
            assigned_by=self.creator
        )
        self.assertEqual(user_role.user, user)
        self.assertEqual(user_role.role, self.role)
        self.assertEqual(user_role.assigned_by, self.creator)
        self.assertIsNone(user_role.revoked_at)

    def test_user_role_unique_constraint(self):
        user = UserProfile.objects.create(
            email="examiner2@example.com",
            status="active"
        )
        UserRole.objects.create(
            user=user,
            role=self.role,
            effective_from=timezone.now().date()
        )
        # Attempting to assign the same role again should raise IntegrityError
        with self.assertRaises(IntegrityError):
            UserRole.objects.create(
                user=user,
                role=self.role,
                effective_from=timezone.now().date()
            )

    def test_user_role_allow_reassign_after_revoke(self):
        user = UserProfile.objects.create(
            email="examiner3@example.com",
            status="active"
        )
        ur1 = UserRole.objects.create(
            user=user,
            role=self.role,
            effective_from=timezone.now().date(),
            revoked_at=timezone.now(),
            revoke_reason="Revoked temporarily"
        )
        # Should succeed because the first assignment is revoked (revoked_at is not null)
        ur2 = UserRole.objects.create(
            user=user,
            role=self.role,
            effective_from=timezone.now().date()
        )
        self.assertIsNotNone(ur2.id)

    def test_role_change_event_log(self):
        user = UserProfile.objects.create(
            email="examiner4@example.com",
            status="active"
        )
        event = RoleChangeEvent.objects.create(
            user=user,
            role=self.role,
            change_type="assign",
            actor=self.creator,
            reason="Assigned via test case"
        )
        self.assertEqual(event.user, user)
        self.assertEqual(event.role, self.role)
        self.assertEqual(event.change_type, "assign")
        self.assertEqual(event.actor, self.creator)
        self.assertEqual(event.reason, "Assigned via test case")
        self.assertIn("assign", str(event))
