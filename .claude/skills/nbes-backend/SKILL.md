---
name: nbes-backend
description: >
  Blueprint for the NBES Core Platform (System 10A) — Phase 1 only:
  Authentication, RBAC, MFA, audit substrate, and the integration patterns
  (System 17 / System 22) that every later phase will consume. Use this skill
  for any change to shared/auth.py, shared/permissions.py, shared/events.py,
  shared/vault.py, shared/middleware.py, shared/exceptions.py, apps/users/,
  apps/audit/, or anything that touches identity, sessions, MFA, RBAC,
  permission checks, audit events, the outbox, the daily hash anchor, brute-
  force defence, or the two-Administrator approval flow.
  Project structure is fixed — do not restructure apps or shared modules.
  Phase 2–10 work lives in separate skills; do not pre-build it here.
  Task-level breakdowns live in tasks.md — this file is the blueprint, not
  the to-do list.
---

# NBES Core Platform — Phase 1: Foundation

**System**: 10A — National Bar Examination System (NBES) Core Platform
**Framework**: Django 5 / DRF · PostgreSQL 16 · Celery · Redis · Kafka
**Legislative basis**: §§65–73 of the Legal Practitioners Act (Ghana)
**Port**: 8003 (direct) / 8000 via API Gateway (System 17)
**Phase 1 SRS reference**: REQ-F000 (F000-01 … F000-06)

Phase 1 builds the floor on which every other module stands: authenticated
users with the right roles, an audit substrate that makes every later action
defensible, and the integration patterns (System 17 for outbound calls,
System 22 for audit) that subsequent phases will reuse rather than re-invent.

This skill covers Phase 1 only. Phases 2–10 (committee, item bank, sitting
config, registration, marking, results, re-sit, cert trigger) are out of scope
here — they live in their own skills and have their own blueprints.

---

## 1. Invariants — Apply to Every Change

These are non-negotiable. Code that violates any of them is wrong even if
the tests pass. They are the things a judicial-review auditor will check first.

1. **Every state change emits an `AuditEvent`** via `AuditEvent.record(...)`.
   No exceptions — including failed authorisations (403), failed logins,
   role changes, MFA enrolments, and account deactivations.
2. **All domain events publish to the outbox** via `shared.events.publish(...)`
   in the **same DB transaction** as the state change. Wrap service methods
   in `@transaction.atomic` whenever they both save state and publish.
3. **Views never set status fields directly.** For Phase 1 entities with
   lifecycle (sessions, accounts), state transitions go through service
   methods — never inline assignment in a view.
4. **Inter-system calls go through `shared.events.publish()` + Kafka/System 17
   outbox** — never raw HTTP inside a service. The outbox guarantees at-least-
   once delivery; raw HTTP loses events on partial failure.
5. **All views use `HasPermission` or `has_permission()`** from
   `shared.permissions`. There is no implicit allow.
6. **Standard response envelope**: `success_response()` / `error_response()`
   from `shared.exceptions`. Clients depend on the shape.
7. **Business logic lives in `services.py`, not views.** Views validate
   input, call a service, and return the envelope. Nothing else.
8. **Audit emission must never block the originating action.** Failures fall
   back to a local append-only file and replay on recovery.

---

## 2. Project Structure (Fixed — Do Not Restructure)

