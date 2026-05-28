---
name: phase1-tasks
description: >
  Full Phase 1 sprint task breakdown — Authentication, RBAC & Foundation (SRS REQ-F000).
  IAM-Aligned Architecture: NBES delegates authentication, MFA, password policy, and session
  management to the central IAM (System 19 / Keycloak). NBES receives verified identity and
  authorisation claims via the API Gateway (System 17). This document replaces the previous
  version that assumed local authentication.
metadata:
  type: project
  version: 2.0
  updated: 2026-05-23
  architecture: iam-delegated
---

# Phase 1 — Authentication, RBAC & Foundation (IAM-Aligned)
SRS ref: REQ-F000 | 3 sprints (3 weeks)

---

## Architectural Principle

> **NBES does not own authentication.** The IAM (System 19 / Keycloak) handles login, MFA,
> password policies, session management, and credential storage. NBES trusts the signed
> headers the API Gateway (System 17) adds to every inbound request.
>
> NBES owns:
> - **Local user profile store** (first_name, last_name, status, metadata — NOT passwords)
> - **RBAC enforcement** (role → permission matrix, mutual-exclusion rules)
> - **IAM bridge** (provisioning users in IAM via admin API, syncing roles)
> - **Audit substrate** (append-only event store, daily hash chain)
> - **Step-up policy enforcement** (checking `x-acr` / `x-mfa-verified` headers from the gateway)
>
> The following are **IAM responsibilities** and are NOT implemented in NBES:
> - Login / logout / token refresh endpoints
> - Password storage, hashing, policy enforcement, HIBP checks
> - MFA enrolment (TOTP, WebAuthn, SMS)
> - Session management, refresh tokens
> - Account lockout / auto-unlock
> - Invite email dispatch (IAM sends the invite with first-time-login link)

---

## PRE-WORK: Critical Bug Fixes (All resolved)

| Bug | File | Status |
|-----|------|--------|
| BUG-01 — dev.py wiping DRF config | `config/settings/dev.py` | Fixed — REST_FRAMEWORK block removed |
| BUG-02 — SECRET_KEY can be None | `config/settings/base.py` | Fixed — uses `os.environ["SECRET_KEY"]` |
| BUG-03 — timezone.timedelta crash | `apps/cert_trigger/models.py` | Fixed — uses `datetime.timedelta` |
| BUG-04 — nlems guard checks wrong field | `workflow/guards.py` | Fixed — checks `candidate.eligibility_status` |
| BUG-05 — AuditEvent race condition | `apps/audit/models.py` | Fixed — uses `select_for_update()` in `_record_atomic()` |

---

## Sprint 1.1 — IAM Bridge & User Profile Store (Week 1)

**Goal:** An Administrator can create, edit, deactivate, and delete user profiles via the API. Each profile is provisioned in IAM (Keycloak) and stored locally with lifecycle states. Role assignment uses a proper join table with effective dates.

---

### TASK-1.1.1 — Expand UserProfile model (§1.5.1, GAP-01)
**File:** `apps/users/models.py`

**Current state:** Skeletal model with only `keycloak_sub`, `email`, `role` (CharField).

**Changes:**
```python
class UserProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keycloak_sub = models.UUIDField(unique=True, null=True, blank=True,
        help_text="IAM subject identifier. Null until IAM provisioning completes.")
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
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
```

**Remove:** The `role` CharField. Multi-role support is via the `UserRole` model (TASK-1.1.2).

**Keep:** `keycloak_sub` but make it `null=True, blank=True` — it's set when IAM provisioning completes.

**Migration:** Create a data migration that migrates existing `role` values to `UserRole` rows.

