"""apps/committee/models.py — NBEC Committee domain models.

Field names follow the SRS Phase 2 data model (§2.5.1) verbatim:
``nbec_member (id, full_name, designation, instrument_ref unique,
              tenure_start, tenure_end, status, contact, photo_ref)``

Identity (the underlying user account, password, MFA, invite email,
role grants in Keycloak) belongs to IAM. NBES only stores the
NBEC-specific domain record and links to the IAM identity via
``keycloak_sub``. NBES never creates or directly mutates Keycloak users.
"""
import uuid
from django.db import models
from django.utils import timezone


class NBECMember(models.Model):
    """NBEC member register. ``keycloak_sub`` links to the IAM identity."""

    class Designation(models.TextChoices):
        # Per SRS §2.2.1: designations are exactly Chair, Deputy Chair, Member.
        # NBEC Secretariat is a Phase 1 platform role, NOT a member designation.
        CHAIR = "chair", "Chair"
        DEPUTY_CHAIR = "deputy_chair", "Deputy Chair"
        MEMBER = "member", "Member"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        RENEWED = "renewed", "Renewed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keycloak_sub = models.UUIDField(unique=True)
    full_name = models.CharField(max_length=255)
    title = models.CharField(max_length=50, blank=True)            # SRS §2.2.1 "title and post-nominals"
    post_nominals = models.CharField(max_length=100, blank=True)   # SRS §2.2.1 "title and post-nominals"
    contact = models.EmailField()                                  # SRS §2.5.1 "contact"
    designation = models.CharField(
        max_length=20, choices=Designation.choices, default=Designation.MEMBER
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    # Appointing instrument reference — letter/gazette ref from appointing authority
    instrument_ref = models.CharField(max_length=100, unique=True, null=True, blank=True)
    tenure_start = models.DateField()
    tenure_end = models.DateField(null=True, blank=True)
    photo_ref = models.TextField(blank=True)   # MinIO object key
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "committee_nbecmember"
        constraints = [
            # At most one active Chair at a time (SRS §2.7 / §2.2.1).
            models.UniqueConstraint(
                fields=["designation"],
                condition=models.Q(designation="chair", status="active"),
                name="unique_active_chair",
            ),
            # SRS §2.7: "Tenure end > tenure start". Enforced at the DB so
            # services that bypass full_clean() (e.g. bulk operations, raw
            # ORM .create() in tests) can't persist invalid ranges.
            models.CheckConstraint(
                condition=models.Q(tenure_end__isnull=True)
                | models.Q(tenure_end__gt=models.F("tenure_start")),
                name="tenure_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.get_designation_display()})"

    @property
    def is_active(self) -> bool:
        """Derived from status; SRS data model exposes ``status`` only."""
        return self.status == self.Status.ACTIVE

    def activate(self):
        if self.status not in (self.Status.DRAFT, self.Status.RENEWED):
            raise ValueError(f"Cannot activate member in status '{self.status}'.")
        self.status = self.Status.ACTIVE
        self.save(update_fields=["status", "updated_at"])

    def expire(self):
        self.status = self.Status.EXPIRED
        self.save(update_fields=["status", "updated_at"])


class Meeting(models.Model):
    """NBEC meeting — lifecycle from Draft through to Minuted."""

    class MeetingType(models.TextChoices):
        ORDINARY = "ordinary", "Ordinary"
        EXTRAORDINARY = "extraordinary", "Extraordinary"
        CLOSED = "closed", "Closed"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        AGENDA_ISSUED = "agenda_issued", "Agenda Issued"
        SCHEDULED = "scheduled", "Scheduled"   # kept for backward compat
        CONVENED = "convened", "Convened"
        ADJOURNED = "adjourned", "Adjourned"
        MINUTED = "minuted", "Minuted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=50, unique=True)
    meeting_type = models.CharField(
        max_length=20, choices=MeetingType.choices, default=MeetingType.ORDINARY
    )
    scheduled_date = models.DateTimeField()
    venue = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    quorum_required = models.PositiveSmallIntegerField(default=5)
    attendees = models.JSONField(default=list)   # list of keycloak_sub UUID strings
    # Presiding chair and recording secretariat (keycloak subs)
    chair_id = models.UUIDField(null=True, blank=True)
    secretariat_id = models.UUIDField(null=True, blank=True)
    convened_at = models.DateTimeField(null=True, blank=True)
    adjourned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "committee_meeting"

    def __str__(self):
        return f"Meeting {self.reference} — {self.get_status_display()}"

    @property
    def quorum_met(self) -> bool:
        return len(set(self.attendees)) >= self.quorum_required


class Agenda(models.Model):
    """Versioned agenda for a meeting. Each published agenda increments version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(Meeting, on_delete=models.PROTECT, related_name="agendas")
    version = models.PositiveSmallIntegerField(default=1)
    # [{order, title, description, presenter_id, duration_minutes}]
    items = models.JSONField(default=list)
    document_ref = models.TextField(blank=True)  # MinIO object key for PDF
    published_at = models.DateTimeField(null=True, blank=True)
    created_by_id = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "committee_agenda"
        unique_together = [("meeting", "version")]
        ordering = ["meeting", "-version"]

    def __str__(self):
        return f"Agenda v{self.version} — {self.meeting.reference}"


class Minutes(models.Model):
    """Meeting minutes — immutable once the Chair digitally signs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.OneToOneField(Meeting, on_delete=models.PROTECT, related_name="minutes")
    content = models.TextField()
    approved = models.BooleanField(default=False)
    approved_by_id = models.UUIDField(null=True, blank=True)   # Chair keycloak_sub
    approved_at = models.DateTimeField(null=True, blank=True)
    document_ref = models.TextField(blank=True)   # MinIO object key (draft PDF)
    # Set when Chair signs; row becomes immutable from this point
    immutable_at = models.DateTimeField(null=True, blank=True)
    signature_ref = models.CharField(max_length=200, blank=True)  # signature artefact ref
    # System 05 archive reference assigned after archival
    archive_ref = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "committee_minutes"
        verbose_name_plural = "minutes"

    def sign(self, chair_id: str, signature_ref: str = ""):
        """Sign and immutably seal the minutes."""
        if self.approved:
            raise ValueError("Minutes are already signed and cannot be changed.")
        self.approved = True
        self.approved_by_id = chair_id
        self.approved_at = timezone.now()
        self.immutable_at = timezone.now()
        self.signature_ref = signature_ref
        self.save(update_fields=["approved", "approved_by_id", "approved_at",
                                 "immutable_at", "signature_ref", "updated_at"])


class MinutesAddendum(models.Model):
    """Addendum to signed minutes — issued by Chair when correction needed post-sign."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    minutes = models.ForeignKey(Minutes, on_delete=models.PROTECT, related_name="addenda")
    content = models.TextField()
    issued_by_id = models.UUIDField()    # Chair keycloak_sub
    issued_at = models.DateTimeField(default=timezone.now)
    document_ref = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "committee_minutesaddendum"
        ordering = ["minutes", "issued_at"]

    def __str__(self):
        return f"Addendum to {self.minutes.meeting.reference} at {self.issued_at:%Y-%m-%d}"


class ConflictDeclaration(models.Model):
    """COI declaration — auto-excludes member from affected decisions."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending Review"
        APPROVED = "approved", "Approved"
        DISMISSED = "dismissed", "Dismissed"

    class SubjectType(models.TextChoices):
        CANDIDATE = "candidate", "Candidate"
        ITEM_WRITER = "item_writer", "Item Writer"
        EXAMINER = "examiner", "Examiner"
        SUPPLIER = "supplier", "Supplier"
        OTHER = "other", "Other"

    class Nature(models.TextChoices):
        FINANCIAL = "financial", "Financial"
        PERSONAL = "personal", "Personal"
        PROFESSIONAL = "professional", "Professional"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    member = models.ForeignKey(NBECMember, on_delete=models.PROTECT, related_name="conflicts")
    subject_type = models.CharField(
        max_length=20, choices=SubjectType.choices, default=SubjectType.OTHER
    )
    subject_description = models.TextField()
    nature = models.CharField(max_length=20, choices=Nature.choices, blank=True)
    # Generic reference to the affected entity (item, application, etc.)
    affected_entity_type = models.CharField(max_length=100, blank=True)
    affected_entity_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    effective_from = models.DateField(null=True, blank=True)
    review_date = models.DateField(null=True, blank=True)
    declared_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "committee_conflictdeclaration"

    def __str__(self):
        return f"Conflict: {self.member} — {self.affected_entity_type}"


class ActionItem(models.Model):
    """Action item arising from a meeting, recorded in the minutes."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETE = "complete", "Complete"
        VERIFIED = "verified", "Verified"
        OVERDUE = "overdue", "Overdue"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(Meeting, on_delete=models.PROTECT, related_name="action_items")
    # Minutes FK — set once minutes are created for this meeting
    minutes = models.ForeignKey(
        Minutes, on_delete=models.SET_NULL, null=True, blank=True, related_name="action_items"
    )
    description = models.TextField()
    assigned_to_id = models.UUIDField()     # keycloak_sub of assignee
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_escalated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "committee_actionitem"

    def __str__(self):
        return f"Action [{self.status}]: {self.description[:60]}"
