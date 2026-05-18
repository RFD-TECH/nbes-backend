---
name: phase1-tasks
description: Full Phase 1 sprint task breakdown — Authentication, RBAC & Foundation (SRS REQ-F000)
metadata:
  type: project
---

# Phase 1 — Authentication, RBAC & Foundation
SRS ref: REQ-F000 | 3 sprints (3 weeks)

---

## PRE-WORK: Fix Critical Bugs (before Sprint 1.1 begins)

These 5 bugs must be fixed before any sprint work is testable.

### BUG-01 — Fix dev.py wiping DRF config
**File:** `config/settings/dev.py`
**Problem:** `REST_FRAMEWORK = {**globals().get("REST_FRAMEWORK", {})}` is a no-op after `from .base import *` — it wipes the JWT auth class and custom exception handler in dev.
**Fix:** Remove the `REST_FRAMEWORK` block entirely from `dev.py`. Base settings already define it correctly.
**Acceptance:** JWT Bearer tokens are accepted by the dev server.

### BUG-02 — Fix SECRET_KEY can be None
**File:** `config/settings/base.py:9`
**Problem:** `os.environ.get("SECRET_KEY")` returns None if env var unset.
**Fix:** Change to `os.environ["SECRET_KEY"]` (no `.get()`). Server fails loudly at startup if missing.
**Acceptance:** Starting server without SECRET_KEY set prints a clear KeyError, not a silent None.

### BUG-03 — Fix timezone.timedelta crash in cert_trigger
**File:** `apps/cert_trigger/models.py:47`
**Problem:** `timezone.timedelta` does not exist on `django.utils.timezone`.
**Fix:** `from datetime import timedelta` and use `timedelta(hours=1)`.
**Acceptance:** `CertTriggerRecord.fire()` no longer raises AttributeError.

### BUG-04 — Fix nlems_eligibility_verified guard
**File:** `workflow/guards.py:77`
**Problem:** Checks `instance.eligibility_status` on `Registration`, which has no such field. Should check `instance.candidate.eligibility_status`.
**Fix:** `return instance.candidate.eligibility_status == "eligible"`
**Acceptance:** Guard returns correct value; registration FSM payment transition unblocked.

### BUG-05 — Fix AuditEvent race condition
**File:** `apps/audit/models.py:55`
**Problem:** `cls.objects.order_by("-id").values("chain_hash").first()` without `select_for_update()` — concurrent writes fork the hash chain.
**Fix:** Wrap the `record()` method body in `with transaction.atomic(): cls.objects.select_for_update().order_by("-id")...`
**Acceptance:** Concurrent audit writes produce a linear chain, not a fork.

---

## Sprint 1.1 — Identity & MFA Core (Week 1)

**Goal:** A user can be created, log in with a password, configure MFA, and have their session managed.

---

### TASK-1.1.1 — Expand UserProfile model (data model additions §1.5.1)
**File:** `apps/users/models.py`
**What to add:**
- `first_name` CharField(max_length=100)
- `last_name` CharField(max_length=100)
- `status` CharField with choices: `active`, `inactive`, `locked`, `pending_mfa` (default: `pending_mfa`)
- `mfa_enrolled` BooleanField(default=False)
- `password_hash` CharField(max_length=255, blank=True) — stores bcrypt hash; Django's `make_password()` / `check_password()`
- `password_changed_at` DateTimeField(null=True)
- `failed_login_count` PositiveSmallIntegerField(default=0)
- `locked_until` DateTimeField(null=True, blank=True) — set on 5th failed login
- `last_login_at` DateTimeField(null=True, blank=True)
- `deactivated_at` DateTimeField(null=True, blank=True)
- `created_by` ForeignKey('self', null=True, on_delete=SET_NULL)
- `invite_token` UUIDField(null=True, blank=True) — single-use first-time-login token
- `invite_expires_at` DateTimeField(null=True, blank=True)

**Note:** Keep `keycloak_sub` but make it nullable — in dev we create users directly. In prod, Keycloak owns this.

**Acceptance:** Model fields match §1.5.1 `user` table definition.

---

### TASK-1.1.2 — Add LoginAttempt and Session models (§1.5.1)
**File:** `apps/users/models.py` (add below UserProfile)

**LoginAttempt model:**
```
id, user (FK nullable — failed attempts may have no valid user), ip_address,
user_agent, outcome (choices: success/failure/locked/mfa_required),
occurred_at (auto_now_add)
```

**Session model:**
```
id (UUID), user (FK), issued_at, expires_at, mfa_verified_at (null),
revoked_at (null), ip_address, user_agent, is_active (property: not revoked and not expired)
```

**Acceptance:** LoginAttempt written on every auth attempt. Session row created on successful login.

---

### TASK-1.1.3 — Add MFAEnrolment model (§1.5.1)
**File:** `apps/users/models.py`

