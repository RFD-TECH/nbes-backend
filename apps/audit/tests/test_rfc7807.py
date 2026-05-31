from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.views import APIView
from django.urls import path

from rest_framework.permissions import AllowAny
from shared.exceptions import error_response


class MockErrorView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        # Trigger an exception that will be caught by nbes_exception_handler
        raise PermissionError("Access denied to resource.")

    def post(self, request):
        # Trigger a manual error response
        return error_response(
            "Manual error trigger", code="MANUAL_ERROR", status_code=400
        )


urlpatterns = [
    path("test-err/", MockErrorView.as_view()),
]


@override_settings(ROOT_URLCONF=__name__, KEYCLOAK_ENABLED=False)
class RFC7807ErrorTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_uncaught_exception_format(self):
        response = self.client.get("/test-err/")
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(
            response.headers.get("Content-Type"), "application/problem+json"
        )

        data = response.json()
        self.assertIn("type", data)
        self.assertIn("title", data)
        self.assertEqual(data["status"], 500)
        self.assertIn("detail", data)
        self.assertIn("errorCode", data)
        self.assertIn("timestamp", data)
        self.assertIn("instance", data)

        # Ensure no legacy/hybrid keys are present
        self.assertNotIn("success", data)
        self.assertNotIn("error", data)
        self.assertNotIn("meta", data)

    def test_manual_error_response_format(self):
        response = self.client.post("/test-err/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.headers.get("Content-Type"), "application/problem+json"
        )

        data = response.json()
        self.assertEqual(data["errorCode"], "MANUAL_ERROR")
        self.assertEqual(data["status"], 400)
        self.assertEqual(data["detail"], "Manual error trigger")

        # Ensure no legacy/hybrid keys are present
        self.assertNotIn("success", data)
        self.assertNotIn("error", data)
        self.assertNotIn("meta", data)