**Acceptance:** `UserProfile` fields match the IAM-aligned §1.5.1 `user` table definition. No `password_hash`, `mfa_enrolled`, `failed_login_count`, `locked_until`, or `invite_token` fields (those are IAM's responsibility).

---

### TASK-1.1.2 — Add UserRole join table + RoleChangeEvent (§1.5.1, GAP-02, GAP-10)
**File:** `apps/users/models.py`

**UserRole model:**
```python
class UserRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='user_roles')
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='user_roles')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True,
        help_text="Null = open-ended assignment")
    assigned_by = models.ForeignKey(UserProfile, null=True, on_delete=models.SET_NULL,
        related_name='role_assignments_made')
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'role'],
                condition=models.Q(revoked_at__isnull=True),
                name='unique_active_user_role',
            )
        ]
```

**RoleChangeEvent model (event sourcing — immutable log):**
```python
class RoleChangeEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='role_events')
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    change_type = models.CharField(max_length=10, choices=[
        ("assign", "Assigned"),
        ("revoke", "Revoked"),
    ])
    actor = models.ForeignKey(UserProfile, null=True, on_delete=models.SET_NULL,
        related_name='role_changes_made')
    reason = models.TextField(blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'occurred_at']),
            models.Index(fields=['role', 'occurred_at']),
        ]
```

**Acceptance:** A user can have multiple active roles. `RoleChangeEvent` is the immutable log; `UserRole` is the current-state projection. Query "what roles did user X have on date Y?" is answerable.

---

### TASK-1.1.3 — Add `is_internal` + `version` to Role model (GAP-14, GAP-11)
**File:** `apps/users/models.py`

**Add to existing `Role` model:**
```python
is_internal = models.BooleanField(default=True,
    help_text="Internal roles require MFA. Candidate is the only external role.")
version = models.PositiveIntegerField(default=1,
    help_text="Incremented on permission matrix changes for this role.")
```

**Acceptance:** `Role.objects.filter(is_internal=False)` returns only `candidate`. Version increments on permission changes (see TASK-1.2.1).

---

### TASK-1.1.4 — Expand IAM bridge: create, deactivate, assign (§1.2.1, GAP-04)
**File:** `shared/keycloak_admin.py`

**Add functions (alongside existing `revoke_realm_role`):**

```python
def create_user(email: str, first_name: str, last_name: str,
                roles: list[str], *, send_invite: bool = True) -> str:
    """Provision user in IAM. Returns the IAM user UUID (sub).

    In dev (KEYCLOAK_ENABLED=False): returns a generated UUID.
    In prod: POST to Keycloak Admin API /auth/admin/realms/{realm}/users
    with emailVerified=False, enabled=True, requiredActions=["VERIFY_EMAIL", "UPDATE_PASSWORD"].
    If send_invite=True, Keycloak sends the invite email with first-time-login link.
    Assigns NBES client roles via POST /role-mappings/clients/{client-id}.
    """

def deactivate_user(user_sub: str) -> None:
    """Disable IAM account and revoke all active sessions.

    PUT /auth/admin/realms/{realm}/users/{sub} with enabled=False.
    DELETE /auth/admin/realms/{realm}/users/{sub}/sessions (revoke all sessions).
    """

def assign_client_role(user_sub: str, role_name: str) -> None:
    """Assign an NBES client role to a user in IAM.

    POST /auth/admin/realms/{realm}/users/{sub}/role-mappings/clients/{nbes-client-id}
    """

def remove_client_role(user_sub: str, role_name: str) -> None:
    """Remove an NBES client role from a user in IAM.

    DELETE /auth/admin/realms/{realm}/users/{sub}/role-mappings/clients/{nbes-client-id}
    """

def bulk_create_users(users: list[dict]) -> list[dict]:
    """Batch provision users in IAM.

    For each user: call create_user(). Returns list of
    {"email": str, "sub": str | None, "error": str | None}.
    Uses individual calls — Keycloak has no batch endpoint.
    """
```

**Error handling:** All functions must:
1. Log the call with correlation_id
2. Retry 3x with exponential backoff on 5xx
3. Raise `IntegrationError(retryable=True/False)` on permanent failure
4. Write `AuditEvent` on success and failure

**Dev mode:** When `KEYCLOAK_ENABLED=False`, all functions log the call and return stub values. No-op in dev.

**Acceptance:** `create_user("test@gsl.edu.gh", "Kwame", "Mensah", ["examiner"])` returns a UUID. In dev, returns a generated UUID. In prod, creates the Keycloak user and returns their `sub`.

---

### TASK-1.1.5 — Admin User CRUD: Create user endpoint (§1.6, GAP-03)
**Files:** `apps/users/views.py`, `apps/users/serializers.py`, `apps/users/services.py`

**Endpoint:** `POST /api/v1/admin/users`
**Permission:** `users:manage` (system-administrator only)

**Request body:**
```json
{
  "first_name": "Kwame",
  "last_name": "Mensah",
  "email": "kmensah@gsl.edu.gh",
  "roles": ["examiner"],
  "effective_date": "2026-05-20",
  "metadata": {"national_id": "GHA-12345", "department": "Legal"}
}
```

**Service logic (`apps/users/services.py` → `create_user()`):**
1. Validate email uniqueness (case-insensitive)
2. Validate each role exists in `Role` table and is active
3. Check mutual-exclusion rules (TASK-1.2.3, can stub initially)
4. Call `keycloak_admin.create_user(email, first_name, last_name, roles)` → get `iam_sub`
5. Create `UserProfile(keycloak_sub=iam_sub, status="pending_invite", created_by=request_user)`
6. Create `UserRole` for each role with `effective_from=effective_date, assigned_by=request_user`
7. Create `RoleChangeEvent(change_type="assign")` for each role
8. Write `AuditEvent(action="USER_CREATED", entity_type="user", old_state=None, new_state={...})`
9. Publish `UserCreated` outbox event
10. Return 201 with user data

**Compensatory action:** If IAM provisioning fails after local profile creation, mark profile `status="pending_invite"` with `keycloak_sub=None` and log the failure. A retry task can re-attempt IAM provisioning.

**Acceptance (F000-01):** Admin creates Examiner → profile exists + IAM account created + invite email sent by IAM within 5 min. Duplicate email → 400.

---

### TASK-1.1.6 — Admin User CRUD: Edit/deactivate endpoint (§1.6, GAP-05)
**Files:** `apps/users/views.py`, `apps/users/serializers.py`, `apps/users/services.py`

**Endpoint:** `PATCH /api/v1/admin/users/{id}`
**Permission:** `users:manage`

**Supported operations:**
- **Edit:** PATCH with `{first_name, last_name, email, metadata}` subset
- **Deactivate:** PATCH with `{"status": "inactive"}` → calls `keycloak_admin.deactivate_user(sub)` → sets `deactivated_at=now()`, sends notification
- **Logical delete:** PATCH with `{"deleted": true}` → sets `status="inactive"`, preserves data (15-year retention)

**Constraint (GAP-06):** Cannot deactivate a user with open active assignments. For now, implement as a stub guard that checks `UserRole.objects.filter(user=user, revoked_at__isnull=True).exists()` — later phases add specific assignment checks (scripts in marking queue, etc.).

**Audit:** Every change writes `AuditEvent(action="USER_UPDATED", old_state={...before...}, new_state={...after...})`.

**Acceptance:** PATCH changes fields. Deactivation calls IAM, sets `deactivated_at`, sets status. Audit entry shows before/after.

---

### TASK-1.1.7 — Admin User CRUD: List/detail endpoints
**Files:** `apps/users/views.py`, `apps/users/serializers.py`

**Endpoints:**
- `GET /api/v1/admin/users` — Paginated list with filters (status, role, search by name/email)
- `GET /api/v1/admin/users/{id}` — Full profile with active roles and permissions

**Permission:** `users:manage`

**Filters:** `?status=active&role=examiner&search=kwame&page=1&page_size=20`

**Acceptance:** List returns paginated users. Detail returns full profile with resolved permissions.

---

### TASK-1.1.8 — Unified `/api/v1/me` endpoint (§1.6, GAP-08)
**Files:** `apps/users/views.py`, `apps/users/serializers.py`

**Endpoint:** `GET /api/v1/me`
**Permission:** `IsAuthenticated` (any authenticated user)

**Response:**
```json
{
  "success": true,
  "data": {
    "id": "...",
    "email": "...",
    "first_name": "...",
    "last_name": "...",
    "status": "active",
    "roles": [
      {"name": "examiner", "effective_from": "2026-05-20", "effective_to": null}
    ],
    "effective_permissions": ["marking:second_mark", "..."],
    "metadata": {"department": "Legal"}
  }
}
```

**`effective_permissions`** computed from active `UserRole` records → `RolePermission` codenames union.

**Acceptance:** Authenticated user sees their full profile. Permissions reflect current `UserRole` assignments, not stale JWT claims.

---

### TASK-1.1.9 — Fix `_mirror_profile` auto-create behaviour (GAP-07)
**File:** `shared/auth.py`

**Problem:** `_mirror_profile()` auto-creates `UserProfile` for ANY valid JWT. Under the IAM-aligned architecture, profiles must be created by Administrators via the admin API. An unknown `sub` should either:
- **Option A (strict):** Reject with 403 — "NBES profile not provisioned"
- **Option B (graceful):** Create a minimal profile with `status="active"` but log a warning. This supports edge cases where the IAM has the user but the admin hasn't provisioned them in NBES yet.

**Recommendation:** Option B with a warning log + AuditEvent(`action="AUTO_PROFILE_CREATED"`). Update the auto-created profile to use the JWT claims for `email` and extract `first_name`/`last_name` from the `name` claim if available.

**Also fix:** Update on every authentication to sync `email` and role claims from the JWT, not just on first creation.

**Acceptance:** Unknown `sub` → profile created with warning. Subsequent requests update email/role from JWT claims.

---

### TASK-1.1.10 — Create migrations for Sprint 1.1
**Command:** `python manage.py makemigrations users && python manage.py migrate`

**Include:**
- Data migration: migrate existing `UserProfile.role` values to `UserRole` rows
- Seed migration: ensure all 15 roles exist in `Role` table

**Acceptance:** `python manage.py showmigrations users` shows all green. `UserProfile`, `UserRole`, `RoleChangeEvent` tables created.

---

## Sprint 1.2 — Roles, Permissions & RBAC Gateway (Week 2)

**Goal:** Configurable role/permission matrix, mutual-exclusion rules, two-approver flow for high-privilege roles, step-up MFA enforcement, bulk import.

---

### TASK-1.2.1 — Role assignment and revocation endpoints (§1.6, GAP-03)
**Files:** `apps/users/views.py`, `apps/users/services.py`

**Endpoints:**
- `POST /api/v1/admin/users/{id}/roles` — body: `{"role": "examiner", "effective_from": "2026-05-20"}`
- `DELETE /api/v1/admin/users/{id}/roles/{role_name}` — body: `{"reason": "Tenure expired"}`
- `GET /api/v1/admin/users/{id}/roles` — returns active UserRole records

**Permission:** `users:manage`

**Role assignment logic (`services.py` → `assign_role()`):**
1. Validate role exists and is active
2. Check mutual-exclusion rules (TASK-1.2.3)
3. If high-privilege role → create `RoleAssignmentApproval` (TASK-1.2.4), return pending status
4. Otherwise → create `UserRole`, create `RoleChangeEvent(change_type="assign")`
5. Call `keycloak_admin.assign_client_role(user.keycloak_sub, role_name)` to sync to IAM
6. Write `AuditEvent(action="ROLE_ASSIGNED")`
7. Publish `RoleChanged` outbox event → downstream systems invalidate cached permissions within 60s

**Role revocation logic (`services.py` → `revoke_role()`):**
1. Set `UserRole.revoked_at = now()`, `revoke_reason = reason`
2. Create `RoleChangeEvent(change_type="revoke")`
3. Call `keycloak_admin.remove_client_role(user.keycloak_sub, role_name)` to sync to IAM
4. Write `AuditEvent(action="ROLE_REVOKED", old_state={"role": ...})`
5. Publish `RoleChanged` outbox event
6. Invalidate RBAC cache for the affected role (`shared.rbac.invalidate_role()`)

**Acceptance (F000-02):** Revoking a role publishes `RoleChanged` event; within 60 seconds the user gets 403 on that role's endpoints.

---

### TASK-1.2.2 — Seed full permission codename catalog (GAP-34, GAP-36)
**File:** Migration file in `apps/users/migrations/`

**Full codename catalog (25+ codenames):**
```python
PERMISSION_SEED = [
    # Users & Admin
    ("users:manage", "Manage user profiles (CRUD)"),
    ("users:import", "Bulk import users"),
    ("rbac:manage", "Manage role-permission matrix"),

    # Item Bank
    ("item:create", "Create examination items"),
    ("item:approve", "Approve/reject items"),

    # Committee
    ("committee:manage", "Manage NBEC committee operations"),

    # Sitting
    ("sitting:configure", "Configure examination sittings"),
    ("sitting:invigilate", "Invigilate at examination centres"),

    # Registration
    ("registration:self", "Self-register as candidate"),
    ("registration:eligibility:override", "Override eligibility decisions"),

    # Marking
    ("marking:second_mark", "Second-mark examination scripts"),
    ("marking:moderate", "Moderate marking decisions"),

    # Results
    ("results:ratify", "Ratify examination results"),
    ("results:publish:approve", "Approve results publication"),
    ("results:view:own", "View own examination results"),

    # Resit
    ("resit:register", "Register for resit examination"),
    ("resit:exception:grant", "Grant resit exceptions"),

    # Certificate
    ("cert:trigger", "Trigger certificate issuance"),

    # Audit & Security
    ("audit:search", "Search audit trail"),
    ("audit:verify", "Verify hash chain integrity"),
    ("audit:export", "Export audit data"),
    ("secops:view", "View security operations console"),

    # Centre Operations (System 10B)
    ("centre:manage", "Manage examination centres"),
    ("centre:invigilate", "Invigilate candidates at centres"),
    ("centre:checkin", "Check in candidates at centres"),
    ("proctoring:remote", "Remote proctoring operations"),
    ("candidate:verify_identity", "Verify candidate identity"),

    # Dashboards & Reporting
    ("dashboards:manage", "Manage dashboard configuration"),
    ("sla:view", "View SLA monitoring"),
    ("reporting:view", "View reports and analytics"),

    # Director General
    ("dg:overview", "Director-General overview access"),

    # Service Desk
    ("helpdesk:support", "Service desk support operations"),
]
```

**Role → Permission mapping (seed RolePermission rows):**

| Role | Permissions |
|------|-------------|
| `system-administrator` | `users:manage`, `users:import`, `rbac:manage`, `audit:search`, `audit:verify`, `secops:view`, `dashboards:manage` |
| `nbec-member` | `item:approve`, `sitting:configure`, `results:ratify`, `results:publish:approve`, `committee:manage`, `audit:export`, `resit:exception:grant` |
| `nbec-secretariat` | `committee:manage`, `sla:view`, `reporting:view` |
| `item-writer` | `item:create` |
| `moderator` | `item:approve`, `marking:moderate` |
| `examiner` | `marking:second_mark` |
| `candidate` | `registration:self`, `results:view:own`, `resit:register` |
| `clet-registrar` | `registration:eligibility:override`, `results:publish:approve`, `cert:trigger`, `sla:view` |
| `invigilator` | `centre:invigilate`, `sitting:invigilate`, `centre:checkin`, `candidate:verify_identity` |
| `centre-coordinator` | `centre:manage`, `centre:invigilate`, `sitting:invigilate`, `centre:checkin`, `candidate:verify_identity` |
| `remote-proctor` | `proctoring:remote` |
| `dti-operations` | `centre:manage`, `secops:view` |
| `service-desk-agent` | `helpdesk:support` |
| `auditor` | `audit:search`, `audit:verify`, `audit:export` |
| `director-general` | `dg:overview`, `audit:search`, `audit:export` |

**Acceptance:** `Permission.objects.count()` ≥ 30. `Role.objects.count()` = 15. Every role has at least 1 permission.

---

### TASK-1.2.3 — Mutual-exclusion rule enforcement (§1.2.2, GAP-09)
**Files:** `apps/users/models.py`, `apps/users/services.py`

**MutualExclusionRule model:**
```python
class MutualExclusionRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role_a = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='exclusion_rules_a')
    role_b = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='exclusion_rules_b')
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['role_a', 'role_b'], name='unique_exclusion_pair'),
        ]
```

**Seed data migration:**
```python
EXCLUSION_PAIRS = [
    ("item-writer", "moderator", "Item Writer and Moderator cannot coexist (conflict of interest)"),
    ("invigilator", "candidate", "Invigilator and Candidate are always mutually exclusive"),
    ("system-administrator", "candidate", "Admin roles cannot coexist with Candidate"),
    ("director-general", "candidate", "DG cannot coexist with Candidate"),
    ("nbec-member", "candidate", "NBEC Member cannot coexist with Candidate"),
]
```

**Validation in `services.py` → `check_mutual_exclusion(user, new_role)`:**
1. Get user's active roles (UserRole where revoked_at is null)
2. Check all MutualExclusionRule pairs
3. If conflict found: raise `ValidationError` with code `ROLE_MUTUAL_EXCLUSION`

**Called from:** `assign_role()` (TASK-1.2.1), `bulk_assign_roles()` (TASK-1.2.8)

**Acceptance:** Assigning `invigilator` to a `candidate` → 400 with `ROLE_MUTUAL_EXCLUSION`. Assigning `examiner` to an `item-writer` → 400.

---

### TASK-1.2.4 — Two-approver flow for high-privilege roles (§1.2.2, GAP-12)
**File:** `apps/users/models.py`, `apps/users/views.py`, `apps/users/services.py`

**RoleAssignmentApproval model:**
```python
class RoleAssignmentApproval(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='pending_approvals')
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    requested_by = models.ForeignKey(UserProfile, on_delete=models.CASCADE,
        related_name='role_approval_requests')
    approved_by = models.ForeignKey(UserProfile, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='role_approvals_given')
    approved_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=[
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ], default="pending")
    effective_from = models.DateField()
    reject_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

**High-privilege roles requiring two-approver:** `nbec-member` (Chair), `director-general`, `system-administrator`

**Endpoints:**
- `GET /api/v1/admin/role-approvals` — List pending approvals (permission: `rbac:manage`)
- `POST /api/v1/admin/role-approvals/{id}/approve` — Second admin approves
- `POST /api/v1/admin/role-approvals/{id}/reject` — Reject with reason

**Approval logic:**
1. `requested_by` and `approved_by` MUST be different users → else 400 `SELF_APPROVAL_NOT_PERMITTED`
2. On approval: create `UserRole` + `RoleChangeEvent`, sync to IAM, publish `RoleChanged`
3. On rejection: set status, write `AuditEvent(action="ROLE_ASSIGNMENT_REJECTED")`

**Acceptance:** High-privilege assignment pends until a second (different) administrator approves.

---

### TASK-1.2.5 — Step-up authentication enforcement (§1.2.3, GAP-15, GAP-16, GAP-17)
**Files:** `shared/permissions.py`, `shared/step_up.py` (new file)

**`shared/step_up.py` — Step-up policy configuration:**
```python
"""Step-up policy — declared in code, versioned, testable.

The API Gateway (System 17) adds x-acr or x-mfa-verified headers to requests
where the IAM has confirmed a recent MFA challenge. NBES checks these headers
for high-stakes actions.
"""

STEP_UP_POLICY_VERSION = "1.0"

# Actions requiring step-up MFA verification
STEP_UP_ACTIONS = {
    # Admin operations
    "users:manage",        # Role assignment, user creation
    "rbac:manage",         # Permission matrix changes

    # High-stakes examination operations
    "results:publish:approve",    # Results publication
    "results:ratify",             # Board ratification
    "resit:exception:grant",      # Resit exceptions

    # Security operations
    "audit:export",               # Audit data export

    # Certificate operations
    "cert:trigger",               # Certificate issuance

    # Candidate high-stakes actions (MFA optional for candidates,
    # but required for these specific actions per §1.2.3)
    "results:view:own",           # View own results
}
```

**`RequiresStepUp` DRF permission class:**
```python
class RequiresStepUp(BasePermission):
    """Checks x-acr or x-mfa-verified header from the API Gateway.

    Returns 403 with error code STEP_UP_REQUIRED if missing.
    Records SecurityEvent on failure.
    """
    def has_permission(self, request, view):
        acr = request.META.get("HTTP_X_ACR", "")
        mfa_verified = request.META.get("HTTP_X_MFA_VERIFIED", "")
        if acr or mfa_verified:
            return True
        # Record denial
        record_security_event(
            category="step_up_required",
            severity="warning",
            ip_address=getattr(request, "ip_address", None),
            actor_id=request.auth.get("sub") if request.auth else None,
            indicators={"path": request.path, "method": request.method},
        )
        return False
```

**Integration with `has_permission()` factory:**
```python
def has_permission(codename):
    """Enhanced factory that auto-adds step-up requirement for high-stakes actions."""
    class Perm(HasPermission):
        permission = codename
    if codename in STEP_UP_ACTIONS:
        return [Perm, RequiresStepUp]
    return Perm
```

**Acceptance (F000-03):** Internal user attempts high-stakes action without `x-acr` header → 403 with `STEP_UP_REQUIRED`. With header → passes through to normal RBAC check.

---

### TASK-1.2.6 — Update RBAC resolver to use UserRole table (GAP-02)
**File:** `shared/rbac.py`

**Current:** Resolves permissions by intersecting JWT `resource_access` roles with `Role` → `RolePermission`.

**Change:** Also check the `UserRole` table as the authoritative source:
1. Extract `sub` from JWT payload
2. Look up `UserRole.objects.filter(user__keycloak_sub=sub, revoked_at__isnull=True, effective_from__lte=today)`
3. If `effective_to` is set and `effective_to < today`, skip that role
4. Union permissions from both JWT claims AND `UserRole` → `RolePermission`
5. Cache result per user (not just per role) with 60s TTL

**Why both:** JWT roles may be stale (up to 8h token lifetime). `UserRole` is the source of truth for NBES-local decisions. Using both provides defence in depth: the JWT gives fast-path verification, `UserRole` is the authoritative fallback.

**Acceptance:** Revoking a `UserRole` causes 403 within 60 seconds (cache TTL), even if the JWT still carries the role.

---

### TASK-1.2.7 — Bulk user import endpoint (§1.2.4, GAP-18, GAP-19)
**Files:** `apps/users/views.py`, `apps/users/services.py`, `apps/users/serializers.py`

**Endpoint:** `POST /api/v1/admin/users/import`
**Permission:** `users:import`
**Content-Type:** `multipart/form-data`

**Service logic (`services.py` → `bulk_import_users()`):**
1. Accept CSV or XLSX file (add `openpyxl` to `requirements.txt`)
2. Parse with schema validation: columns `first_name, last_name, email, role, effective_date, national_id(optional)`
3. Validate each row: email format, email uniqueness, role exists, required fields present
4. **Partial success:** Process valid rows individually; collect errors for invalid rows
5. For each valid row: call `create_user()` service (TASK-1.1.5) which provisions in IAM
6. Compute SHA-256 hash of the uploaded file
7. Store original file (MinIO in prod, local filesystem in dev)
8. Create `BulkImportRecord` model:
   ```python
   class BulkImportRecord(models.Model):
       id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
       file_hash = models.CharField(max_length=64)
       file_ref = models.CharField(max_length=500, help_text="Object storage path")
       total_rows = models.PositiveIntegerField()
       success_count = models.PositiveIntegerField()
       error_count = models.PositiveIntegerField()
       error_report = models.JSONField(default=list)
       imported_by = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True)
       created_at = models.DateTimeField(auto_now_add=True)
   ```
9. Write `AuditEvent(action="BULK_IMPORT", new_state={"file_hash": ..., "total": N, "ok": N, "failed": N})`

**Response:**
```json
{
  "success": true,
  "data": {
    "import_id": "...",
    "created": 195,
    "failed": 5,
    "errors": [
      {"row": 3, "field": "email", "error": "Invalid email format"},
      {"row": 7, "field": "email", "error": "Email already exists"}
    ]
  }
}
```

**Acceptance (F000-04):** 200-row import with 5 invalid rows → 195 users created, 5 error rows reported. File hash in audit.

---

### TASK-1.2.8 — Bulk role assignment endpoint (GAP-20)
**Files:** `apps/users/views.py`, `apps/users/services.py`

**Endpoint:** `POST /api/v1/admin/users/bulk-assign-roles`
**Permission:** `users:manage`

**Request body:**
```json
{
  "role": "invigilator",
  "user_ids": ["uuid1", "uuid2", "uuid3"],
  "effective_from": "2026-06-01"
}
```

**Logic:**
1. Validate all user_ids exist
2. Check mutual-exclusion for each user
3. Assign role to each valid user; collect errors for invalid
4. Partial success same pattern as bulk import

**Acceptance:** Bulk-assign 50 invigilators in one call. Users with conflicting roles reported as errors.

---

### TASK-1.2.9 — Add `user_agent` to 403 audit logs (GAP-23)
**File:** `shared/permissions.py`

**Change:** In the `_record_denial()` function, add `user_agent` to indicators:
```python
indicators={
    "path": request.path,
    "method": request.method,
    "roles": roles,
    "permission": self.permission,
    "user_agent": getattr(request, "user_agent", ""),  # <-- ADD
},
```

**Acceptance:** 403 audit entries include `user_agent` in indicators.

---

### TASK-1.2.10 — Create migrations for Sprint 1.2
**Command:** `python manage.py makemigrations users && python manage.py migrate`

**Include:**
- `MutualExclusionRule` table + seed data
- `RoleAssignmentApproval` table
- `BulkImportRecord` table
- Permission + Role seed updates (full 30+ codenames, 15 roles)
- RolePermission seed (full matrix)

---

## Sprint 1.3 — Integration Polish, Dashboards & Hardening (Week 3)

**Goal:** Consolidate duplicate implementations, add missing dashboard panels, harden integration patterns, verify audit integrity.

---

### TASK-1.3.1 — Consolidate duplicate System 17 clients (GAP-28)
**Files:** `shared/integrations.py`, `shared/integrations/system17.py`

**Action:** Deprecate the older class-based `System17Client` in `shared/integrations/system17.py`. The canonical implementation is the function-based `call_system_17()` in `shared/integrations.py`.

**Steps:**
1. Add a deprecation warning to `System17Client.__init__()`:
   ```python
   import warnings
   warnings.warn("System17Client is deprecated. Use shared.integrations.call_system_17() instead.", DeprecationWarning)
   ```
2. Search codebase for all usages of `System17Client` and migrate to `call_system_17()`
3. Update the smoke test command to use `call_system_17()`
4. Mark the file with `# DEPRECATED — will be removed in Phase 2`