**MFAEnrolment model:**
```
id (UUID), user (FK OneToMany — one per factor type), 
factor_type (choices: totp, webauthn, sms),
credential_ref (TextField — for TOTP: encrypted secret; for WebAuthn: credential_id + public_key JSON; for SMS: phone number),
is_active (BooleanField),
last_used_at (DateTimeField null)
```

**Acceptance:** A user can have multiple enrolments. `user.mfa_enrolled` is True when at least one active MFAEnrolment exists.

---

### TASK-1.1.4 — Create migrations for all apps
**Command:** `python manage.py makemigrations users audit committee itembank registration marking results cert_trigger sla resit`
Then: `python manage.py migrate`

**Acceptance:** `python manage.py showmigrations` shows all apps with `[X]` checkmarks. No migration errors.

---

### TASK-1.1.5 — Implement login endpoint (§1.6: POST /api/v1/auth/login)
**Files:** `apps/users/views.py`, `apps/users/serializers.py`, `apps/users/urls.py`

**What it does:**
1. Accept `{"email": "...", "password": "..."}`
2. Look up `UserProfile` by email (case-insensitive)
3. Check `user.status != "inactive"` and `user.status != "locked"` (check `locked_until`)
4. Verify password with `check_password(raw, user.password_hash)`
5. On failure: increment `failed_login_count`; if `failed_login_count >= 5`, set `status="locked"`, `locked_until=now()+15min`, send lockout email via System 21 stub
6. On failure: write `LoginAttempt(outcome="failure")`, write `AuditEvent(action="AUTH_FAILED")`
7. On success: reset `failed_login_count=0`, set `last_login_at=now()`, create `Session` row
8. If `mfa_enrolled=True` on user: return `{"success": true, "data": {"mfa_required": true, "session_token": "<partial-token>"}}` — do NOT return full JWT yet
9. If `mfa_enrolled=False` and user is internal role: return `{"success": true, "data": {"mfa_required": true, "setup_required": true}}` — force MFA setup before issuing JWT
10. If `mfa_enrolled=False` and user is `candidate` role: issue JWT immediately
11. JWT payload: `{"sub": str(user.id), "email": user.email, "role": user.role, "session_id": str(session.id), "exp": now+8h}`
12. Sign with `settings.JWT_SECRET_KEY` using HS256

**Rate limit:** Max 10 requests/minute per IP using DRF throttling (`AnonRateThrottle`).

**Response envelope:** `{"success": true/false, "data": {...}, "error": {...}, "meta": {"request_id": "..."}}`

**Acceptance (F000-01):** Valid credentials return JWT. Invalid credentials increment counter. 5th failure locks account and sends email.

---

### TASK-1.1.6 — Implement MFA challenge/response endpoint (§1.6: POST /api/v1/auth/mfa)
**Files:** `apps/users/views.py`, `apps/users/serializers.py`

**What it does:**
1. Accept `{"partial_token": "...", "factor_type": "totp", "code": "123456"}`
2. Validate partial_token (short-lived, signed, contains session_id)
3. Look up user's active MFAEnrolment for that factor_type
4. **TOTP:** `import pyotp; totp = pyotp.TOTP(enrolment.credential_ref); totp.verify(code)` — accepts codes within 1 window (±30s)
5. **WebAuthn:** stub in Phase 1.1; return `NotImplementedError` with clear message — implement in Sprint 1.2
6. **SMS:** stub in Phase 1.1 (full SMS via System 21 in Phase 9)
7. On success: update `Session.mfa_verified_at=now()`, issue full JWT with `mfa_verified: true` claim
8. Write `LoginAttempt(outcome="success")`, `AuditEvent(action="AUTH_MFA_SUCCESS")`
9. On failure: `AuditEvent(action="AUTH_MFA_FAILED")`

**Acceptance (F000-03):** TOTP code accepted within window; invalid code rejected. Full JWT only issued after MFA.

---

### TASK-1.1.7 — Implement TOTP enrolment endpoint
**Files:** `apps/users/views.py`

**What it does:**
1. `GET /api/v1/auth/mfa/totp/setup` — generate a new TOTP secret (`pyotp.random_base32()`), return QR code URI and the secret
2. Secret is NOT saved yet — only saved after the user confirms with a valid code
3. `POST /api/v1/auth/mfa/totp/confirm` — `{"secret": "...", "code": "..."}` — verifies code, saves `MFAEnrolment`, sets `user.mfa_enrolled=True`
4. Write `AuditEvent(action="MFA_ENROLLED", entity_type="user")`

**Requires pyotp:** Add `pyotp` to `requirements.txt`.

**Acceptance:** User can scan QR, enter code, and have TOTP enrolment confirmed. Second call to `/setup` after enrolment returns error "MFA already enrolled."