```
config/              Django project config (settings, urls, celery)
  settings/
    base.py          Core settings, Celery queues, DRF config
    dev.py           Dev overrides (KEYCLOAK_ENABLED=False, SQLite ok)
    prod.py          Production (Keycloak RS256, Kafka, HSM vault)

shared/              Cross-cutting infrastructure — Phase 1 owns most of this
  auth.py            KeycloakJWTAuthentication (HS256 dev, RS256 prod)
  permissions.py     HasPermission + ROLE_PERMISSION_MAP
  events.py          publish() → OutboxEvent → Kafka
  vault.py           AES-256-GCM encrypt/decrypt (software dev, HSM prod)
  middleware.py      AuditMiddleware — injects request_id, IP
  exceptions.py      nbes_exception_handler + standard envelope
  pagination.py      StandardResultsPagination (page_size=20)
  validators.py      validate_sitting_ref, validate_index_number, validate_ghana_phone
  storage.py         MinIO/S3 object storage helpers

workflow/
  guards.py          FSM guard conditions (Phase 2+ owns most of these)
  signals.py         Post-transition Django signals

apps/
  users/             UserProfile — thin local profile (Keycloak owns auth)
  audit/             AuditEvent + OutboxEvent (Phase 1 — fully owned here)
  committee/         (Phase 2 — out of scope for this skill)
  itembank/          (Phase 3 — out of scope)
  sitting/           (Phase 4 — out of scope)
  registration/      (Phase 5 — out of scope)
  marking/           (Phase 9 — out of scope)
  results/           (Phase 10 — out of scope)
  resit/             (Phase 10 — out of scope)
  cert_trigger/      (Phase 10 — out of scope)
  notifications/     (Phase 1 uses System 21 directly; full fabric is later)
  sla/               (Phase 10 — out of scope; outbox poller lives here)
  reporting/         (Phase 10 — out of scope)
```

Each app under `apps/` follows this layout:

```
app/
  models.py      Data + FSM transitions only
  services.py    Business logic (called from views)
  selectors.py   Read-only queries (querysets, aggregations)
  serializers.py Input validation + representation
  views.py       Validate input → call service → return envelope
  urls.py        URL patterns
  permissions.py App-specific permission classes (if needed)
  filters.py     DjangoFilterBackend filtersets
  events.py      Event name constants for this app
  tasks.py       Celery tasks
  tests/
    test_models.py
    test_services.py
    test_views.py
    test_tasks.py
```

Phase 1 work touches `shared/`, `apps/users/`, `apps/audit/`, `config/settings/`,
and adds Celery Beat entries for the daily hash anchor and outbox poller.
**Do not create new top-level apps for Phase 1 work** — the structure above is
the contract.

---

## 3. Phase 1 Status — What's Done, What's Pending

### Already implemented

- `shared/auth.py` — `KeycloakJWTAuthentication` (HS256 dev mode)
- `shared/permissions.py` — `ROLE_PERMISSION_MAP` + `HasPermission` class
- `apps/audit/models.py` — `AuditEvent` and `OutboxEvent` with SHA-256 chain hash
- `shared/middleware.py` — `AuditMiddleware` injects `request_id`, `ip_address`
- `shared/events.py` — `publish()` writes to outbox
- `shared/exceptions.py` — `nbes_exception_handler` + standard envelope helpers
- `apps/users/models.py` — `UserProfile` (thin local profile; Keycloak owns auth)

### Pending — the Phase 1 backlog

These are blueprint-level descriptions of what needs to exist. **The task-level
breakdown lives in `tasks.md`** — read that for sprint sequencing and acceptance
criteria. The items below tell you *what shape* each piece must take.

1. **Keycloak RS256 JWKS validation** in `shared/auth.py`
   - Currently HS256 with a shared secret (dev only).
   - Prod must fetch JWKS from `settings.KEYCLOAK_JWKS_URL`, cache keys, verify
     RS256, and validate `iss`, `aud`, `exp`, `nbf`.
   - Must support key rotation without restart.
   - Mode switched by `settings.KEYCLOAK_ENABLED`.

2. **`HasPermission.has_permission()`** in `shared/permissions.py`
   - Resolve `request.auth["role"]` → permission set via `ROLE_PERMISSION_MAP`.
   - On deny, **record an `AuditEvent`** with action `AUTHZ_DENIED`,
     entity_type=`"endpoint"`, entity_id=request path, before/after empty.
   - Return generic message to client (no leakage of *why* it was denied).

3. **Session revocation via JTI invalidation**
   - On deactivation or role revocation, write the JTI to a Redis-backed
     blocklist with TTL = remaining token lifetime.
   - `KeycloakJWTAuthentication` checks the blocklist on every request.
   - SLA: revocation effective within **60 seconds** end-to-end.