**Acceptance:** `grep -r "System17Client" --include="*.py" .` shows only the deprecated class itself and the deprecation warning.

---

### TASK-1.3.2 — Consolidate duplicate dashboard implementations (GAP-32)
**Files:** `apps/users/views.py`, `apps/dashboards/views.py`

**Action:** The `apps/dashboards/` app (DB-driven `DashboardPanel` model) is the canonical implementation. Remove the hardcoded `_DASHBOARD_PANELS` dict from `apps/users/views.py`.

**Steps:**
1. Ensure all panels from `_DASHBOARD_PANELS` exist in the `DashboardPanel` seed data
2. Update `GET /api/v1/me/dashboard` to delegate to `apps/dashboards/` views
3. Remove the `_DASHBOARD_PANELS` dict and `DashboardView` from users views
4. Support multi-role panel aggregation: show panels for ALL active roles, not just the first JWT role

**Acceptance:** `GET /api/v1/me/dashboard` returns panels for all user's active roles. No hardcoded panels remain.

---

### TASK-1.3.3 — Add missing 10B role dashboard panels (GAP-13, GAP-33)
**File:** Migration in `apps/dashboards/migrations/`

**Add dashboard panels for missing roles:**

| Role | Panels |
|------|--------|
| `remote-proctor` | proctoring_queue, ai_flagged_events, session_monitoring |
| `dti-operations` | infrastructure_status, power_backup_status, network_resilience, centre_readiness |
| `service-desk-agent` | support_queue, candidate_lookup, issue_tracker |
| `director-general` | dg_overview, examination_summary, audit_trail_viewer, compliance_dashboard |

