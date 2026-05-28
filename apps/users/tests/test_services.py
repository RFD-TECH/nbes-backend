from django.test import TestCase, override_settings
from shared import keycloak_admin
import uuid


class KeycloakAdminTests(TestCase):
    @override_settings(KEYCLOAK_ENABLED=False)
    def test_create_user_dev_mode(self):
        """In dev mode, create_user should not call HTTP endpoints and should return a stub UUID."""
        sub = keycloak_admin.create_user(
            email="test-dev@example.com",
            first_name="Test",
            last_name="Dev",
            roles=["examiner"],
            send_invite=True
        )
        self.assertIsNotNone(sub)
        # Should be a valid UUID string
        parsed_uuid = uuid.UUID(sub)
        self.assertEqual(str(parsed_uuid), sub)

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_deactivate_user_dev_mode(self):
        """In dev mode, deactivate_user should be a no-op."""
        user_sub = str(uuid.uuid4())
        # Should not raise any exception
        keycloak_admin.deactivate_user(user_sub)

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_assign_client_role_dev_mode(self):
        """In dev mode, assign_client_role should be a no-op."""
        user_sub = str(uuid.uuid4())
        keycloak_admin.assign_client_role(user_sub, "examiner")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_remove_client_role_dev_mode(self):
        """In dev mode, remove_client_role should be a no-op."""
        user_sub = str(uuid.uuid4())
        keycloak_admin.remove_client_role(user_sub, "examiner")

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_bulk_create_users_dev_mode(self):
        """In dev mode, bulk_create_users should provision users with stub UUIDs."""
        users = [
            {"email": "dev1@example.com", "first_name": "Dev1", "last_name": "User", "roles": ["examiner"]},
            {"email": "dev2@example.com", "first_name": "Dev2", "last_name": "User", "roles": ["item-writer"]},
        ]
        results = keycloak_admin.bulk_create_users(users)
        self.assertEqual(len(results), 2)
        for res in results:
            self.assertIsNotNone(res["sub"])
            self.assertIsNone(res["error"])
            # verify valid UUID
            uuid.UUID(res["sub"])