4. **Bulk user import** — `POST /api/v1/admin/users/import`
   - CSV/Excel upload, schema-validated against a versioned schema.
   - Partial success: valid rows committed, invalid rows reported with
     row-level errors.
   - Each created user gets an invite email via System 21.
   - File hash recorded with the bulk import `AuditEvent`; original retained 7y.

5. **Two-Administrator approval flow** for high-privilege role assignments
   (NBEC Chair, DG, System Administrator)
   - First Administrator submits the assignment → status `pending_approval`.
   - Second Administrator (must be different user) approves → effective.
   - Second approver cannot be the same as the requester (server-side check).
   - Both actions audit-logged with a shared correlation_id.

6. **Daily hash anchor export to System 22** via Celery Beat
   - Task: `apps.audit.tasks.export_daily_hash_anchor`.
   - Schedule: 01:00 UTC daily, queue `sla-monitor`.
   - Compute head hash of yesterday's `AuditEvent` chain, write a
     `DailyHashAnchor` row, publish to System 22 via outbox.
   - Failure pages the on-call (alert via System 21).

7. **Brute-force defence**
   - IP-level throttle: 100 failed logins/min → 15-minute block.
   - IP-level escalation: 1000 failed logins/24h → 24-hour block.
   - Account lockout: 5 consecutive failed logins → 15-minute cooldown,
     user notified by email.
   - Counters in Redis with sliding window; blocks enforced in
     `KeycloakJWTAuthentication` and the login endpoint.
   - Each threshold crossing emits an `AuditEvent` with action
     `BRUTE_FORCE_THROTTLE` or `BRUTE_FORCE_BLOCK`.

8. **`UserProfile` management endpoints**
   - Model exists; views do not. CRUD gated on `users:manage` permission.
   - On create, fan out to Keycloak via System 17 (don't write Keycloak
     directly from the service — go through the outbox).

9. **MFA enforcement gate**
   - All internal roles (everyone except `candidate`) must have
     `mfa_enrolled=True` on their `UserProfile` before any non-auth endpoint
     succeeds. If `mfa_enrolled=False`, return 403 with code `MFA_REQUIRED`.
   - Step-up MFA required for: vault export, Board ratification, results
     publication, role assignment, password reset for another user. Phase 1
     ships the gate; later phases call into it.

---

## 4. RBAC Matrix

Roles come from `request.auth["role"]` (set by `KeycloakJWTAuthentication`).
The matrix below is the **full** consolidated role list across 10A and 10B;
Phase 1 ships the map and the gateway, even where the consuming features
land in later phases.

| Role | Key Permissions |
|---|---|
| `nbec-member` | item:approve, sitting:configure, results:ratify, results:publish:approve, audit:export, committee:manage, resit:exception:grant |
| `nbec-secretariat` | committee:manage, sla:view, reporting:view |
| `item-writer` | item:create |
| `moderator` | item:approve, marking:moderate |
| `examiner` | marking:second_mark |
| `clet-registrar` | registration:eligibility:override, results:publish:approve, cert:trigger, sla:view |
| `candidate` | registration:self, results:view:own, resit:register |
| `invigilator` | (System 10B — check-in, proctoring) |
| `centre-coordinator` | (System 10B — centre operations) |
| `remote-proctor` | (System 10B — proctoring queue) |
| `dti-operations` | system:health:view, integrations:manage |
| `service-desk-agent` | candidate:lookup, ticket:manage |
| `system-administrator` | All admin operations including users:manage, roles:manage |
| `auditor` | audit:export, audit:search |

**Permission check pattern in views:**

```python
from shared.permissions import has_permission

class UserProfileViewSet(ViewSet):
    permission_classes = [IsAuthenticated, has_permission("users:manage")]
```

**Mutually exclusive roles** (server-side rejected on assignment):
- `item-writer` + `moderator` on the same `item`
- `invigilator` + `candidate` at all times (same user)
- `examiner` + `candidate` for the same sitting

**Multiple roles per user** is supported in the data model
(`user_role` is many-to-one on user). The current single-role read from
`request.auth["role"]` is a known limitation flagged in §10.

---

## 5. Audit Pattern

