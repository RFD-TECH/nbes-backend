"""apps/registration/models.py — Candidate Registration with NLEMS gate and FSM."""
import uuid
from django.db import models
from django_fsm import FSMField, transition
from shared.validators import validate_index_number, validate_ghana_phone
from workflow.guards import nlems_eligibility_verified


class Candidate(models.Model):
    """
    NBES candidate. Linked to Keycloak via keycloak_sub.
    Reference: NBES Architecture §7.1 — candidates schema
    """
    class EligibilityStatus(models.TextChoices):
        PENDING = "pending", "Pending Verification"
        ELIGIBLE = "eligible", "Eligible"
        BLOCKED = "blocked", "Blocked — Ineligible"
        ELIGIBLE_OVERRIDE = "eligible_override", "Eligible (DG Override)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keycloak_sub = models.UUIDField(unique=True, db_index=True)
    index_number = models.CharField(
        max_length=15, unique=True, null=True, blank=True,
        validators=[validate_index_number],
        help_text="Assigned on confirmed registration. Format: BAR-YYYY-CCCCC"
    )
    surname = models.CharField(max_length=150)
    given_names = models.CharField(max_length=150)
    date_of_birth = models.DateField()
    national_id = models.CharField(max_length=30, unique=True)
    llb_id = models.CharField(max_length=50)
    lpt_cert_number = models.CharField(max_length=50)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=20, validators=[validate_ghana_phone])
    photograph_ref = models.TextField(blank=True)   # MinIO object key
    eligibility_status = models.CharField(
        max_length=30, choices=EligibilityStatus.choices, default=EligibilityStatus.PENDING
    )
    disability_codes = models.JSONField(default=list)  # NLEMS-aligned accommodation flags
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "registration_candidate"

    def __str__(self):
        return f"{self.surname}, {self.given_names} ({self.index_number or 'unassigned'})"

    @property
    def full_name(self):
        return f"{self.given_names} {self.surname}"


class Registration(models.Model):
    """
    Candidate registration for a specific sitting.
    FSM enforces the eligibility → payment → confirmed flow.
    Reference: NBES Architecture §3.3 — Candidate Registration Workflow States
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(Candidate, on_delete=models.PROTECT, related_name="registrations")
    sitting_ref = models.CharField(max_length=15)
    status = FSMField(default="draft", protected=True)
    payment_confirmed = models.BooleanField(default=False)
    payment_reference = models.CharField(max_length=100, blank=True)
    block_reason = models.TextField(blank=True)
    withdrawal_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "registration_registration"
        unique_together = [("candidate", "sitting_ref")]

    def __str__(self):
        return f"{self.candidate} — {self.sitting_ref} [{self.status}]"

    # ── FSM Transitions ────────────────────────────────────────────────────────

    @transition(field=status, source="draft", target="pending_eligibility")
    def submit(self):
        """Candidate submits. Triggers async NLEMS eligibility check."""
        from apps.registration.tasks import check_eligibility_async
        check_eligibility_async.delay(str(self.id))

    @transition(field=status, source="pending_eligibility", target="pending_payment",
                conditions=[nlems_eligibility_verified])
    def mark_eligible(self):
        """NLEMS confirmed eligible. Awaiting fee payment via System 20."""
        from shared.events import publish
        publish("EligibilityVerified", {"registration_id": str(self.id)})

    @transition(field=status, source="pending_eligibility", target="blocked")
    def block(self, reason: str = ""):
        """NLEMS returned ineligible."""
        self.block_reason = reason
        from shared.events import publish
        publish("EligibilityBlocked", {"registration_id": str(self.id), "reason": reason})

    @transition(field=status, source="pending_payment", target="registered",
                conditions=[payment_confirmed])
    def confirm_registration(self):
        """System 20 webhook confirmed fee payment."""
        from apps.registration.services import generate_index_number, generate_slip
        generate_index_number(self)
        generate_slip(self)
        from shared.events import publish
        publish("CandidateRegistered", {"registration_id": str(self.id)})

    @transition(field=status, source="registered", target="withdrawn")
    def withdraw(self, reason: str = ""):
        """Candidate withdraws before T-21 cut-off."""
        self.withdrawal_reason = reason
        from shared.events import publish
        publish("RegistrationWithdrawn", {"registration_id": str(self.id)})


class EligibilityCheck(models.Model):
    """Log of each NLEMS eligibility check attempt."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    registration = models.ForeignKey(Registration, on_delete=models.CASCADE, related_name="eligibility_checks")
    outcome = models.CharField(max_length=30, blank=True)
    nlems_response = models.JSONField(null=True, blank=True)
    checked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "registration_eligibilitycheck"


class RegistrationSlip(models.Model):
    """Registration slip PDF + QR code for System 10B check-in."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    registration = models.OneToOneField(Registration, on_delete=models.CASCADE, related_name="slip")
    slip_ref = models.CharField(max_length=50, unique=True)
    document_ref = models.TextField(blank=True)   # MinIO path of signed PDF
    qr_data = models.TextField(blank=True)         # QR code payload
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "registration_registrationslip"
