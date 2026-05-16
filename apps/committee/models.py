"""apps/committee/models.py — NBEC Committee domain models."""
import uuid
from django.db import models
from django.utils import timezone


class NBECMember(models.Model):
    """NBEC member register. Keycloak sub links to identity."""
    class Role(models.TextChoices):
        CHAIR = "chair", "Chair"
        MEMBER = "member", "Member"
        SECRETARY = "secretary", "Secretary"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keycloak_sub = models.UUIDField(unique=True)
    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    appointment_date = models.DateField()
    tenure_end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_voting_member = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "committee_nbecmember"

    def __str__(self):
        return f"{self.full_name} ({self.get_role_display()})"


class Meeting(models.Model):
    """NBEC meeting with quorum enforcement."""
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CONVENED = "convened", "Convened"
        ADJOURNED = "adjourned", "Adjourned"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=50, unique=True)
    scheduled_date = models.DateTimeField()
    venue = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    quorum_required = models.PositiveSmallIntegerField(default=5)
    attendees = models.JSONField(default=list)  # list of keycloak_sub UUIDs
    convened_at = models.DateTimeField(null=True, blank=True)
    adjourned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "committee_meeting"

    def __str__(self):
        return f"Meeting {self.reference} — {self.get_status_display()}"

    @property
    def quorum_met(self) -> bool:
        return len(self.attendees) >= self.quorum_required


class Minutes(models.Model):
    """Meeting minutes — immutable once Chair approves."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.OneToOneField(Meeting, on_delete=models.PROTECT, related_name="minutes")
    content = models.TextField()
    approved = models.BooleanField(default=False)
    approved_by_id = models.UUIDField(null=True, blank=True)  # Chair keycloak_sub
    approved_at = models.DateTimeField(null=True, blank=True)
    document_ref = models.TextField(blank=True)  # MinIO object key
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "committee_minutes"
        verbose_name_plural = "minutes"

    def approve(self, chair_id: str):
        """Approve minutes — immutable from this point."""
        if self.approved:
            raise ValueError("Minutes are already approved and cannot be changed.")
        self.approved = True
        self.approved_by_id = chair_id
        self.approved_at = timezone.now()
        self.save()


class ConflictDeclaration(models.Model):
    """Conflict-of-interest declaration — auto-excludes member from affected decisions."""
    class Status(models.TextChoices):
        PENDING = "pending", "Pending Review"
        APPROVED = "approved", "Approved"
        DISMISSED = "dismissed", "Dismissed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    member = models.ForeignKey(NBECMember, on_delete=models.PROTECT, related_name="conflicts")
    subject_description = models.TextField()
    # Link to affected entity (item, application, etc.) — generic UUID reference
    affected_entity_type = models.CharField(max_length=100, blank=True)
    affected_entity_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    declared_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "committee_conflictdeclaration"

    def __str__(self):
        return f"Conflict: {self.member} — {self.affected_entity_type}"


class ActionItem(models.Model):
    """Action item from a meeting."""
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETE = "complete", "Complete"
        OVERDUE = "overdue", "Overdue"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(Meeting, on_delete=models.PROTECT, related_name="action_items")
    description = models.TextField()
    assigned_to_id = models.UUIDField()  # keycloak_sub
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "committee_actionitem"