Every service method that mutates state must call:

```python
AuditEvent.record(
    actor_id=request.auth["sub"],          # Keycloak UUID
    action="USER_DEACTIVATED",             # SCREAMING_SNAKE from apps/<app>/events.py
    entity_type="user",
    entity_id=user.id,
    old_state={"status": old_status},
    new_state={"status": user.status},
    request_id=getattr(request, "request_id", None),
    ip_address=getattr(request, "ip_address", None),
)
```

`AuditEvent.record()` automatically:
- Computes the SHA-256 chain hash (`prev_hash || canonical_event_json`).
- Publishes `AuditEventRecorded` to the outbox → System 22.

**Phase 1 standard event names** (define in `apps/audit/events.py` and
`apps/users/events.py`):

```python
# apps/users/events.py
USER_CREATED          = "UserCreated"
USER_UPDATED          = "UserUpdated"
USER_DEACTIVATED      = "UserDeactivated"
ROLE_ASSIGNED         = "RoleAssigned"
ROLE_REVOKED          = "RoleRevoked"
MFA_ENROLLED          = "MfaEnrolled"
MFA_CLEARED_BY_ADMIN  = "MfaClearedByAdmin"
PASSWORD_CHANGED      = "PasswordChanged"
LOGIN_SUCCESS         = "LoginSuccess"
LOGIN_FAILED          = "LoginFailed"
SESSION_REVOKED       = "SessionRevoked"
BULK_IMPORT_COMPLETED = "BulkImportCompleted"
AUTHZ_DENIED          = "AuthzDenied"
BRUTE_FORCE_THROTTLE  = "BruteForceThrottle"
BRUTE_FORCE_BLOCK     = "BruteForceBlock"
TWO_ADMIN_APPROVAL_REQUESTED = "TwoAdminApprovalRequested"
TWO_ADMIN_APPROVAL_GRANTED   = "TwoAdminApprovalGranted"
```

**Why audit emission is async with local-file fallback**: the originating
business action must not fail because System 22 is unreachable. The outbox
relay handles delivery; on outbox failure, emission falls back to a local
append-only file that replays when the outbox is healthy again.

---

## 6. Event Publishing Pattern

For domain events (not audit), use:

```python
from shared.events import publish

publish("UserDeactivated", {"user_id": str(user.id), "reason": reason})
```

`publish()` writes an `OutboxEvent` row in the **same DB transaction**.
Always wrap service methods in `@transaction.atomic` when they both save
state AND publish events. The outbox poller (`apps/sla/tasks.py`, queue
`outbox`) runs every 5 seconds and ships events to Kafka.

Kafka topics are inferred from the event name prefix (`_infer_topic` in
`shared/events.py`). In dev (`KAFKA_ENABLED=False`), events live in the
outbox table only — useful for assertion in tests.

---

## 7. Integration Patterns (Substrate Owned by Phase 1)

Later phases consume these patterns. Get them right here, once.

### 7.1 Outbound calls via System 17

```python
# shared/integrations.py — to be created in Phase 1
def call_system_17(endpoint, payload, *, method="POST", correlation_id=None):
    """
    Signed HTTP to System 17 with:
      - nonce + timestamp + HMAC signature (replay protection)
      - exponential backoff retry (3 attempts, 1s/4s/16s)
      - correlation_id propagation (W3C Trace Context)
      - mutual TLS in prod
    Returns {code, data, correlation_id, retryable} envelope.
    """
```

| Target | Env Var | Pattern |
|---|---|---|
| **System 17** (API Layer) | `SYSTEM_17_URL` | Signed HTTP + replay protection — all outbound inter-system calls |
| **System 22** (Audit) | via Kafka `nbes.audit` | Outbox → Kafka — tamper-evident audit storage |
| **System 21** (Communications) | `SYSTEM_21_URL`, `SYSTEM_21_API_KEY` | Outbound REST — invite emails, lockout notifications, password resets |
| **Keycloak** | `KEYCLOAK_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_JWKS_URL` | Identity provider — JWKS in prod, shared secret in dev |

### 7.2 Inbound webhooks