---

### TASK-1.1.8 — Implement token refresh endpoint (§1.6: POST /api/v1/auth/refresh)
**Files:** `apps/users/views.py`

**What it does:**
1. Accept `{"refresh_token": "..."}` (separate long-lived refresh token issued at login, 24h expiry)
2. Validate refresh token, check `Session.revoked_at is None` and `Session.expires_at > now()`
3. Issue a new short-lived access JWT (8h)
4. Write `AuditEvent(action="TOKEN_REFRESHED")`

**Acceptance:** Valid refresh token returns new access JWT. Revoked session refresh returns 401.

---

### TASK-1.1.9 — Implement logout endpoint (§1.6: POST /api/v1/auth/logout)
**Files:** `apps/users/views.py`

**What it does:**
1. Authenticated request only
2. Set `Session.revoked_at = now()` for the session in the current JWT (`session_id` claim)
3. Write `AuditEvent(action="AUTH_LOGOUT")`
4. Return `{"success": true}`

**Note:** JWT tokens are stateless — logout works by revoking the Session record. The auth middleware must check `Session.revoked_at is None` on every request (or use Redis cache for revoked session IDs).

**Acceptance:** After logout, the same JWT returns 401 on next use.

---

### TASK-1.1.10 — Implement password policy validation
**File:** `apps/users/services.py` — function `validate_password(raw_password) -> list[str]`

**Rules from §1.2.3:**
- Minimum 12 characters
- Must contain: uppercase, lowercase, digit, special character
- Must NOT be in the last 12 passwords (check `PasswordHistory` model — add this model)
- SHOULD check against HaveIBeenPwned API (k-anonymity SHA-1 prefix method): `GET https://api.pwnedpasswords.com/range/{first5}` — if count > 0, reject

**PasswordHistory model** (add to `apps/users/models.py`):
```
id, user (FK), password_hash, created_at
```
Keep last 12 rows per user; on new password, check none match, then insert and delete oldest if > 12.

**Acceptance:** Password "Password1!" passes. "password" fails (no uppercase, no special, too short-ish). "correct horse battery staple" fails (no digit/special). "P@ssw0rd123!" passes all rules.

---

### TASK-1.1.11 — Implement account lockout Celery task (auto-unlock)
**File:** `apps/users/tasks.py`

**Task:** `unlock_expired_accounts` — runs every 5 minutes via Celery Beat.
- Find all `UserProfile` where `status="locked"` and `locked_until < now()`
- Set `status="active"`, `failed_login_count=0`, `locked_until=None`
- Write `AuditEvent(action="ACCOUNT_AUTO_UNLOCKED")` for each

**Add to `config/celery.py` Beat schedule.**

**Acceptance (F000-06):** Account locked at T=0 becomes `active` automatically after 15 minutes.

---

### TASK-1.1.12 — Implement invite email on account creation (§1.2.1)
**File:** `apps/users/services.py` — function `send_invite_email(user: UserProfile)`

**What it does:**
1. Generate `invite_token = uuid4()`, set `invite_expires_at = now() + 7 days`
2. Save to user
3. Send email via `django.core.mail.send_mail` (in dev: prints to console per `EMAIL_BACKEND`)
4. Email body contains first-time-login link: `https://{FRONTEND_URL}/auth/first-login?token={invite_token}`

**Acceptance:** Creating a user via the admin console (TASK-1.1.13) sends an email within 5 minutes (F000-01 acceptance).

---

### TASK-1.1.13 — Admin User Console — Create user endpoint (§1.6: POST /api/v1/admin/users)
**Files:** `apps/users/views.py`, `apps/users/serializers.py`

**Permission:** `system-administrator` role only.

**Accepted body:**
```json
{
  "first_name": "Kwame",
  "last_name": "Mensah",
  "email": "kmensah@gsl.edu.gh",
  "role": "examiner",
  "effective_date": "2026-05-20",
  "id_number": "GHA-12345"   // optional
}
```

**What it does:**
1. Validate email uniqueness (case-insensitive: `UserProfile.objects.filter(email__iexact=email).exists()`)
2. Validate `role` is in `ROLE_PERMISSION_MAP` keys (or a separate roles list)
3. Create `UserProfile` with `status="pending_mfa"`, no password set yet
4. Call `send_invite_email(user)`
5. Write `AuditEvent(action="USER_CREATED", entity_type="user", actor_id=request.auth["sub"], old_state=None, new_state={...}, ip_address=request.ip_address)`
6. Return 201 with user data

**Acceptance (F000-01):** Creates account, invite email dispatched. Duplicate email returns 400.

---

### TASK-1.1.14 — Admin User Console — Edit/deactivate/delete endpoint (§1.6: PATCH /api/v1/admin/users/{id})
**Files:** `apps/users/views.py`, `apps/users/serializers.py`