**Acceptance:** `DashboardPanel.objects.filter(role_codename="remote-proctor").count()` ≥ 3. All 15 roles have dashboard panels.

---

### TASK-1.3.4 — Verify append-only DB trigger for AuditEvent (GAP-25)
**Command:** `python manage.py sqlmigrate audit 0003`

**Verify:** The migration output contains PL/pgSQL `CREATE FUNCTION` + `CREATE TRIGGER` that prevents UPDATE/DELETE on `audit_auditevent`.

**If missing:** Create a new migration:
```python
from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [("audit", "0006_alter_auditevent_entity_id")]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE OR REPLACE FUNCTION prevent_audit_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'AuditEvent rows are immutable — UPDATE/DELETE is prohibited';
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS no_audit_mutation ON audit_auditevent;
            CREATE TRIGGER no_audit_mutation
                BEFORE UPDATE OR DELETE ON audit_auditevent
                FOR EACH ROW
                EXECUTE FUNCTION prevent_audit_mutation();
            """,
            reverse_sql="""
            DROP TRIGGER IF EXISTS no_audit_mutation ON audit_auditevent;
            DROP FUNCTION IF EXISTS prevent_audit_mutation();
            """,
        ),
    ]
```

**Acceptance:** `UPDATE audit_auditevent SET action='test' WHERE id=1;` in psql raises an exception.

