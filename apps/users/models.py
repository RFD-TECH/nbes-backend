"""Local profile + NBES RBAC catalog.

Identity belongs to IAM/Keycloak; NBES only mirrors the minimum.
Permissions belong to NBES: codenames are domain concepts (e.g. ``item:approve``)
that mean nothing outside this service, so the catalog lives here.

Three RBAC tables:

* ``Permission`` — the catalog of codenames NBES enforces. Codenames are
  declared in code (``HasPermission("...")`` in views) and seeded via
  migration; admins do not invent new codenames at runtime because nothing
  would enforce them.
* ``Role`` — local registry of role names NBES recognises. Mirrors the
  NBES-scoped role names IAM has created in the Keycloak realm (one row per
  role-name that NBES knows about). A JWT can carry roles NBES does not
  recognise — those are ignored.
* ``RolePermission`` — the editable matrix. A system_administrator can grant
  or revoke a codename on a role without a redeploy. This is the bit
  REQ-F000-02 calls "configurable".
"""
import uuid
from django.db import models, transaction


class UserProfile(models.Model):
    """Thin local profile. Keycloak owns authentication; never store secrets."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keycloak_sub = models.UUIDField(unique=True, db_index=True, null=True, blank=True,
        help_text="IAM subject identifier. Null until IAM provisioning completes.")
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=[
        ("pending_invite", "Pending Invite"),   # Created locally, IAM invite pending
        ("active", "Active"),                    # IAM account active, profile complete
        ("inactive", "Inactive"),                # Deactivated by admin
    ], default="pending_invite")
    metadata = models.JSONField(default=dict, blank=True,
        help_text="Extensible fields: national_id, department, phone, etc.")
    created_by = models.ForeignKey('self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='created_profiles')
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users_userprofile"
        verbose_name = "User Profile"

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def __str__(self):
        return f"{self.email} (status={self.status})"


class Permission(models.Model):
    """A permission codename NBES enforces. Seeded; not user-created."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    codename = models.CharField(max_length=100, unique=True, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_permission"
        ordering = ["codename"]

    def __str__(self):
        return self.codename


class Role(models.Model):
    """NBES-scoped role name. Mirrored from IAM's UserSystemAssignment role_type
    so NBES can ignore JWT roles it does not recognise as its own."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    is_custom = models.BooleanField(default=True, db_index=True)
    is_internal = models.BooleanField(default=True,
        help_text="Internal roles require MFA. Candidate is the only external role.")
    version = models.PositiveIntegerField(default=1,
        help_text="Incremented on permission matrix changes for this role.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users_role"
        ordering = ["name"]

    def __str__(self):
        return self.name


class RolePermission(models.Model):
    """Editable role → permission grant. The matrix REQ-F000-02 says is
    configurable. Edits propagate within 60s via the rbac cache."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="grants")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="grants")
    granted_by = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_rolepermission"
        unique_together = ("role", "permission")
        ordering = ["role__name", "permission__codename"]

    def __str__(self):
        return f"{self.role.name}:{self.permission.codename}"


class UserRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='user_roles')
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='user_roles')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True,
        help_text="Null = open-ended assignment")
    assigned_by = models.ForeignKey(UserProfile, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='role_assignments_made')
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_userrole"
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'role'],
                condition=models.Q(revoked_at__isnull=True),
                name='unique_active_user_role',
            )
        ]

    def __str__(self):
        return f"{self.user.email}:{self.role.name}"


class RoleChangeEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='role_events')
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    change_type = models.CharField(max_length=10, choices=[
        ("assign", "Assigned"),
        ("revoke", "Revoked"),
    ])
    actor = models.ForeignKey(UserProfile, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='role_changes_made')
    reason = models.TextField(blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_rolechangeevent"
        indexes = [
            models.Index(fields=['user', 'occurred_at']),
            models.Index(fields=['role', 'occurred_at']),
        ]

    def __str__(self):
        return f"{self.user.email}:{self.change_type}:{self.role.name}"


# ── Role Mutual-Exclusion Rules ─────────────────────────────────────

#: Roles that can never coexist on the same profile (from SRS §1.2.2).
#: This set is also used by the admin assignment API to gate high-privilege
#: roles through the two-administrator approval workflow.
#: Note: All "nbec_member" instances are treated as high-privilege (not just the Chair)
#: because all NBEC members have access to draft exams, grade boundaries, and ratification votes.
HIGH_PRIVILEGE_ROLES: frozenset[str] = frozenset({
    "director_general",
    "system_administrator",
    "nbec_member",
})


class RoleMutualExclusion(models.Model):
    """Declares two roles that cannot be held simultaneously by one user.

    Pairs are stored unordered — the canonical form enforces
    ``role_a.name < role_b.name`` so a (A, B) pair and (B, A) pair cannot
    both exist. The assignment service checks this table before committing
    any new UserRole record.

    Blueprint §1.2.2:
      "Mutually exclusive roles cannot coexist: e.g., Item Writer and
      Moderator on the same profile; Invigilator and Candidate at all times."
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role_a = models.ForeignKey(
        Role, on_delete=models.CASCADE, related_name='exclusions_as_a',
        help_text="The role whose name sorts first alphabetically in the pair."
    )
    role_b = models.ForeignKey(
        Role, on_delete=models.CASCADE, related_name='exclusions_as_b',
        help_text="The role whose name sorts second alphabetically in the pair."
    )
    reason = models.CharField(max_length=255, blank=True,
        help_text="Human-readable explanation for the exclusion rule.")
    created_by = models.UUIDField(null=True, blank=True,
        help_text="Actor UUID who defined this exclusion.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_rolemutualexclusion"
        unique_together = [("role_a", "role_b")]
        ordering = ["role_a__name", "role_b__name"]

    def __str__(self):
        return f"{self.role_a.name} ↔ {self.role_b.name}"

    @classmethod
    def check_conflict(
        cls, user: "UserProfile", incoming_role: Role
    ) -> "RoleMutualExclusion | None":
        """Return the first exclusion rule that blocks assigning *incoming_role*
        to *user*, or ``None`` if no conflict exists.

        Checks both (incoming_role, existing_role) and (existing_role, incoming_role)
        orderings so callers don't have to.
        """
        active_role_ids = (
            user.user_roles.filter(revoked_at__isnull=True)
            .values_list("role_id", flat=True)
        )
        from django.db.models import Q
        return cls.objects.filter(
            Q(role_a=incoming_role, role_b_id__in=active_role_ids)
            | Q(role_b=incoming_role, role_a_id__in=active_role_ids)
        ).select_related("role_a", "role_b").first()


# ── Two-Administrator Approval  ──────────────────────────────────────

class RoleAssignmentApproval(models.Model):
    """Pending two-administrator approval for high-privilege role assignments.

    When Administrator A tries to assign a role in ``HIGH_PRIVILEGE_ROLES``, a
    ``RoleAssignmentApproval`` record is created (status=``pending``) and the
    API returns HTTP 202. A different Administrator B then hits the
    ``/approve`` or ``/reject`` endpoint. On approval the ``UserRole`` record
    is created atomically inside ``RoleAssignmentApproval.do_approve()``.

    The record expires 48 hours after creation. A Celery periodic task marks
    stale pending records as ``expired``.

    Blueprint §1.2.2:
      "High-privilege local role assignments (NBEC Chair, Administrator)
      require two-Administrator approval (enforced in the admin API)."
    """
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_EXPIRED, "Expired"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target_user = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE,
        related_name='pending_role_approvals',
    )
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    requested_by = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL, null=True,
        related_name='role_approval_requests',
    )
    reviewed_by = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='role_approval_reviews',
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
    )
    reason = models.TextField(blank=True,
        help_text="Reason provided by the requesting administrator.")
    review_note = models.TextField(blank=True,
        help_text="Note added by the reviewing administrator.")
    expires_at = models.DateTimeField(
        help_text="48 hours after creation. Celery marks expired records automatically.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users_roleassignmentapproval"
        indexes = [
            models.Index(fields=['status', 'expires_at']),
            models.Index(fields=['target_user', 'status']),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"Approval({self.status}): "
            f"{self.target_user.email} → {self.role.name}"
        )

    def do_approve(self, reviewer: "UserProfile", note: str = "") -> "UserRole":
        """Atomically approve this request and create the UserRole.

        Raises ``ValueError`` if the reviewer is the same as the requester,
        or if the record is not in ``pending`` status.
        """
        if self.status != self.STATUS_PENDING:
            raise ValueError(f"Cannot approve a {self.status} request.")
        if reviewer.pk == self.requested_by_id:
            raise ValueError("The approving administrator must differ from the requester.")
        from django.utils import timezone as _tz
        if self.expires_at and self.expires_at < _tz.now():
            raise ValueError("Cannot approve an expired request.")

        with transaction.atomic():
            user_role = UserRole.objects.create(
                user=self.target_user,
                role=self.role,
                effective_from=self.effective_from,
                effective_to=self.effective_to,
                assigned_by=reviewer,
            )
            RoleChangeEvent.objects.create(
                user=self.target_user,
                role=self.role,
                change_type="assign",
                actor=reviewer,
                reason=f"Two-admin approval by {reviewer.email}. Note: {note}".strip(". "),
            )
            self.status = self.STATUS_APPROVED
            self.reviewed_by = reviewer
            self.review_note = note
            self.save(update_fields=["status", "reviewed_by", "review_note", "updated_at"])

        return user_role

    def do_reject(self, reviewer: "UserProfile", note: str = "") -> None:
        """Reject this request. Same reviewer constraints apply."""
        if self.status != self.STATUS_PENDING:
            raise ValueError(f"Cannot reject a {self.status} request.")
        if reviewer.pk == self.requested_by_id:
            raise ValueError("The reviewing administrator must differ from the requester.")

        self.status = self.STATUS_REJECTED
        self.reviewed_by = reviewer
        self.review_note = note
        self.save(update_fields=["status", "reviewed_by", "review_note", "updated_at"])


# ── Bulk Import Record  ───────────────────────────────────────────

class BulkImportRecord(models.Model):
    """Tracks each bulk user import job for audit and 7-year retention.

    Blueprint §1.2.4:
      "File hash recorded with the bulk import audit entry; original file
      retained for 7 years. Partial-success: valid rows processed; invalid
      rows reported with row-level errors."
    """
    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    uploaded_by = models.ForeignKey(
        UserProfile, on_delete=models.SET_NULL, null=True, related_name="bulk_imports"
    )
    original_filename = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64, help_text="SHA-256 hex of the uploaded file.")
    file_path = models.CharField(max_length=500, blank=True,
        help_text="Storage path for the original file (retained 7 years).")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    total_rows = models.PositiveIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    failure_count = models.PositiveIntegerField(default=0)
    row_errors = models.JSONField(default=list,
        help_text="List of {row, email, errors} for each failed row.")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "users_bulkimportrecord"
        ordering = ["-created_at"]

    def __str__(self):
        return f"BulkImport({self.original_filename}, {self.status})"