**Permission:** `system-administrator` only.

**Edit:** PATCH with any subset of `{first_name, last_name, email, role, effective_date}`
**Deactivate:** PATCH with `{"status": "inactive"}` — sets `deactivated_at=now()`, sends notification email
**Logical delete:** PATCH with `{"deleted": true}` — sets `status="deleted"`, preserves all data (retention)

**Constraint:** Cannot deactivate a user with open active assignments (Phase 3/9 will add specific checks; for now, the check is a placeholder that always passes).

**Audit:** Every change writes `AuditEvent(action="USER_UPDATED", old_state={...before...}, new_state={...after...})`.

**Acceptance:** PATCH changes fields. Deactivation sets `deactivated_at`. Audit entry shows before/after.

---

### TASK-1.1.15 — First-time-login endpoint (accept invite token)
**File:** `apps/users/views.py` — `POST /api/v1/auth/first-login`

**What it does:**
1. Accept `{"token": "...", "password": "...", "confirm_password": "..."}`
2. Find user by `invite_token`, check `invite_expires_at > now()`
3. Run password policy validation (TASK-1.1.10)
4. Hash password with `make_password(raw)`, save to `user.password_hash`
5. Clear `invite_token`, `invite_expires_at`
6. Set `user.status = "pending_mfa"` (MFA setup required before first real login)
7. Write `AuditEvent(action="FIRST_LOGIN_PASSWORD_SET")`
8. Return `{"success": true, "data": {"mfa_setup_required": true}}`

**Acceptance:** Invite link works once. Second use of same token returns 400 "invalid or expired token."

---

## Sprint 1.2 — Roles, Permissions & RBAC Gateway (Week 2)

**Goal:** Configurable role/permission matrix, mutual-exclusion rules, two-approver flow for high-privilege roles, bulk import, WebAuthn MFA.

---

### TASK-1.2.1 — Add Role and Permission models (§1.5.1)
**File:** `apps/users/models.py`

**Role model:**
```
id (UUID), name (unique, e.g. "nbec-member"), description, is_internal (bool),
created_at
```

**Permission model:**
```
id (UUID), scope (e.g. "item"), resource (e.g. "item"), action (e.g. "approve"),
code (unique, e.g. "item:approve"), created_at
```

**RolePermission model:**
```
id, role (FK Role), permission (FK Permission), unique_together(role, permission)
```

**UserRole model:**
```
id (UUID), user (FK UserProfile), role (FK Role),
effective_from (DateField), effective_to (DateField null — open-ended),
assigned_by (FK UserProfile null), revoked_at (DateTimeField null),
revoke_reason (TextField blank)
```

**Seed data migration:** Create a `RunPython` migration that populates `Role` and `Permission` from `ROLE_PERMISSION_MAP` in `shared/permissions.py`. All 15 SRS roles must exist:
`nbec-member, nbec-secretariat, item-writer, moderator, examiner, candidate, clet-registrar, invigilator, centre-coordinator, remote-proctor, dti-operations, service-desk-agent, auditor, system-administrator, director-general`

**Acceptance:** `python manage.py shell` → `Role.objects.count()` returns 15.

---

### TASK-1.2.2 — Update ROLE_PERMISSION_MAP with all missing roles (§1.2.2)
**File:** `shared/permissions.py`

**Add permissions for missing roles:**
```python
"users:manage":            ["system-administrator"],
"users:import":            ["system-administrator"],
"audit:view":              ["auditor", "director-general", "system-administrator"],
"audit:export":            ["auditor", "director-general"],
"security:view":           ["system-administrator", "dti-operations"],
"centre:manage":           ["centre-coordinator", "dti-operations"],
"centre:invigilate":       ["invigilator"],
"proctoring:remote":       ["remote-proctor"],
"helpdesk:support":        ["service-desk-agent"],
"dg:overview":             ["director-general"],
"sitting:invigilate":      ["invigilator", "centre-coordinator"],
```

**Update HasPermission** to also check `UserRole` table (TASK-1.2.1) rather than only `request.auth["role"]`. UserRole is the authoritative source; JWT role is a cached hint only.

**Acceptance:** An `auditor` JWT can access `GET /api/v1/audit/search`. An `item-writer` JWT gets 403 on the same endpoint.

---

### TASK-1.2.3 — Mutual-exclusion rule enforcement (§1.2.2)
**File:** `apps/users/services.py` — function `assign_role(user, role, assigned_by, effective_from)`

**Mutually exclusive pairs (from SRS):**
- `item-writer` ↔ `moderator` (same item — enforced at Phase 3; at Phase 1, block coexistence entirely)
- `invigilator` ↔ `candidate` (always blocked)
- Any pair from: `{system-administrator, director-general, nbec-member}` with `candidate`