---

### TASK-1.3.5 — Fix request_id → correlation_id threading (GAP-29 partial)
**Files:** `shared/middleware.py`, `shared/events.py`

**Problem:** `request.request_id` from `AuditMiddleware` is NOT threaded through to `publish()` as the outbox `correlation_id`. Each outbox event gets a fresh UUID, breaking end-to-end tracing.

**Fix:** Thread `request_id` through the request context:
1. Add `request_id` to thread-local storage in `AuditMiddleware`
2. In `publish()`, default `correlation_id` to the thread-local `request_id` if available
3. In `AuditEvent.record()`, default `request_id` to the thread-local value

**Acceptance:** An API request that triggers both an `AuditEvent` and an `OutboxEvent` uses the same UUID as both `request_id` and `correlation_id`.

---

### TASK-1.3.6 — Per-user 403 rate detection (GAP-24)
**File:** `shared/permissions.py` or `shared/middleware.py`

**Add user-level denial tracking alongside IP-level:**
1. On each 403, increment Redis counter `nbes:denied:user:{sub}:15m` with 15-min TTL
2. If count exceeds 50 in 15 minutes → emit `SecurityEvent(category="user_excessive_denials")`
3. Do NOT block the user (unlike IP throttling) — just alert

**Acceptance:** A user generating 50+ 403s in 15 minutes triggers a SecurityEvent visible in the SecOps console.