Phase 1 does not own inbound webhooks (Phase 5 owns the payment webhook
from System 20). Phase 1 ships the **HMAC verification helper** in
`shared/integrations.py` for later phases to call.

### 7.3 Outbox pattern

Every NBES service writes domain events to the `OutboxEvent` table in the
**same DB transaction** as the state change. The outbox relay
(Celery task `apps/sla/tasks.py:relay_outbox`) ships them to Kafka with
at-least-once semantics. Consumers must be idempotent.

### 7.4 Idempotency keys

Every state-mutating API call accepts an optional `Idempotency-Key` header.
Phase 1 ships the middleware that caches the response for 24h keyed on
(actor_id, endpoint, idempotency_key) — later phases just rely on it.

---

## 8. Celery Queues — Phase 1 Allocation

| Queue | Worker | Phase 1 Tasks |
|---|---|---|
| `sla-monitor` | `worker-sla` | `export_daily_hash_anchor` (01:00 UTC daily), `relay_outbox` (every 5s) |
| `notifications` | `worker-general` | Invite emails, lockout notifications, password resets (via System 21) |

Always specify the queue when defining tasks:

```python
@shared_task(queue="sla-monitor", bind=True, max_retries=3)
def export_daily_hash_anchor(self):
    ...
```

Later phases add `marking-high`, `moderation`, `results`, `cert-trigger`,
`vault-integrity` — do not add them in Phase 1.

---

## 9. Coding Patterns — Adding a Phase 1 Feature

When adding a Phase 1 capability (e.g. "implement bulk user import"):

1. **Model** (`apps/users/models.py` or `apps/audit/models.py`) — add fields,
   set `db_table`, add lifecycle field if needed.
2. **Migration** — `python manage.py makemigrations <app>`.
3. **Service** (`services.py`) — business logic; `@transaction.atomic`;
   call `AuditEvent.record()`; call `publish()`; outbound work goes via
   `shared.integrations.call_system_17`.
4. **Serializer** (`serializers.py`) — input validation + representation.
5. **View** (`views.py`) — `permission_classes = [IsAuthenticated, has_permission("users:manage")]`;
   call service; return `success_response(data, status_code)`.
6. **URL** (`urls.py`) — register the view.
7. **Events** (`events.py`) — add the event name constant.
8. **Tests** — model, service, view tests with role-correct JWTs.

---

## 10. Validation Rules — Phase 1 Specific

- **Email**: uniqueness at the DB level; case-insensitive comparison.
- **Password**: minimum 12 chars; mixed case + digits + special; HIBP check
  before accept; reuse blocked for last 12 passwords.
- **Account lockout**: 5 consecutive failures → 15-minute cooldown; user
  notified by email.
- **MFA factors**: TOTP (RFC 6238), WebAuthn / FIDO2 (preferred for internal
  roles), SMS OTP (fallback only — never default).
- **MFA cannot be self-disabled** for internal roles. Only an Administrator
  can clear an MFA enrolment; the action is audited (`MFA_CLEARED_BY_ADMIN`)
  and forces re-enrolment on next login.
- **Mutually exclusive roles**: server-side rejection on assignment; the API
  returns 400 with code `ROLE_MUTUAL_EXCLUSION_VIOLATION`.
- **Two-Administrator approval**: required for NBEC Chair, DG, System
  Administrator role assignments. Second approver ≠ requester (enforced
  server-side, not just UI).
- **Invite link**: single-use, 7-day expiry.
- **Session revocation**: effective end-to-end within 60 seconds of role
  revocation or account deactivation.
- **403 responses**: generic message; specifics live only in the audit log.
- **Audit retention**: 15 years (per NBE-N07).

Cross-cutting validators already in `shared/validators.py`:
`validate_sitting_ref`, `validate_index_number`, `validate_ghana_phone`.
Phase 1 does not modify these.

---

## 11. Testing Strategy — Phase 1

**Unit tests** (`tests/test_models.py`, `tests/test_services.py`):
- Audit chain hash continuity — given two consecutive events, the second
  event's `prev_hash` equals the first event's `hash`.
