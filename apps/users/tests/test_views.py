import jwt
import uuid
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.users.models import UserProfile, Role, UserRole, RoleChangeEvent
from apps.audit.models import AuditEvent, OutboxEvent


class UserAPIViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        
        # Ensure standard roles exist in DB (typically seeded, but get_or_create to be robust)
        self.admin_role, _ = Role.objects.get_or_create(
            name="system_administrator",
            defaults={"description": "System Admin", "is_active": True, "is_custom": False, "is_internal": True}
        )
        self.examiner_role, _ = Role.objects.get_or_create(
            name="examiner",
            defaults={"description": "Examiner role", "is_active": True, "is_custom": False, "is_internal": True}
        )
        
        # Create an admin user locally
        self.admin_sub = uuid.uuid4()
        self.admin_user = UserProfile.objects.create(
            keycloak_sub=self.admin_sub,
            email="admin@example.com",
            first_name="Admin",
            last_name="User",
            status="active"
        )
        UserRole.objects.create(
            user=self.admin_user,
            role=self.admin_role,
            effective_from=timezone.now().date()
        )

        # Generate HS256 token for admin (using super_admin realm role for wildcard permission)
        self.admin_token = jwt.encode(
            {
                "sub": str(self.admin_sub),
                "email": "admin@example.com",
                "realm_access": {
                    "roles": ["super_admin", "system_administrator"]
                }
            },
            settings.JWT_SECRET_KEY,
            algorithm="HS256"
        )
        
        # Non-admin user token
        self.examiner_sub = uuid.uuid4()
        self.examiner_user = UserProfile.objects.create(
            keycloak_sub=self.examiner_sub,
            email="examiner@example.com",
            first_name="Exam",
            last_name="Iner",
            status="active"
        )
        UserRole.objects.create(
            user=self.examiner_user,
            role=self.examiner_role,
            effective_from=timezone.now().date()
        )
        self.examiner_token = jwt.encode(
            {
                "sub": str(self.examiner_sub),
                "email": "examiner@example.com",
                "resource_access": {
                    "nbes-api": {
                        "roles": ["examiner"]
                    }
                }
            },
            settings.JWT_SECRET_KEY,
            algorithm="HS256"
        )

    def _set_auth(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_list_users_unauthorized(self):
        # Without credentials
        response = self.client.get("/api/v1/admin/rbac/users/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_list_users_forbidden(self):
        # Standard examiner tries to list users
        self._set_auth(self.examiner_token)
        response = self.client.get("/api/v1/admin/rbac/users/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_list_users_success(self):
        self._set_auth(self.admin_token)
        response = self.client.get("/api/v1/admin/rbac/users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify envelopes and pagination
        self.assertIn("data", response.data)
        self.assertEqual(len(response.data["data"]), 2)

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_list_users_filtering_and_search(self):
        self._set_auth(self.admin_token)
        
        # Search by first name
        response = self.client.get("/api/v1/admin/rbac/users/", {"search": "Admin"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["email"], "admin@example.com")

        # Filter by status
        response = self.client.get("/api/v1/admin/rbac/users/", {"status": "active"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 2)

        # Filter by role
        response = self.client.get("/api/v1/admin/rbac/users/", {"role": "examiner"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["email"], "examiner@example.com")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_create_user_success(self):
        self._set_auth(self.admin_token)
        payload = {
            "first_name": "New",
            "last_name": "User",
            "email": "newuser@example.com",
            "roles": ["examiner"],
            "effective_date": str(timezone.now().date()),
            "metadata": {"department": "Law"}
        }
        response = self.client.post(
            "/api/v1/admin/rbac/users/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Verify response structure
        self.assertTrue(response.data["success"])
        user_data = response.data["data"]
        self.assertEqual(user_data["email"], "newuser@example.com")
        self.assertEqual(user_data["status"], "pending_invite")
        self.assertEqual(user_data["metadata"]["department"], "Law")
        
        # Verify DB records
        new_profile = UserProfile.objects.get(email="newuser@example.com")
        self.assertEqual(new_profile.first_name, "New")
        self.assertEqual(new_profile.last_name, "User")
        self.assertEqual(new_profile.status, "pending_invite")
        
        # Verify Role mapping
        self.assertTrue(UserRole.objects.filter(user=new_profile, role=self.examiner_role).exists())
        self.assertTrue(RoleChangeEvent.objects.filter(user=new_profile, role=self.examiner_role, change_type="assign").exists())

        # Verify Audit event
        self.assertTrue(AuditEvent.objects.filter(action="USER_CREATED", entity_id=new_profile.id).exists())

        # Verify Outbox event
        self.assertTrue(OutboxEvent.objects.filter(event_name="UserCreated", payload__email="newuser@example.com").exists())

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_create_user_duplicate_email(self):
        self._set_auth(self.admin_token)
        payload = {
            "first_name": "Duplicate",
            "last_name": "Email",
            "email": "examiner@example.com", # already exists
            "roles": ["examiner"]
        }
        response = self.client.post(
            "/api/v1/admin/rbac/users/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
        )
        data = response.json() if hasattr(response, 'json') else response.data
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(data["errorCode"], "VALIDATION_ERROR")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_retrieve_user_success(self):
        self._set_auth(self.admin_token)
        response = self.client.get(f"/api/v1/admin/rbac/users/{self.examiner_user.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["email"], "examiner@example.com")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_retrieve_user_not_found(self):
        self._set_auth(self.admin_token)
        fake_uuid = uuid.uuid4()
        response = self.client.get(f"/api/v1/admin/rbac/users/{fake_uuid}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_patch_user_success(self):
        self._set_auth(self.admin_token)
        payload = {
            "first_name": "UpdatedName",
            "metadata": {"specialization": "Torts"}
        }
        response = self.client.patch(
            f"/api/v1/admin/rbac/users/{self.examiner_user.id}/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify update
        self.examiner_user.refresh_from_db()
        self.assertEqual(self.examiner_user.first_name, "UpdatedName")
        self.assertEqual(self.examiner_user.metadata["specialization"], "Torts")

        # Verify Audit event
        self.assertTrue(AuditEvent.objects.filter(action="USER_UPDATED", entity_id=self.examiner_user.id).exists())

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_deactivate_user_success(self):
        self._set_auth(self.admin_token)
        payload = {
            "status": "inactive"
        }
        response = self.client.patch(
            f"/api/v1/admin/rbac/users/{self.examiner_user.id}/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        self.examiner_user.refresh_from_db()
        self.assertEqual(self.examiner_user.status, "inactive")
        self.assertIsNotNone(self.examiner_user.deactivated_at)

        # Verify Audit event
        self.assertTrue(AuditEvent.objects.filter(action="USER_UPDATED", entity_id=self.examiner_user.id).exists())

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_my_profile_endpoint(self):
        self._set_auth(self.examiner_token)
        response = self.client.get("/api/v1/me/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        data = response.data["data"]
        self.assertEqual(data["email"], "examiner@example.com")
        self.assertEqual(data["first_name"], "Exam")
        self.assertEqual(data["last_name"], "Iner")
        self.assertEqual(len(data["roles"]), 1)
        self.assertEqual(data["roles"][0]["role_name"], "examiner")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_auto_profile_mirroring_and_sync(self):
        # Authenticate as a user who doesn't exist locally at all
        new_sub = uuid.uuid4()
        new_token = jwt.encode(
            {
                "sub": str(new_sub),
                "email": "stranger@example.com",
                "given_name": "Stranger",
                "family_name": "User",
                "resource_access": {
                    "nbes-api": {
                        "roles": ["examiner"]
                    }
                }
            },
            settings.JWT_SECRET_KEY,
            algorithm="HS256"
        )
        
        self._set_auth(new_token)
        response = self.client.get("/api/v1/me/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Check that user profile was automatically mirrored in DB
        created_user = UserProfile.objects.filter(keycloak_sub=new_sub).first()
        self.assertIsNotNone(created_user)
        self.assertEqual(created_user.email, "stranger@example.com")
        self.assertEqual(created_user.first_name, "Stranger")
        self.assertEqual(created_user.last_name, "User")
        self.assertEqual(created_user.status, "active")
        
        # Verify that their active role assignments synced correctly
        active_roles = UserRole.objects.filter(user=created_user, revoked_at__isnull=True)
        self.assertEqual(len(active_roles), 1)
        self.assertEqual(active_roles[0].role.name, "examiner")
        
        # Verify that an AUTO_PROFILE_CREATED audit event was recorded
        self.assertTrue(AuditEvent.objects.filter(action="AUTO_PROFILE_CREATED", entity_id=str(created_user.id)).exists())