---

### TASK-1.3.7 — Enforce `old_state` capture in state-change audit events (GAP-27)
**File:** `apps/audit/models.py`

**Change:** Add a validation warning when `AuditEvent.record()` is called with a state-change action but no `old_state`:
```python
STATE_CHANGE_ACTIONS = {"USER_CREATED", "USER_UPDATED", "ROLE_ASSIGNED", "ROLE_REVOKED",
    "ROLE_PERMISSIONS_UPDATED", ...}

@classmethod
def record(cls, *, action, **kwargs):
    if action in cls.STATE_CHANGE_ACTIONS and not kwargs.get("old_state"):
        import warnings
        warnings.warn(f"AuditEvent '{action}' should include old_state for compliance")
    ...
```

**Acceptance:** Missing `old_state` on state-change actions produces a warning in dev logs.

---

### TASK-1.3.8 — Wire notification bridge for profile provisioning (GAP-31)
**File:** `apps/notifications/services.py`

**Implement:** `send_provisioning_notification(user: UserProfile)` that:
1. Looks up `NotificationTemplate` for event `USER_PROVISIONED`
2. Creates a `Notification` record with `status="queued"`
3. In prod: calls System 21 API to send email "Your NBES profile is ready"
4. In dev: logs the notification

**Called from:** `create_user()` service after successful IAM provisioning + local profile creation.