- `HasPermission` deny path emits an `AUTHZ_DENIED` audit event.
- Brute-force counters increment correctly and reset after the window.
- Two-Administrator approval rejects same-user approval.

**Integration tests** (`tests/test_views.py`):
- Use `APIClient` with a JWT in the correct role.
- 401 on missing/expired token.
- 403 on wrong role — with audit entry assertion.
- Standard envelope shape on success and error.
- 60-second session revocation: assign role → revoke → assert 403 on next
  call (test uses time-travel rather than real sleep).

**Test JWT helper pattern** (place in `shared/test_utils.py`):

```python
import jwt
from django.conf import settings

def make_jwt(role="nbec-member", sub="test-sub-uuid", jti=None):
    return jwt.encode(
        {
            "sub": sub,
            "email": "test@example.com",
            "role": role,
            "roles": [role],
            "jti": jti or "test-jti",
        },
        settings.JWT_SECRET_KEY, algorithm="HS256",
    )
```

**Acceptance criteria (Given/When/Then)** — trace every test back to a SRS
requirement ID (e.g. `# SRS-REQ-F000-02` in the docstring).

---

## 12. Phase 1 Risks and Open Questions

| Risk | Impact | Mitigation |
|---|---|---|
| Keycloak RS256 JWKS not implemented | Auth broken in production | Implement before any production deployment; gate on `KEYCLOAK_ENABLED` |
| Audit emission blocks business actions on System 22 outage | Cascade failure across all features | Async emission + local-file fallback + replay on recovery |
| Session revocation slower than 60s SLA | F000-02 acceptance fails | Redis JTI blocklist + check on every auth call |
| HIBP availability for password check | Onboarding blocked | Cache HIBP responses; on outage, fall back to local breach list |
| Multiple roles per user not supported | `request.auth["role"]` is single-valued | Product decision required before Phase 2 ships; Phase 1 data model already supports many-to-one `user_role` |
| Two-Administrator approval bypass via concurrent requests | High-privilege escalation | DB unique constraint on (request_id, approver_id) and explicit check `approver_id ≠ requester_id` |
| Brute-force counter race conditions | Throttle ineffective under load | Use Redis atomic INCR with TTL; do not implement in Python |
| Daily hash anchor failure overnight | Tamper-evidence gap | Page on-call on first failure; second consecutive failure blocks all writes |

---

## 13. Phase 1 Out of Scope — Do Not Build Here

These belong to later phases. Adding them in Phase 1 expands the blast radius
of any auth/RBAC change and slows the foundation.

- Committee / NBEC management (Phase 2 — `apps/committee/`)
- Item bank, vault, peer review (Phase 3 — `apps/itembank/`)
- Sitting configuration, blueprint, T-30 lock (Phase 4 — `apps/sitting/`)
- Candidate registration, NLEMS gate, index numbers (Phase 5 — `apps/registration/`)
- CBT response/attendance ingestion (Phase 6–8 — System 10B handoff)
- AI scoring, moderation, reconciliation (Phase 9 — `apps/marking/`)
- Results normalisation, ratification, publication (Phase 10 — `apps/results/`)
- §73 attempt counter, NBEC exceptions (Phase 10 — `apps/resit/`)
- Certificate trigger (Phase 10 — `apps/cert_trigger/`)
- 21-day publication SLA, 1-hour cert SLA (Phase 10 — `apps/sla/` business logic)

Phase 1 **does** ship the empty app skeletons (`models.py` placeholders,
`urls.py` stubs) and the Role Dashboard Skeletons so the IA is stable — but
not the features.

---

## 14. Reference

- **Task-level breakdown**: `tasks.md` (sprint sequencing, acceptance criteria
  per ticket, demo targets). This SKILL.md is the blueprint; tasks.md is the
  to-do list.
- **SRS requirement IDs**: REQ-F000-01 through REQ-F000-06.
- **Legislative basis for the wider system**: §§65–73 Legal Practitioners Act
  (Ghana). Phase 1 itself has no direct statutory clause — it is the
  enabling substrate.