**What it does:**
1. Check if user already has any role in the exclusion group
2. If yes, return error `{"code": "ROLE_MUTUAL_EXCLUSION", "message": "User already has role X which conflicts with Y"}`
3. If the role is high-privilege (`nbec-member` with Chair designation, `director-general`, `system-administrator`): create a `RoleAssignmentApproval` record (see TASK-1.2.4) instead of activating immediately
4. Otherwise: create `UserRole(effective_from=effective_from)`, write `AuditEvent(action="ROLE_ASSIGNED")`
5. Publish `RoleChanged` outbox event so downstream systems invalidate cached permissions within 60 seconds

**Acceptance (F000-02):** Assigning `invigilator` to a `candidate` returns 400 with `ROLE_MUTUAL_EXCLUSION`. Assigning `examiner` to an `item-writer` returns 400.

---

### TASK-1.2.4 — Two-approver flow for high-privilege roles (§1.2.2)
**File:** `apps/users/models.py` — add `RoleAssignmentApproval` model

```
id (UUID), user (FK), role (FK), requested_by (FK UserProfile),
first_approval_by (FK UserProfile null), first_approval_at (DateTimeField null),
status (choices: pending/approved/rejected), created_at
```

**Endpoint:** `POST /api/v1/admin/role-approvals/{id}/approve` — second administrator approves.

**What it does:**
1. On first approval: set `first_approval_by`, `first_approval_at`
2. Second administrator (different from requester AND first approver) calls approve: creates `UserRole`, writes audit
3. Same administrator cannot provide both approvals — return 400 `SELF_APPROVAL_NOT_PERMITTED`

**Acceptance (§1.2.2):** High-privilege assignment pends until a second administrator approves. One admin cannot self-approve.

---

### TASK-1.2.5 — Role assignment and revocation endpoints (§1.6)
**File:** `apps/users/views.py`

**Endpoints:**
- `POST /api/v1/admin/users/{id}/roles` — body: `{"role": "examiner", "effective_from": "2026-05-20"}`
- `DELETE /api/v1/admin/users/{id}/roles/{role_name}` — body: `{"reason": "..."}`

**Role revocation:**
1. Set `UserRole.revoked_at = now()`, `revoke_reason = reason`
2. Publish `RoleChanged` outbox event
3. Write `AuditEvent(action="ROLE_REVOKED", old_state={"role": ...})`

**List roles for user:**
- `GET /api/v1/admin/users/{id}/roles` — returns active UserRole records

**Acceptance:** Revoking a role publishes `RoleChanged` event; within 60 seconds the user's next API call is 403 for that role's endpoints.

---

### TASK-1.2.6 — Role and permission matrix endpoints (§1.6)
**File:** `apps/users/views.py`

**Endpoints:**
- `GET /api/v1/admin/roles` — list all roles with their permissions (system-administrator only)
- `POST /api/v1/admin/roles/{id}/permissions` — body: `{"add": ["audit:export"], "remove": ["audit:view"]}` — update role permissions
- Every permission change writes `AuditEvent(action="ROLE_PERMISSIONS_UPDATED")`

**Acceptance:** Administrator can view and update the permission matrix via the API. Changes reflected on next request.

---

### TASK-1.2.7 — Current user profile endpoint (§1.6: GET /api/v1/me)
**File:** `apps/users/views.py`

**What it returns:**
```json
{
  "success": true,
  "data": {
    "id": "...",
    "email": "...",
    "first_name": "...",
    "last_name": "...",
    "role": "examiner",
    "roles": [...],
    "mfa_enrolled": true,
    "effective_permissions": ["marking:second_mark", "..."]
  }
}
```

**`effective_permissions`** is computed: look up all active `UserRole` records for the user, collect all `Permission.code` values from their `RolePermission` records.

**Acceptance:** Authenticated user gets their profile and permission list. Permissions reflect current role assignments, not stale JWT claims.

---

### TASK-1.2.8 — Bulk user import (§1.2.4, §1.6: POST /api/v1/admin/users/import)
**File:** `apps/users/views.py`, `apps/users/services.py`

**What it does:**
1. Accept `multipart/form-data` with `file` (CSV or XLSX)
2. Parse CSV: columns `first_name, last_name, email, role, effective_date, id_number(optional)`
3. Validate each row: email format, role valid, required fields present
4. **Partial success:** commit valid rows individually; collect errors for invalid rows
5. For each created user: call `send_invite_email(user)`
6. Compute SHA-256 hash of the uploaded file; write `AuditEvent(action="BULK_IMPORT", new_state={"file_hash": "...", "rows_total": N, "rows_ok": N, "rows_failed": N})`
7. Store original file in MinIO (or local filesystem in dev — skip MinIO if `MINIO_ENABLED=False`)
8. Return: `{"success": true, "data": {"created": 195, "failed": 5, "errors": [{"row": 3, "error": "invalid email"}, ...]}}`