**Acceptance:** Creating a user logs a notification in dev. `Notification.objects.filter(event_name="USER_PROVISIONED").count()` matches created users.

---

### TASK-1.3.9 — Machine-token authentication path (GAP-22)
**File:** `shared/auth.py`

**Add service-account token detection to `KeycloakJWTAuthentication`:**
1. Check if JWT contains `azp` (authorized party) claim typical of client_credentials grant
2. Check if `sub` == `azp` (service accounts have sub == client_id in Keycloak)
3. If service account: resolve permissions from a service-specific permission set, not from `UserRole`
4. Create a `ServicePrincipal` (or use existing `UserProfile` with a `is_service_account` flag)

**Acceptance:** A machine token from another system (e.g., System 17 calling NBES) is authenticated and authorized through the same RBAC pipeline.

---

### TASK-1.3.10 — Register new URL routes
**File:** `config/urls.py`, `apps/users/urls.py`

**Add routes:**
```python
# apps/users/urls.py — new admin user endpoints
admin_user_urlpatterns = [
    path("", UserListCreateView.as_view(), name="user-list-create"),
    path("<uuid:pk>/", UserDetailView.as_view(), name="user-detail"),
    path("<uuid:pk>/roles/", UserRoleListCreateView.as_view(), name="user-roles"),
    path("<uuid:pk>/roles/<str:role_name>/", UserRoleRevokeView.as_view(), name="user-role-revoke"),
    path("import/", BulkImportView.as_view(), name="bulk-import"),
    path("bulk-assign-roles/", BulkAssignRolesView.as_view(), name="bulk-assign-roles"),
]

role_approval_urlpatterns = [
    path("", RoleApprovalListView.as_view(), name="approval-list"),
    path("<uuid:pk>/approve/", RoleApprovalApproveView.as_view(), name="approval-approve"),
    path("<uuid:pk>/reject/", RoleApprovalRejectView.as_view(), name="approval-reject"),
]

me_urlpatterns = [
    path("", CurrentUserView.as_view(), name="me"),              # GET /api/v1/me
    path("permissions/", PermissionView.as_view(), name="me-permissions"),
    path("dashboard/", DashboardView.as_view(), name="me-dashboard"),
]

# config/urls.py — add
path("api/v1/admin/users/", include((admin_user_urlpatterns, "admin-users"))),
path("api/v1/admin/role-approvals/", include((role_approval_urlpatterns, "role-approvals"))),
```

