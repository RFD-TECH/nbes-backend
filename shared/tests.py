from unittest.mock import patch

import jwt
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed

from shared.auth import KeycloakJWTAuthentication
from shared.middleware import EdgeRateLimitMiddleware, IdempotencyKeyMiddleware


class AuthFailureRecordingTests(SimpleTestCase):
    @override_settings(KEYCLOAK_ENABLED=True, JWT_SECRET_KEY="test-secret")
    def test_hs256_in_keycloak_mode_records_security_event_once(self):
        token = jwt.encode({"sub": "user-1"}, "test-secret", algorithm="HS256")
        request = RequestFactory().get(
            "/api/v1/audit/search",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        with patch("shared.secops.record_security_event") as record:
            with self.assertRaises(AuthenticationFailed):
                KeycloakJWTAuthentication().authenticate(request)

        self.assertEqual(record.call_count, 1)
        self.assertEqual(record.call_args.kwargs["category"], "auth_token_invalid")


class IdempotencyKeyMiddlewareTests(SimpleTestCase):
    def test_cache_key_is_scoped_by_authorization_header(self):
        request_factory = RequestFactory()
        request_a = request_factory.post(
            "/api/v1/example",
            HTTP_AUTHORIZATION="Bearer token-a",
            HTTP_IDEMPOTENCY_KEY="same-key",
        )
        request_b = request_factory.post(
            "/api/v1/example",
            HTTP_AUTHORIZATION="Bearer token-b",
            HTTP_IDEMPOTENCY_KEY="same-key",
        )

        key_a = IdempotencyKeyMiddleware._build_cache_key(request_a, "same-key")
        key_b = IdempotencyKeyMiddleware._build_cache_key(request_b, "same-key")

        self.assertNotEqual(key_a, key_b)

    def test_cache_key_is_scoped_by_path(self):
        request_factory = RequestFactory()
        request_a = request_factory.post(
            "/api/v1/one",
            HTTP_AUTHORIZATION="Bearer token",
            HTTP_IDEMPOTENCY_KEY="same-key",
        )
        request_b = request_factory.post(
            "/api/v1/two",
            HTTP_AUTHORIZATION="Bearer token",
            HTTP_IDEMPOTENCY_KEY="same-key",
        )

        key_a = IdempotencyKeyMiddleware._build_cache_key(request_a, "same-key")
        key_b = IdempotencyKeyMiddleware._build_cache_key(request_b, "same-key")

        self.assertNotEqual(key_a, key_b)


class EdgeRateLimitMiddlewareTests(SimpleTestCase):
    def tearDown(self):
        cache.clear()

    @override_settings(EDGE_BLOCK_THRESHOLD_24H=1000)
    def test_active_throttle_still_counts_toward_24h_block(self):
        middleware = EdgeRateLimitMiddleware(lambda request: None)
        ip = "203.0.113.10"
        cache.set(middleware._throttle_key(ip), True, timeout=900)
        cache.set(middleware._counter_key(ip, "block"), 999, timeout=86400)

        request = RequestFactory().get("/api/v1/protected", REMOTE_ADDR=ip)
        with patch("shared.secops.record_security_event"):
            response = middleware(request)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(cache.get(middleware._counter_key(ip, "block")), 1000)
        self.assertTrue(cache.get(middleware._block_key(ip)))
