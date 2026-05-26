import uuid
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from django.utils import timezone

from apps.audit.models import OutboxEvent
from shared.middleware import AuditMiddleware
from shared.events import publish, set_request_id, set_trace_context
from apps.audit.tasks import _publish_via_system_17


class W3CTraceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = AuditMiddleware(lambda r: None)

    def test_parse_incoming_traceparent(self):
        # A valid traceparent header: version-trace_id-parent_id-trace_flags
        trace_id_hex = "4bf92f3577b34da6a3ce929d0e0e4736"
        parent_id_hex = "00f067aa0ba902b7"
        traceparent = f"00-{trace_id_hex}-{parent_id_hex}-01"
        tracestate = "rojo=1,congo=2"

        request = self.factory.get(
            "/", HTTP_TRACEPARENT=traceparent, HTTP_TRACESTATE=tracestate
        )
        self.middleware.process_request(request)

        # Trace ID must be parsed as a UUID and set as request.request_id
        expected_uuid = uuid.UUID(hex=trace_id_hex)
        self.assertEqual(request.request_id, expected_uuid)
        self.assertEqual(request.traceparent, traceparent)
        self.assertEqual(request.tracestate, tracestate)

        # Check response headers propagation
        response = {}
        self.middleware.process_response(request, response)
        self.assertEqual(response.get("traceparent"), traceparent)
        self.assertEqual(response.get("tracestate"), tracestate)

    def test_generate_missing_traceparent(self):
        request = self.factory.get("/")
        self.middleware.process_request(request)

        # Verify a new request_id is generated and traceparent is formatted
        self.assertIsNotNone(request.request_id)
        self.assertTrue(request.traceparent.startswith("00-"))
        self.assertEqual(len(request.traceparent.split("-")), 4)
        self.assertEqual(request.traceparent.split("-")[1], request.request_id.hex)

    def test_outbox_event_captures_trace_context(self):
        # Setup context in thread-local storage
        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        tracestate = "rojo=1"
        request_id = uuid.uuid4()

        set_request_id(request_id)
        set_trace_context(traceparent, tracestate)

        # Publish a mock event
        publish("TestEvent", {"foo": "bar"}, topic="nbes.test")

        # Verify that the saved OutboxEvent contains the trace context
        event = OutboxEvent.objects.get(event_name="TestEvent")
        self.assertEqual(event.request_id, request_id)
        self.assertEqual(event.traceparent, traceparent)
        self.assertEqual(event.tracestate, tracestate)

    @patch("apps.audit.tasks.call_system_17")
    def test_poller_forwards_trace_headers_to_system_17(self, mock_call):
        mock_call.return_value.ok = True

        event = OutboxEvent.objects.create(
            event_name="TestEvent",
            topic="nbes.test",
            payload={"foo": "bar"},
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            tracestate="rojo=1",
            request_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            created_at=timezone.now(),
        )

        _publish_via_system_17(event)

        # call_system_17 should have been called with traceparent and tracestate
        mock_call.assert_called_once()
        kwargs = mock_call.call_args.kwargs
        self.assertEqual(kwargs.get("traceparent"), event.traceparent)
        self.assertEqual(kwargs.get("tracestate"), event.tracestate)