**Requires:** `openpyxl` for XLSX parsing (add to `requirements.txt`).

**Acceptance (F000-04):** 200-row import with 5 invalid rows: 195 users created, 5 error rows reported. File hash recorded in audit.

---

### TASK-1.2.9 — WebAuthn MFA enrolment (§1.2.3)
**File:** `apps/users/views.py`

**Endpoints:**
- `POST /api/v1/auth/mfa/webauthn/register/begin` — returns WebAuthn challenge options JSON
- `POST /api/v1/auth/mfa/webauthn/register/complete` — verifies attestation, saves credential to MFAEnrolment
- `POST /api/v1/auth/mfa/webauthn/authenticate/begin` — returns assertion options
- `POST /api/v1/auth/mfa/webauthn/authenticate/complete` — verifies assertion, issues JWT

**Requires:** `py_webauthn` library (add to `requirements.txt`).

**Dev note:** WebAuthn requires HTTPS or localhost — works on `http://localhost:8003` in dev.

**Acceptance:** User can register a hardware key or platform authenticator (Windows Hello, Touch ID). Authentication with the key completes login and issues JWT.

---

### TASK-1.2.10 — IP-level brute-force throttle (§1.2.6)
**File:** `apps/users/services.py` — function `check_ip_throttle(ip_address) -> bool`

**Rules from §1.2.6:**
- 100 failed logins from a single IP in any 60-second window → 15-minute IP throttle
- 1000 failed logins from a single IP in any 24-hour window → 24-hour IP block

**Implementation:**
1. Use Redis (already in stack) as counter store: `INCR nbes:login_fail:{ip}:60s` with `EXPIRE 60`
2. 24h counter: `INCR nbes:login_fail:{ip}:86400` with `EXPIRE 86400`
3. On threshold: store `nbes:throttle:{ip}` with expiry in Redis; check at the start of the login endpoint
4. On 24h block: write `AuditEvent(action="IP_BLOCKED", new_state={"ip": ip, "duration_hours": 24})`
5. Send security event to System 22 via outbox (topic: `nbes.security`)

**Acceptance (F000-06):** 100 bad-credential requests from same IP within 1 minute triggers throttle. Next request from that IP gets 429 before password check even runs.

---

## Sprint 1.3 — Audit Substrate, Security Ops & Integration Patterns (Week 3)

**Goal:** Audit chain exported to System 22, Security Operations console, System 17 integration substrate, role dashboard skeleton endpoints.

---

### TASK-1.3.1 — Fix AuditEvent select_for_update (pre-work BUG-05, now in sprint)
Already covered in pre-work. Confirm it's done before this sprint starts.

---

### TASK-1.3.2 — DailyHashAnchor model and daily export job (§1.2.7)
**Files:** `apps/audit/models.py`, `apps/audit/tasks.py`

**DailyHashAnchor model:**
```
id, date (DateField unique), head_hash (CharField 64), 
exported_to_s22_at (DateTimeField null), anchor_ref (CharField blank)
```

**Celery Beat task:** `export_daily_audit_anchor` — runs at 01:00 UTC (already in `config/celery.py` framework, add Beat entry).

**What the task does:**
1. Find all `AuditEvent` for `date=yesterday`
2. Take the last event's `chain_hash` as `head_hash`
3. If already exported (`DailyHashAnchor` exists with `exported_to_s22_at` set): skip (idempotent)
4. Sign the anchor payload with `settings.JWT_SECRET_KEY` (HMAC-SHA256)
5. POST to System 22 stub: `shared/integrations/system22.py` — in dev, just log the call and set `exported_to_s22_at=now()`
6. Create/update `DailyHashAnchor(date=yesterday, head_hash=..., exported_to_s22_at=now())`
7. On failure: write to `logs/audit_export_failed.jsonl` local file fallback; retry next run

**Acceptance (§1.2.7):** After running the task, `DailyHashAnchor.objects.filter(date=yesterday).first().exported_to_s22_at` is not None.

---

### TASK-1.3.3 — Audit search endpoint (§1.6: GET /api/v1/audit/search)
**Files:** `apps/audit/views.py`, `apps/audit/serializers.py`, `apps/audit/urls.py`

**Permission:** `auditor`, `director-general`, `system-administrator` roles only.

**Query parameters:**
- `actor_id` (UUID)
- `action` (string, partial match)
- `entity_type` (string)
- `entity_id` (UUID)
- `from_date`, `to_date` (ISO date)
- `page`, `page_size`

**Endpoint uses `django-filter`** (`AuditEventFilter` in `apps/audit/filters.py`).

**Returns:** Paginated list of audit events in standard envelope. `AuditEvent.record()` calls that include `actor_id` from current request must include `ip_address` and `user_agent` from `request`.

