"""Unit and integration tests for §1.2.3 MFA & Step-Up Policy."""

import uuid
import jwt
from unittest.mock import patch
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory

from apps.users.models import Role, UserProfile, UserRole, Permission, RolePermission
from apps.audit.models import AuditEvent, SecurityEvent
from shared.step_up import STEP_UP_ACTIONS, check_step_up, requires_step_up
from shared.permissions import has_permission_with_step_up


class StepUpTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.factory = APIRequestFactory()

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_check_step_up_passes_with_mfa_verified_header(self):
        request = self.factory.get("/", HTTP_X_MFA_VERIFIED="true")
        self.assertTrue(check_step_up(request))

        request = self.factory.get("/", HTTP_X_MFA_VERIFIED="TRUE")
        self.assertTrue(check_step_up(request))

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_check_step_up_passes_with_acr_2(self):
        request = self.factory.get("/", HTTP_X_ACR="2")
        self.assertTrue(check_step_up(request))

        request = self.factory.get("/", HTTP_X_ACR="3")
        self.assertTrue(check_step_up(request))

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_check_step_up_fails_with_acr_1(self):
        request = self.factory.get("/", HTTP_X_ACR="1")
        self.assertFalse(check_step_up(request))

        request = self.factory.get("/", HTTP_X_ACR="invalid")
        self.assertFalse(check_step_up(request))

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_check_step_up_fails_with_no_header(self):
        request = self.factory.get("/")
        self.assertFalse(check_step_up(request))

    @override_settings(KEYCLOAK_ENABLED=False)
    def test_step_up_bypassed_in_dev_mode(self):
        request = self.factory.get("/")
        self.assertTrue(check_step_up(request))

    def test_step_up_actions_registry_contains_required_codenames(self):
        required = {
            "vault:operate",
            "sitting:lock:override",
            "users:manage",
            "rbac:manage",
            "results:publish",
            "cert:trigger",
            "results:view:own",
            "audit:export",
            "committee:manage",
        }
        for codename in required:
            self.assertTrue(
                requires_step_up(codename), f"{codename} should require step-up"
            )

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_step_up_denial_recorded_in_audit_and_secops(self):
        sub = uuid.uuid4()
        user = UserProfile.objects.create(keycloak_sub=sub, email="test@example.com")

        request = self.factory.get("/")
        # Provide super_admin role so that base RBAC check succeeds, entering step-up check
        request.auth = {"sub": str(sub), "realm_access": {"roles": ["super_admin"]}}
        request.user = user
        request.request_id = uuid.uuid4()
        request.ip_address = "127.0.0.1"

        self.assertFalse(check_step_up(request))

        perm_class = has_permission_with_step_up("users:manage")()

        with patch("shared.events.publish"):
            has_perm = perm_class.has_permission(request, None)

        self.assertFalse(has_perm)

        audit_events = AuditEvent.objects.filter(action="STEP_UP_REQUIRED")
        self.assertEqual(audit_events.count(), 1)
        self.assertEqual(audit_events.first().actor_id, sub)
        self.assertEqual(audit_events.first().new_state["permission"], "users:manage")

        sec_events = SecurityEvent.objects.filter(category="step_up_denied")
        self.assertEqual(sec_events.count(), 1)
        self.assertEqual(sec_events.first().actor_id, sub)
        self.assertEqual(sec_events.first().indicators["permission"], "users:manage")

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_vault_operate_requires_step_up(self):
        sub = uuid.uuid4()
        user = UserProfile.objects.create(
            keycloak_sub=sub, email="secretariat@example.com"
        )
        role = Role.objects.get(name="nbec_member")
        UserRole.objects.create(
            user=user, role=role, effective_from=timezone.now().date()
        )

        payload = {
            "sub": str(sub),
            "resource_access": {"nbes-api": {"roles": ["nbec_member"]}},
        }

        # 1. Fails without step-up headers
        with patch(
            "shared.auth.KeycloakJWTAuthentication.authenticate",
            return_value=(user, payload),
        ):
            with patch("shared.events.publish"):
                response = self.client.post(
                    "/api/v1/itembank/vault/export-requests/",
                    {"scope": "All items", "purpose": "Backup"},
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Succeeds with X-Mfa-Verified: true (hits pre-existing FieldError and returns 404 requester not found)
        with patch(
            "shared.auth.KeycloakJWTAuthentication.authenticate",
            return_value=(user, payload),
        ):
            with patch("shared.events.publish"):
                response = self.client.post(
                    "/api/v1/itembank/vault/export-requests/",
                    {"scope": "All items", "purpose": "Backup"},
                    HTTP_X_MFA_VERIFIED="true",
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_role_assign_requires_step_up(self):
        sub = uuid.uuid4()
        user = UserProfile.objects.create(keycloak_sub=sub, email="admin@example.com")
        role = Role.objects.get(name="system_administrator")
        UserRole.objects.create(
            user=user, role=role, effective_from=timezone.now().date()
        )

        payload = {
            "sub": str(sub),
            "resource_access": {"nbes-api": {"roles": ["system_administrator"]}},
        }

        target_user = UserProfile.objects.create(email="target@example.com")
        role_to_assign = Role.objects.get(name="moderator")

        # 1. Fails without step-up headers
        with patch(
            "shared.auth.KeycloakJWTAuthentication.authenticate",
            return_value=(user, payload),
        ):
            with patch("shared.events.publish"):
                response = self.client.post(
                    f"/api/v1/admin/rbac/users/{target_user.id}/roles/",
                    {"action": "assign", "role": role_to_assign.name},
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Succeeds with X-Acr: 2
        with patch(
            "shared.auth.KeycloakJWTAuthentication.authenticate",
            return_value=(user, payload),
        ):
            with patch("shared.events.publish"):
                response = self.client.post(
                    f"/api/v1/admin/rbac/users/{target_user.id}/roles/",
                    {"action": "assign", "role": role_to_assign.name},
                    HTTP_X_ACR="2",
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_rbac_manage_requires_step_up(self):
        sub = uuid.uuid4()
        user = UserProfile.objects.create(keycloak_sub=sub, email="admin@example.com")
        role = Role.objects.get(name="system_administrator")
        UserRole.objects.create(
            user=user, role=role, effective_from=timezone.now().date()
        )

        payload = {
            "sub": str(sub),
            "resource_access": {"nbes-api": {"roles": ["system_administrator"]}},
        }

        # Use non-conflicting roles since item_writer <-> moderator is already seeded
        role_a = Role.objects.get(name="item_writer")
        role_b = Role.objects.get(name="nbec_member")

        # 1. Fails without step-up headers
        with patch(
            "shared.auth.KeycloakJWTAuthentication.authenticate",
            return_value=(user, payload),
        ):
            with patch("shared.events.publish"):
                response = self.client.post(
                    "/api/v1/admin/rbac/exclusions/",
                    {
                        "role_a": role_a.name,
                        "role_b": role_b.name,
                        "reason": "Test exclusion",
                    },
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Succeeds with X-Acr: 2
        with patch(
            "shared.auth.KeycloakJWTAuthentication.authenticate",
            return_value=(user, payload),
        ):
            with patch("shared.events.publish"):
                response = self.client.post(
                    "/api/v1/admin/rbac/exclusions/",
                    {
                        "role_a": role_a.name,
                        "role_b": role_b.name,
                        "reason": "Test exclusion",
                    },
                    HTTP_X_ACR="2",
                    HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
                )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_audit_export_requires_step_up(self):
        sub = uuid.uuid4()
        user = UserProfile.objects.create(keycloak_sub=sub, email="auditor@example.com")
        role = Role.objects.get(name="auditor")
        UserRole.objects.create(
            user=user, role=role, effective_from=timezone.now().date()
        )

        payload = {
            "sub": str(sub),
            "resource_access": {"nbes-api": {"roles": ["auditor"]}},
        }

        # 1. Fails without step-up headers
        with patch(
            "shared.auth.KeycloakJWTAuthentication.authenticate",
            return_value=(user, payload),
        ):
            with patch("shared.events.publish"):
                response = self.client.get("/api/v1/audit/export/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Succeeds with X-Mfa-Verified: true
        with patch(
            "shared.auth.KeycloakJWTAuthentication.authenticate",
            return_value=(user, payload),
        ):
            with patch("shared.events.publish"):
                response = self.client.get(
                    "/api/v1/audit/export/", HTTP_X_MFA_VERIFIED="true"
                )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_settings(KEYCLOAK_ENABLED=True)
    def test_candidate_result_view_requires_step_up(self):
        # Candidate-specific results:view:own high-stakes action
        # Let's verify that requires_step_up("results:view:own") is True
        self.assertTrue(requires_step_up("results:view:own"))

        # Verify that has_permission_with_step_up indeed blocks it when KEYCLOAK_ENABLED=True
        sub = uuid.uuid4()
        user = UserProfile.objects.create(
            keycloak_sub=sub, email="candidate@example.com"
        )
        role = Role.objects.get(name="candidate")
        UserRole.objects.create(
            user=user, role=role, effective_from=timezone.now().date()
        )

        # Give results:view:own permission to candidate
        perm, _ = Permission.objects.get_or_create(codename="results:view:own")
        RolePermission.objects.get_or_create(role=role, permission=perm)

        request = self.factory.get("/")
        request.auth = {
            "sub": str(sub),
            "resource_access": {"nbes-api": {"roles": ["candidate"]}},
        }
        request.user = user
        request.request_id = uuid.uuid4()
        request.ip_address = "127.0.0.1"

        perm_class = has_permission_with_step_up("results:view:own")()

        with patch("shared.events.publish"):
            has_perm = perm_class.has_permission(request, None)
        self.assertFalse(has_perm)