**Acceptance:** All new endpoints are accessible. `GET /api/docs/` shows them in Swagger.

---

## Sprint 1 Acceptance Criteria Summary (from §1.10)

| # | Given/When/Then | SRS Ref | Task |
|---|---|---|---|
| 1 | Admin creates Examiner account → account exists + invite email within 5 min | F000-01 | TASK-1.1.5 |
| 2 | Role revoked → user loses access within 60 seconds | F000-02 | TASK-1.2.1 |
| 3 | Internal user attempts high-stakes action without MFA → gated until MFA satisfied | F000-03 | TASK-1.2.5 |
| 4 | 200-row import with 5 invalid rows → 195 created + row-level error report | F000-04 | TASK-1.2.7 |
| 5 | Item Writer calls results-publish API → HTTP 403 + AUTHZ_DENIED audit entry | F000-05 | (existing) |
| 6 | 100 bad logins from single IP → IP throttled 15 min + security event in System 22 | F000-06 | (existing) |
| 7 | Daily hash-anchor job runs → independent verification succeeds | F000-07 | (existing) |

**Note:** F000-05, F000-06, and F000-07 are already implemented in the existing codebase:
- F000-05: `shared/permissions.py` HasPermission + AUTHZ_DENIED audit logging
- F000-06: `shared/middleware.py` EdgeRateLimitMiddleware
- F000-07: `apps/audit/tasks.py` daily_hash_anchor

---

## Sprint 1 Demo Script (IAM-Aligned, §1.13)

1. Administrator authenticates via IAM (Keycloak login page → MFA → JWT) and calls `POST /api/v1/admin/users` to create an NBEC Member account.
2. IAM sends invite email to the new member. Member clicks link, sets password in IAM, registers MFA in IAM.
3. Administrator calls `POST /api/v1/admin/users/import` with a 50-row Invigilator CSV; 48 succeed, 2 reported with row-level errors.
4. Administrator attempts `POST /api/v1/admin/users/{id}/roles` with both `item-writer` and `moderator` for the same user; second assignment blocked with `ROLE_MUTUAL_EXCLUSION`.
5. Item Writer authenticates via IAM, calls results-publish API → HTTP 403; audit entry appears in `GET /api/v1/audit/search` within seconds.
6. 100 bad-credential attempts from single IP trigger `EdgeRateLimitMiddleware`; Security Operations Console (`GET /api/v1/secops/auth-failures`) shows the event; System 22 stub receives alert via outbox.
7. Auditor calls `GET /api/v1/audit/chain/{yesterday}` to view the hash chain proof, exports via `GET /api/v1/audit/export`, verifies hash externally.