**Write `AuditEvent(action="AUDIT_SEARCH_PERFORMED", new_state={"filters": {...}, "result_count": N})` every time the endpoint is called (meta-audit).

**Acceptance:** Auditor can filter events by date range and actor. Non-auditor gets 403.

---

### TASK-1.3.4 — Audit chain verification endpoint (§1.6: GET /api/v1/audit/chain/{date})
**File:** `apps/audit/views.py`

**Permission:** `auditor` only.

**What it returns:**
```json
{
  "success": true,
  "data": {
    "date": "2026-05-17",
    "head_hash": "abc123...",
    "event_count": 147,
    "exported_to_s22_at": "2026-05-18T01:00:15Z",
    "anchor_ref": "S22-2026-05-17-...",
    "chain_valid": true   // server re-computes chain and verifies
  }
}
```

**`chain_valid`** is computed by replaying all events for that date in order and recomputing chain hashes. If any hash mismatches, `chain_valid=false`.

**Acceptance:** Returns chain proof for any given date. Tampering with an audit row (direct DB edit) causes `chain_valid=false`.

---

### TASK-1.3.5 — Security Operations audit log for auth events (§1.2.6)
**Files:** `apps/users/views.py` (login endpoint), `shared/auth.py`

**All these events must write an AuditEvent AND publish to outbox topic `nbes.security`:**
- `AUTH_FAILED` — bad password
- `AUTH_MFA_FAILED` — bad MFA code
- `AUTH_SUCCESS` — successful login
- `AUTH_TOKEN_EXPIRED` — expired JWT presented
- `AUTH_ROLE_MISMATCH` — valid JWT but role insufficient (403)
- `AUTH_IP_THROTTLED` — IP throttle triggered
- `AUTH_IP_BLOCKED` — IP blocked for 24h
- `ACCOUNT_LOCKED` — account locked after 5 failures
- `ACCOUNT_AUTO_UNLOCKED` — account auto-unlocked after cooldown

**Each event must include:** `actor_id` (null if unauthenticated), `ip_address`, `user_agent`, `action`, `new_state` with relevant context.

**Acceptance (§1.2.6):** Every failed login produces an `AUTH_FAILED` audit entry. Security events appear in `GET /api/v1/audit/search?action=AUTH_FAILED`.

---

### TASK-1.3.6 — System 17 integration substrate (§1.2.8)
**File:** `shared/integrations/system17.py` (create this file)

**What it is:** A reusable client for all inter-system HTTP calls. Every later phase uses this — do not let phases implement their own HTTP calls.

**Implements:**
```python
class System17Client:
    def post(self, path: str, payload: dict, idempotency_key: str) -> dict:
        """
        Signs payload with HMAC-SHA256 using SYSTEM_17_API_KEY.
        Adds headers: X-Nonce, X-Timestamp, X-Signature, X-Idempotency-Key.
        Replay protection: nonce = uuid4(), timestamp = now ISO.
        Retries 3 times with exponential backoff on 5xx.
        Returns response JSON or raises IntegrationError.
        """
```

**Signature scheme:** `HMAC-SHA256(key=SYSTEM_17_API_KEY, msg=f"{nonce}:{timestamp}:{json.dumps(payload, sort_keys=True)}")`

**In dev** (`SYSTEM_17_URL` not configured): log the call and return `{"status": "stub_ok"}`.

**Acceptance:** Calling `System17Client().post("/api/v1/test", {"x": 1}, "key-123")` in dev logs the call with correct signature headers.

---

### TASK-1.3.7 — System 22 integration stub (§1.2.8)
**File:** `shared/integrations/system22.py` (create this file)

**What it is:** Client for forwarding audit anchors and security events to System 22 (SIEM/tamper-evident store).

```python
class System22Client:
    def export_audit_anchor(self, date: str, head_hash: str, event_count: int) -> str:
        """Returns anchor_ref string. In dev: logs and returns stub ref."""
    
    def send_security_event(self, event_type: str, payload: dict) -> None:
        """Forwards security events (auth failures, blocks, anomalies). In dev: logs only."""
```

**Acceptance:** `export_daily_audit_anchor` task calls `System22Client().export_audit_anchor(...)` without raising. Dev logs confirm the call was made.

---

### TASK-1.3.8 — Role Dashboard Skeleton endpoints (§1.2.9)
**File:** `apps/users/views.py` — `GET /api/v1/dashboard`

**What it returns:** An empty-state dashboard payload for the user's role, structured so the frontend can render panels.

```json
{
  "success": true,
  "data": {
    "role": "nbec-member",
    "panels": [
      {"id": "meeting_agenda", "title": "Meeting Agenda", "data": null, "status": "not_implemented"},
      {"id": "pending_approvals", "title": "Pending Approvals", "data": null, "status": "not_implemented"},
      {"id": "conflict_declarations", "title": "Conflict Declarations", "data": null, "status": "not_implemented"},
      {"id": "audit_trail", "title": "Audit Trail", "data": null, "status": "not_implemented"}
    ]
  }
}
```

