"""apps/notifications/models.py — Notification orchestration hub."""
import uuid
from django.db import models


class NotificationTemplate(models.Model):
    """Reusable notification template per event type."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_name = models.CharField(max_length=100, unique=True)
    subject = models.CharField(max_length=255)
    body_template = models.TextField(help_text="Django template syntax. Context vars depend on event.")
    channel = models.CharField(
        max_length=20,
        choices=[("email", "Email"), ("sms", "SMS"), ("both", "Email + SMS")],
        default="email"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notifications_notificationtemplate"

    def __str__(self):
        return f"Template: {self.event_name}"


class Notification(models.Model):
    """One notification record per recipient per event."""
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        NotificationTemplate, on_delete=models.SET_NULL, null=True, related_name="notifications"
    )
    recipient_id = models.UUIDField()             # keycloak_sub
    recipient_email = models.EmailField(blank=True)
    recipient_phone = models.CharField(max_length=20, blank=True)
    event_name = models.CharField(max_length=100)
    context = models.JSONField(default=dict)      # template rendering context
    rendered_subject = models.CharField(max_length=255, blank=True)
    rendered_body = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    retry_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notifications_notification"
        indexes = [models.Index(fields=["status", "created_at"])]


class DeliveryLog(models.Model):
    """System 21 delivery attempt log per notification."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="delivery_logs"
    )
    attempt = models.PositiveSmallIntegerField()
    system_21_ref = models.CharField(max_length=100, blank=True)
    success = models.BooleanField()
    error_message = models.TextField(blank=True)
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications_deliverylog"
        ordering = ["attempt"]