**Panel map per role** (from §1.2.9):
- `nbec-member`: meeting_agenda, pending_approvals, conflict_declarations, audit_trail_viewer
- `nbec-secretariat`: committee_operations, candidate_registration_desk, exception_queue
- `item-writer`: my_items, drafts, peer_review_feedback
- `moderator`: review_queue, panel_decisions, item_search
- `examiner`: marking_queue, borderline_review_queue
- `candidate`: registration, payment, slip, results, remarking
- `clet-registrar`: override_queue, ratification_dashboard, cert_trigger_panel
- `invigilator`: centre_operations, candidate_checkin, proctoring_queue
- `centre-coordinator`: centre_operations, candidate_checkin, proctoring_queue
- `system-administrator`: users, roles, integrations, audit, system_health
- `auditor`: audit_trail_search, hash_chain_verifier, export

**Acceptance:** `GET /api/v1/dashboard` returns correct panel list for each role. Unknown role returns empty panels.

---

### TASK-1.3.9 — Ensure all 403 events are audit-logged (§1.2.5)
**File:** `shared/permissions.py` — `HasPermission.has_permission()`

**Uncomment and implement the TODO block (lines 68-74):**
```python
if not granted:
    from apps.audit.models import AuditEvent
    AuditEvent.record(
        actor_id=request.auth.get("sub") if request.auth else None,
        action="AUTHZ_DENIED",
        entity_type="api_endpoint",
        new_state={
            "permission_required": self.permission,
            "role_presented": role,
            "path": request.path,
            "method": request.method,
        },
        ip_address=getattr(request, "ip_address", None),
        user_agent=getattr(request, "user_agent", ""),
    )
```

**Acceptance (F000-05):** Item Writer calls `POST /api/v1/admin/users` → receives 403. Audit search shows `AUTHZ_DENIED` entry with actor and path.

---

### TASK-1.3.10 — Update UserProfile.role on every authenticated request
**File:** `shared/auth.py` — `KeycloakJWTAuthentication.authenticate()`

**Problem:** `get_or_create` only sets `role` on first creation. If the role changes in Keycloak or via the admin, the stale role is used.

**Fix:** Change `get_or_create` to update `role` and `email` on every request:
```python
user, created = UserProfile.objects.get_or_create(
    keycloak_sub=payload["sub"],
    defaults={"email": payload.get("email", ""), "role": payload.get("role", "")},
)
if not created:
    UserProfile.objects.filter(pk=user.pk).update(
        email=payload.get("email", user.email),
        role=payload.get("role", user.role),
    )
    user.refresh_from_db()
```

**Acceptance:** Changing a user's role in the JWT payload and re-authenticating immediately reflects the new role.

---

### TASK-1.3.11 — Write migrations for all new models
After TASK-1.3.8, run `python manage.py makemigrations` to capture all new models from this sprint (DailyHashAnchor, Role, Permission, RolePermission, UserRole, RoleAssignmentApproval, MFAEnrolment, LoginAttempt, Session, PasswordHistory).

---

## Sprint 1 Acceptance Criteria Summary (from §1.11)

| # | Given/When/Then | SRS Ref |
|---|---|---|
| 1 | Admin creates Examiner account → account exists + invite email within 5 min | F000-01 |
| 2 | Internal user attempts high-stakes action without MFA → gated until MFA satisfied | F000-03 |
| 3 | Role revoked → user loses access within 60 seconds | F000-02 |
| 4 | Item Writer calls results-publish API → HTTP 403 + audit entry | F000-05 |
| 5 | 100 bad logins from single IP → IP throttled 15 min + security event in System 22 | F000-06 |
| 6 | 200-row import with 5 invalid rows → 195 created + row-level error report | F000-04 |
| 7 | Daily hash-anchor job runs → independent verification with public key succeeds | §1.2.7 |

---

## Sprint 1 Demo Script (§1.12)

1. Admin logs in (MFA TOTP prompt) → creates an NBEC Member account
2. NBEC Member receives invite email, sets password (HaveIBeenPwned check enforced), registers WebAuthn
3. Admin bulk-imports 50-row Invigilator cohort; 48 succeed, 2 reported with row-level errors
4. Admin attempts to assign Item Writer + Moderator to same user; blocked
5. Item Writer logs in, calls results-publish API → HTTP 403; audit entry appears in Auditor search within seconds
6. Simulate 100 bad-credential attempts from single IP; throttle engages; Security Operations Console shows event; System 22 stub receives alert
7. Auditor views yesterday's audit chain, exports proof, verifies hash externally
