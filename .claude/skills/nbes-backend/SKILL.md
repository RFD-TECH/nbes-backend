---
name: nbes-backend
description: >
  Backend blueprint and implementation guide for the NBES Core Platform (System 10A).
  Use this skill when implementing any feature in the nbes-backend service.
  Covers system architecture, all 13 Django apps, 10 implementation phases, RBAC,
  audit, integrations, validation rules, testing strategy, and risks.
---

# NBES Core Platform — Backend Blueprint

**System**: 10A — National Bar Examination System (NBES) Core Platform
**Framework**: Django 5 / DRF · PostgreSQL 16 · Celery · Redis · Kafka
**Legislative basis**: §§65–73 of the Legal Practitioners Act (Ghana)
**Port**: 8003 (direct) / 8000 via API Gateway (System 17)

---

## 1. Context Before You Write a Line

This is a high-stakes, legally auditable examination system. Every decision it records can be
cited in judicial review. Follow these invariants in every change:

1. Every state change must emit an `AuditEvent` via `AuditEvent.record(...)`.
2. All domain events must publish to the outbox via `shared.events.publish(...)`.
3. FSM transitions are the **only** way to change status fields — never set them directly.
4. Vault operations (Item content) must go through `shared.vault`.
5. Inter-system calls go through `shared.events.publish()` + Kafka/System 17 outbox — never raw HTTP in services.
6. All views must use `HasPermission` or `has_permission()` from `shared.permissions`.
7. Standard response envelope: `success_response()` / `error_response()` from `shared.exceptions`.
8. Business logic lives in `services.py`, not views.

---

## 2. Project Structure

```
config/              Django project config (settings, urls, celery)
  settings/
    base.py          Core settings, Celery queues, DRF config
    dev.py           Dev overrides (KEYCLOAK_ENABLED=False, SQLite ok)
    prod.py          Production (Keycloak RS256, Kafka, HSM vault)

shared/              Cross-cutting infrastructure — import freely from any app
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
  guards.py          FSM guard conditions (many have TODO stubs — see §6)
  signals.py         Post-transition Django signals
  viewflow/
    ratification.py  Board ratification flow (django-viewflow)
    vault_export.py  Multi-party vault export flow

apps/
  users/             UserProfile — thin local profile (Keycloak owns auth)
  committee/         NBEC members, meetings, minutes, conflict-of-interest
  itembank/          Item authoring, vault, peer review, paper construction
  sitting/           Exam cycle config, blueprint, T-30 lock
  registration/      Candidate registration, NLEMS gate, index numbers
  marking/           AI scoring, borderline flagging, moderation, reconciliation
  results/           Normalisation, Board ratification, publication
  resit/             Attempt counter, §73 limit, NBEC exceptions
  cert_trigger/      Trigger System 14 on confirmed PASS (1-hour SLA)
  notifications/     Notification orchestration → System 21
  audit/             AuditEvent + OutboxEvent models (fully implemented)
  sla/               SLA monitor — 21-day publication, cert trigger deadlines
  reporting/         KPI aggregation, audit exports
```

Each app follows this structure:
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

---

## 3. RBAC Matrix

Roles come from `request.auth["role"]` (set by `KeycloakJWTAuthentication`).

| Role | Key Permissions |
|---|---|
| `nbec-member` | item:approve, sitting:configure, results:ratify, results:publish:approve (via clet-registrar path), audit:export, committee:manage, resit:exception:grant |
| `nbec-secretariat` | committee:manage, sla:view, reporting:view |
| `item-writer` | item:create |
| `moderator` | item:approve, marking:moderate |
| `examiner` | marking:second_mark |
| `clet-registrar` | registration:eligibility:override, results:publish:approve, cert:trigger, sla:view |
| `candidate` | registration:self, results:view:own, resit:register |
| `invigilator` | (System 10B operations — check-in, proctoring) |
| `system-administrator` | All admin operations |
| `auditor` | audit:export |

Permission check in views:
```python
from shared.permissions import has_permission
permission_classes = [IsAuthenticated, has_permission("item:approve")]
```

Every 403 must be audit-logged. The TODO in `shared/permissions.py` `HasPermission.has_permission()` needs implementation.

---

## 4. Audit Pattern

Every service method that mutates state must call:
```python
AuditEvent.record(
    actor_id=request.auth["sub"],      # Keycloak UUID
    action="ITEM_APPROVED",            # SCREAMING_SNAKE from apps/<app>/events.py
    entity_type="item",
    entity_id=item.id,
    old_state={"status": old_status},
    new_state={"status": item.status},
    request_id=getattr(request, "request_id", None),
    ip_address=getattr(request, "ip_address", None),
)
```

`AuditEvent.record()` automatically:
- Computes the SHA-256 chain hash
- Publishes `AuditEventRecorded` to the outbox → System 22

---

## 5. Event Publishing Pattern

For domain events (not audit), use:
```python
from shared.events import publish
publish("ItemApproved", {"item_id": str(item.id)})
```

`publish()` writes an `OutboxEvent` row in the **same DB transaction**. Always wrap service
methods in `@transaction.atomic` when they both save state AND publish events.

Kafka topics are inferred from the event name prefix (`_infer_topic` in `shared/events.py`).
In dev (`KAFKA_ENABLED=False`), events live in the DB only.

---

## 6. FSM Guard TODOs (Priority Implementation)

The following guards in `workflow/guards.py` have placeholder `return True` stubs and must be
implemented before their respective features are shippable:

| Guard | App | What needs implementing |
|---|---|---|
| `has_valid_mcq_config` | itembank | Validate `metadata["options"]` — exactly 1 correct answer for MCQ |
| `has_sufficient_panel_votes` | itembank | Count `ItemPanelVote` records — 2 of 3 votes required |
| `no_active_conflict` | itembank / marking | Check `ConflictDeclaration` for actor vs item/script subject |
| `is_moderation_panel_member` | itembank | Check actor role from thread-local request |
| `nlems_eligibility_verified` | registration | Reads `candidate.eligibility_status == "eligible"` —implemented |
| `payment_confirmed` | registration | Reads `registration.payment_confirmed` —implemented |
| `ai_scoring_complete` | marking | Reads `marking_decision.ai_mark` —implemented |
| `is_borderline` | marking | Reads `script.borderline_flagged` — set by borderline engine task |
| `no_moderator_conflict` | marking | Check `ConflictDeclaration` for moderator vs candidate |
| `has_justification` | marking | Word-count check on `marking_decision.justification` —implemented |
| `reconciliation_required` | marking | Reads `script.reconciliation_required` — set by moderation service |
| `below_attempt_limit` | resit | Check `AttemptCounter` for candidate + paper vs §73 limit |

---

## 7. Implementation Phases

### Phase 1 — Authentication, RBAC & Foundation
**SRS Ref**: REQ-F000 (System 10A and System 10B) | **Status**: Not started (boilerplate scaffolding only)

#### 1.1 Objective & Strategic Context

Phase 1 builds the floor on which every other NBES and CBT module stands. It produces three things:
authenticated users with the right roles, an audit substrate that makes every subsequent action
defensible, and the integration patterns (System 17 for API, System 22 for audit, NLEMS for identity)
that every later phase will reuse rather than re-invent.

This is also where the unified identity model across System 10A (NBEC, examiners, candidates,
registrar) and System 10B (invigilators, centre coordinators, proctors, DTI operations) is established.
The Bar Examination is one event — its identity model must not split across the two systems.

**Why Phase 1 cannot be merged into later phases:**
- Every SRS functional requirement opens with an RBAC clause. There is no NBEC management
  without an Administrator who can create the Chair account; no item authoring without role-restricted access.
- The audit substrate is mentioned in every functional requirement. Retrofitting auditability after
  features ship is more expensive than building features that emit audit events from day one.
- MFA enforcement for internal roles is mandatory per F000-03. Adding MFA late forces every
  downstream feature to be re-tested under the higher auth bar.

#### 1.2 Core Module Breakdown

##### 1.2.1 User Account Management (identity-service)

The Administrator's surface for creating, editing, deactivating and (logically) deleting accounts
across every role in 10A and 10B.

**Requirements:**
- Account actions per role: Create, Edit, Deactivate, logical Delete (with retention).
- Required fields on create: first_name, last_name, email (unique), role(s), effective_date, optional id_number.
- Only the Administrator role can perform these actions; the surface itself is RBAC-gated.
- Cannot delete an account with open active assignments (e.g. an Examiner with scripts in their queue).
- New accounts trigger an invite email with a single-use first-time-login link; link expires in 7 days.
- Deactivation notifies the user via email; active sessions terminated within 60 seconds.
- Every account action audit-logged with actor, target, before/after, timestamp, IP.

**Pending implementation:**
- `apps/users/services.py` — `create_user()`, `edit_user()`, `deactivate_user()`, `delete_user()`
- `apps/users/serializers.py` — `UserCreateSerializer`, `UserEditSerializer`
- `apps/users/views.py` — `UserAdminViewSet`
- Invite email dispatch via System 21 (notification-bridge)
- Active-assignment check before deletion

##### 1.2.2 Role Assignment & Revocation (role-service)

The permission control plane. The role matrix is configurable; effective changes propagate within
60 seconds.

**Roles supported (consolidated across 10A and 10B):**
NBEC Member, NBEC Secretariat, Item Writer, Moderator, Examiner, Candidate, CLET Registrar,
Invigilator, Centre Coordinator, Remote Proctor, DTI Operations, Service Desk Agent, Auditor,
System Administrator.

**Requirements:**
- Each role mapped to a set of permissions; permissions mapped to API scopes and UI capabilities.
- Effective date immediate or future-dated; assignments scheduled in advance.
- Mutually exclusive roles cannot coexist on the same user — e.g. Item Writer and Moderator on
  the same item; Invigilator and Candidate at all times.
- Revoke with optional reason; effective immediately or scheduled.
- Active sessions for affected users are re-evaluated within 60 seconds — UI controls disappear,
  API tokens become unauthorised on next call.
- High-privilege role assignments (NBEC Chair, DG, Administrator) require two-Administrator approval.

**Pending implementation:**
- `apps/users/services.py` — `assign_role()`, `revoke_role()`, `check_mutual_exclusion()`
- Role-change event publishing via outbox
- Two-Administrator approval flow for high-privilege roles
- Session re-evaluation within 60 seconds (JTI invalidation + gateway cache bust)

##### 1.2.3 Multi-Factor Authentication & Strong Password Policy

Authentication hardening for the entire platform.

**MFA requirements:**
- MFA required for ALL internal roles (NBEC, Item Writers, Moderators, Examiners, Registrar,
  all centre operations roles, Administrators, Auditors).
- MFA optional but recommended for candidates; required for any high-stakes candidate action
  (results view, withdrawal, remark request, profile change on locked fields).
- MFA factors supported: TOTP (RFC 6238), WebAuthn / FIDO2 (preferred for internal roles),
  SMS OTP (fallback only — fragile against SIM-swap).
- Step-up authentication required for sensitive actions: vault export, override approval, Board
  ratification signing, role assignment, results publication.

**Password policy:**
- Minimum 12 characters, mixed case, digits, special characters.
- Checked against known-compromised-password list (Have I Been Pwned API).
- Reuse blocked for last 12 passwords.
- Account lockout after 5 consecutive failed logins with 15-minute cooldown.

**Pending implementation:**
- MFA enrolment/challenge flow in `shared/auth.py`
- TOTP (RFC 6238) verification
- WebAuthn / FIDO2 registration and assertion
- Password history tracking (last 12)
- HIBP password check integration
- Step-up MFA gate decorator for sensitive service methods

##### 1.2.4 Bulk User Import

The fast onboarding path used at the start of each sitting cycle and for centre cohorts.

**Requirements:**
- CSV/Excel upload with schema validation. Schema versioned and published in Admin documentation.
- Partial-success handling — valid rows committed, invalid rows reported with row-level errors.
- Each created user receives an invite email automatically.
- File hash recorded with the bulk import audit entry; original file retained for 7 years.
- Bulk role assignment supported as a separate operation (existing users only).
- Used heavily by 10B to onboard invigilator cohorts before sittings.

**Pending implementation:**
- `apps/users/services.py` — `bulk_import_users()`, `bulk_assign_roles()`
- `apps/users/serializers.py` — `BulkImportSerializer`
- `POST /api/v1/admin/users/import` endpoint
- CSV/Excel parsing with row-level validation
- File hash computation + 7-year retention in object storage

##### 1.2.5 RBAC Enforcement at UI and API

Defence in depth — UI controls disappear for forbidden actions, but the API is the source of truth.

**Requirements:**
- API: every endpoint requires an authenticated, authorised principal. Authorisation is evaluated
  at every call — there is no implicit caching of allow decisions.
- Permission changes apply within 60 seconds across UI and API.
- API responses for forbidden calls: HTTP 403 with error code `AUTHZ_DENIED` and a generic
  message (no information leakage about why).
- Every 403 logged with actor, attempted action, resource, timestamp, IP, user agent.
- Service-to-service calls use service principals with short-lived JWTs; the same RBAC model applies.

**Implementation required:**
- `shared/permissions.py` — `HasPermission` class with 403 audit event recording
- `ROLE_PERMISSION_MAP` with full role-to-permission mapping
- Redis cache (60s TTL) for role→permission lookups in production
- Service principal JWT validation path

##### 1.2.6 Unauthorised Access Logging & Brute-Force Defence

Active defence against credential stuffing, brute force, and role-escalation attempts.

**Requirements:**
- Failed-authentication, expired-session, role-mismatch, and forbidden-API events all logged
  and forwarded to System 22.
- Brute-force pattern detection: 100 failed logins from a single IP → 15-minute throttle;
  1000 in 24h → 24-hour block.
- Anomaly detection on geo and device fingerprint — unusual logins prompt step-up MFA.
- Security event taxonomy aligned with the System 22 SIEM schema (severity, category, indicators).
- Daily security-event summary delivered to the Security Officer and DTI Operations.

**Pending implementation:**
- `login_attempt` model (see data model below)
- IP-level throttle middleware or DRF throttle class
- Redis-backed failed-login counter per IP
- Celery task for daily security-event summary generation
- Anomaly detection on geo/device fingerprint (COULD — defer to Phase 10 if time-pressed)

##### 1.2.7 Audit Substrate (Cross-cutting)

The append-only audit log that every later phase will write to. Phase 1 produces the platform;
later phases produce the content.

**Requirements:**
- Every state change in the system emits an audit event: actor, action, target, before, after,
  timestamp, correlation_id.
- Append-only storage; rows cannot be updated or deleted.
- Daily integrity chain: each day's events hashed; hash chained to prior day; daily anchor
  exported to System 22's tamper-evident store.
- Audit search API gated by Auditor / DG / Administrator roles; results scoped by RBAC.
- Audit log retention: 15 years (per NBE-N07 and SRS Section 7 constraints).
- Audit events emitted asynchronously to a high-priority queue; an audit-store outage degrades
  to local append-only file with replay on recovery.

**Implementation required:**
- `AuditEvent` model with `record()` classmethod and SHA-256 chain hash
- `OutboxEvent` transactional outbox model
- `poll_outbox` Celery task for outbox relay
- `daily_hash_anchor` model + daily Celery Beat task (export by 01:00 UTC; failure pages on-call)
- Audit search API: `GET /api/v1/audit/search` with filterable fields
- Hash-chain verification endpoint: `GET /api/v1/audit/chain/{date}`
- Local-file fallback when audit-store is unreachable + replay-on-recovery
- Audit export functionality (Auditor / DG / Administrator roles only)
- DB trigger (migration RunSQL) to prevent UPDATE/DELETE on audit_event table

##### 1.2.8 Integration Patterns (System 17 & System 22)

The reusable integration substrate that later phases consume rather than re-implement.

**Requirements:**
- All inter-system calls go through System 17 (API Layer) with signed payloads and replay
  protection (nonce + timestamp + signature).
- Mutual TLS for partner systems (NLEMS, System 14, System 20, System 18, System 10B↔10A).
- Outbox pattern in every NBES service: domain events written to an outbox table in the same
  DB transaction as the state change; an outbox relay publishes them to the event bus reliably.
- Idempotency keys on every state-mutating call; safe retry semantics.
- Standard error envelope: `{ code, message, correlation_id, retryable }`.
- All audit events flow to System 22 via the same audit substrate (1.2.7).

**Pending implementation:**
- `shared/integrations.py` — `call_system_17()` with signed payloads, replay protection,
  exponential backoff retry, correlation_id logging
- Mutual TLS configuration for production
- Idempotency key middleware / decorator
- Notification-bridge (light) for invite emails, lockout notifications, password-reset emails
  (calls System 21 directly with templated bodies; full notification fabric lands in Phase 9)

##### 1.2.9 Role Dashboard Skeletons

Each role's home page, ready to receive feature panels in later phases. Phase 1 ships the
skeletons so the navigation IA is stable from day one.

**Dashboard panels per role:**
- **NBEC Member**: meeting agenda, pending approvals, conflict declarations, audit-trail viewer
- **NBEC Secretariat**: committee operations, candidate registration desk, exception queue
- **Item Writer**: my items, drafts, peer-review feedback
- **Moderator**: review queue, panel decisions, item-search
- **Examiner**: marking queue, borderline review queue
- **Candidate**: registration, payment, slip, results, remarking
- **CLET Registrar**: override queue, ratification dashboard, certificate trigger panel
- **Invigilator / Centre Coordinator**: centre operations, candidate check-in, proctoring queue
- **Administrator**: users, roles, integrations, audit, system health
- **Auditor**: audit-trail search, hash-chain verification, export

#### 1.3 Data Model (Phase 1 — IAM-Aligned)

> **Architecture note:** NBES delegates authentication, MFA, password policy, and session
> management to the central IAM (System 19 / Keycloak). The data model below reflects only
> what NBES owns locally. Password, MFA, session, and login_attempt tables live in IAM.

```
user_profile        — id (UUID PK), keycloak_sub (UUID, unique, nullable),
                      email (unique, case-insensitive), first_name, last_name,
                      status (pending_invite/active/inactive),
                      metadata (JSON — national_id, department, phone, etc.),
                      created_by (FK user_profile), deactivated_at,
                      created_at, updated_at

user_role           — id (UUID), user_id (FK), role_id (FK), effective_from, effective_to,
                      assigned_by (FK user_profile), revoked_at, revoke_reason,
                      created_at
                      UNIQUE(user, role) WHERE revoked_at IS NULL

role_change_event   — id (UUID), user_id (FK), role_id (FK),
                      change_type (assign/revoke), actor_id (FK), reason,
                      occurred_at
                      (immutable event-sourced log — current roles are a projection)

role                — id (UUID), name (unique), description, is_internal (bool),
                      is_active (bool), version (int), created_at, updated_at

permission          — id (UUID), codename (unique), description, created_at

role_permission     — id (UUID), role_id (FK), permission_id (FK), granted_by (UUID),
                      created_at.  UNIQUE(role, permission)

mutual_exclusion    — id (UUID), role_a (FK), role_b (FK), description, created_at
                      UNIQUE(role_a, role_b)

role_assignment_approval — id (UUID), user_id (FK), role_id (FK),
                      requested_by (FK), approved_by (FK nullable),
                      approved_at, status (pending/approved/rejected),
                      effective_from, reject_reason, created_at

audit_event         — (already implemented — append-only, SHA-256 chain hash)

daily_hash_anchor   — (already implemented — date, head_hash, exported_to_s22_at)

security_event      — (already implemented — SIEM-aligned taxonomy)

outbox_event        — (already implemented — transactional outbox)

bulk_import_record  — id (UUID), file_hash (SHA-256), file_ref (object storage path),
                      total_rows, success_count, error_count, error_report (JSON),
                      imported_by (FK user_profile), created_at
```

**Note:** The existing `UserProfile` in `apps/users/models.py` is boilerplate only. The full
`user_profile` model above replaces it with status, metadata, and lifecycle fields.
No `password_hash`, `mfa_enrolment`, `login_attempt`, or `session` tables — those live in IAM.

#### 1.4 API Endpoints (Phase 1 — IAM-Aligned)

> **Removed:** `auth/login`, `auth/mfa`, `auth/refresh`, `auth/logout` — these are IAM
> (System 19) endpoints accessed via the API Gateway (System 17). NBES does not implement
> authentication endpoints.

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| `GET` | `/api/v1/admin/users` | `users:manage` | List user profiles (paginated, filtered) |
| `POST` | `/api/v1/admin/users` | `users:manage` | Create user profile (provisions in IAM) |
| `GET` | `/api/v1/admin/users/{id}` | `users:manage` | Get user profile detail |
| `PATCH` | `/api/v1/admin/users/{id}` | `users:manage` | Edit / deactivate / delete |
| `POST` | `/api/v1/admin/users/import` | `users:import` | Bulk import via CSV/Excel |
| `POST` | `/api/v1/admin/users/bulk-assign-roles` | `users:manage` | Bulk role assignment |
| `GET` | `/api/v1/admin/users/{id}/roles` | `users:manage` | List user's active roles |
| `POST` | `/api/v1/admin/users/{id}/roles` | `users:manage` | Assign role |
| `DELETE` | `/api/v1/admin/users/{id}/roles/{role}` | `users:manage` | Revoke role |
| `GET` | `/api/v1/admin/role-approvals` | `rbac:manage` | List pending high-privilege approvals |
| `POST` | `/api/v1/admin/role-approvals/{id}/approve` | `rbac:manage` | Approve role assignment |
| `POST` | `/api/v1/admin/role-approvals/{id}/reject` | `rbac:manage` | Reject role assignment |
| `GET` | `/api/v1/admin/rbac/roles` | `rbac:manage` | List roles + permission matrix |
| `PUT` | `/api/v1/admin/rbac/roles/{id}/permissions` | `rbac:manage` | Update role permissions |
| `GET` | `/api/v1/audit/search` | `audit:search` | Search audit trail |
| `GET` | `/api/v1/audit/chain/{date}` | `audit:verify` | Hash-chain proof for a date |
| `GET` | `/api/v1/audit/export` | `audit:export` | Streamed NDJSON export |
| `GET` | `/api/v1/me` | Authenticated | Current user profile + effective permissions |

#### 1.5 End-to-End Workflows

**1.6.1 New internal user onboarding:**
1. Administrator creates the user with role(s) and effective date.
2. Invite email dispatched with single-use first-time-login link (7-day expiry).
3. User clicks the link, sets a password meeting policy, configures MFA factor (WebAuthn preferred, TOTP fallback).
4. On first successful login, account moves to Active; audit entry recorded.
5. Permissions resolved on first API call; UI scaffolding for the assigned role renders.

**1.6.2 Role change propagation:**
1. Administrator revokes a role from a user.
2. Permission service publishes a role-change event.
3. Gateway invalidates the user's cached permission set within 60 seconds.
4. Next UI call: forbidden controls disappear; next API call: 403 with audit entry.

**1.6.3 Failed-login throttling and escalation:**
1. Login attempt with bad credentials; failure counter increments.
2. At 5 consecutive failures, account locked for 15 minutes; user notified by email.
3. From the same IP, when failed attempts across all accounts reach 100/min, IP-level throttle engages (15 min).
4. If sustained 1000 failures in 24h, IP added to a 24-hour block; Security Officer notified.

#### 1.6 Validation Rules (Phase 1)

- Email uniqueness enforced at the database level; case-insensitive comparison.
- Password validated against policy AND against the known-compromised list before acceptance.
- MFA cannot be disabled for internal roles via self-service; only an Administrator can clear an
  MFA enrolment, and the action is audited and triggers re-enrolment on next login.
- Roles flagged as mutually exclusive cannot be assigned together; the API blocks the second
  assignment with a clear error code.
- High-privilege role assignments require a recorded second Administrator approval before becoming effective.
- Audit emission must not block the originating action — emission is async with a local-file
  fallback if the platform is unreachable.
- Daily hash anchor must be exported to System 22 by 01:00 UTC; failure pages the on-call.

#### 1.7 Architecture Recommendations

- Centralise the authorisation decision in the authz-gateway. Services receive a signed claims
  payload and a permission decision; they should not re-implement authorisation logic.
- Use WebAuthn / FIDO2 for internal MFA. SMS OTP is fragile against SIM-swap and should be
  fallback only — not default.
- Audit emission is non-blocking and uses an outbox table in the originating service to guarantee
  at-least-once delivery.
- Adopt one standard correlation-id propagation pattern (W3C Trace Context). Every later phase
  benefits from it being uniform from day one.
- Make the role/permission matrix versioned and exportable. Future audits will ask 'what
  permissions did this user have on date X?' — answerable only if the matrix is versioned.

#### 1.8 Sprint Goals (IAM-Aligned)

**Sprint 1.1 — IAM Bridge & User Profile Store (Week 1):**
- Expand `UserProfile` model with lifecycle states, metadata, created_by.
- Create `UserRole` join table with effective dates and `RoleChangeEvent` event sourcing.
- Expand IAM bridge (`keycloak_admin.py`) with create/deactivate/assign user functions.
- Admin User Console MVP (`POST/PATCH/GET /api/v1/admin/users`, role assignment).
- Unified `GET /api/v1/me` endpoint.

**Sprint 1.2 — Roles, Permissions & RBAC Gateway (Week 2):**
- Full permission codename catalog (30+ codenames, 15 roles).
- Mutual-exclusion rules model and enforcement.
- Two-admin approval flow for high-privilege roles.
- Step-up MFA policy enforcement (checking gateway `x-acr` / `x-mfa-verified` headers).
- Bulk import flow for centre cohorts (`POST /api/v1/admin/users/import`).
- Update RBAC resolver to use `UserRole` table as authoritative source.

**Sprint 1.3 — Integration Polish, Dashboards & Hardening (Week 3):**
- Consolidate duplicate System 17 clients and dashboard implementations.
- Add missing 10B role dashboard panels (Remote Proctor, DTI Ops, Service Desk, DG).
- Verify append-only DB trigger on AuditEvent.
- Fix request_id → correlation_id threading.
- Machine-token authentication path for service-to-service calls.
- Wire notification bridge for profile provisioning confirmations.

#### 1.9 Implementation Priorities

| Priority | Module / Capability | Rationale |
|----------|-------------------|-----------|
| **MUST** | Identity, MFA, password policy | Every later phase depends on authenticated users |
| **MUST** | Role/permission matrix + RBAC gateway | Every SRS functional req starts with an RBAC clause |
| **MUST** | Audit substrate + daily hash chain | Required by NBE-N02, NBE-F01-05, REQ-F000 audit trail |
| **MUST** | Unauthorised access logging + throttling | REQ-F000-06 acceptance criterion |
| **MUST** | System 17 / System 22 integration patterns | Reused by every later phase |
| **SHOULD** | Bulk import | Required at sitting onboarding peaks |
| **SHOULD** | Role Dashboard Skeletons | Stable IA accelerates Phase 2-10 UI delivery |
| **COULD** | Anomaly-detection / geo-fingerprint | Defer to Phase 10 hardening if time-pressed |
| **COULD** | Self-service WebAuthn re-enrolment | Admin-mediated path acceptable at launch |

#### 1.10 Acceptance Criteria

- **F000-01**: Given an Administrator creates a new Examiner account with valid data, when the
  action is saved, then the account exists with the Examiner role and an invite email is sent
  within 5 minutes.
- **F000-02**: Given I revoke a role from a user, when the change is saved, then the user loses
  access to role-restricted screens within 60 seconds.
- **F000-03**: Given an internal user attempts a high-stakes action without MFA, when the request
  is made, then the action is gated until MFA is satisfied.
- **F000-04**: Given a 200-row bulk import contains 5 invalid rows, when the import completes,
  then 195 users are created and a row-level error report is produced.
- **F000-05**: Given an Item Writer attempts to call the results-publish API, when the call is made,
  then the request is denied with HTTP 403 `AUTHZ_DENIED` and an audit entry is recorded.
- **F000-06**: Given an attacker triggers 100 failed logins from a single IP, when the threshold is
  reached, then the IP is throttled for 15 minutes and a security event is logged in System 22.
- **F000-07**: Given the daily hash-anchor job runs, when the audit chain is exported to System 22,
  then independent verification with the published public key succeeds for every day in the
  retention window.

#### 1.11 Backend Services (Phase 1 — IAM-Aligned)

| Service | Responsibility |
|---------|---------------|
| `iam-bridge` | Typed client for IAM admin API (create/deactivate/assign user) with retry, idempotency, audit logging. IAM owns auth, MFA, sessions, passwords. |
| `profile-store` | Local user profile CRUD with lifecycle states, metadata, created_by tracking |
| `role-service` | Role/permission matrix, UserRole join table, mutual-exclusion rules, two-approver flow, role-change events |
| `authz-gateway` | Central policy decision point (RBAC + step-up MFA enforcement); emits decisions to audit |
| `audit-platform` | Append-only event store, daily hash chain, integrity job, search API (already implemented) |
| `integration-substrate` | System 17 client, outbox-relay, standard error envelope (already implemented) |
| `notification-bridge` | Profile provisioning confirmations via System 21 (light — full fabric in Phase 9) |

#### 1.12 Screens, Dashboards & UI Inventory

| Surface | Audience | Key Screens |
|---------|----------|-------------|
| Login & Auth | All users | Login · MFA challenge · WebAuthn registration · Password reset · Account recovery |
| Admin User Console | System Administrator | User list · Create / Edit · Bulk import · Activity · Sessions · Reset MFA |
| Role & Permission Admin | System Administrator | Role matrix · Permission editor · Mutual-exclusion rules · Two-approver queue |
| Security Operations Console | Security Officer / DPO | Failed-auth dashboard · Throttle / block list · Anomaly review · Daily summary |
| Audit Search | Auditor / DG | Filterable audit search · Correlation-id viewer · Hash-chain verifier · Export |
| Role Dashboard Skeletons | Every role | Empty-state panels ready for Phase 2-10 feature delivery |

#### 1.13 Sprint Demonstration Target

1. Administrator logs in (MFA prompt) and creates an NBEC Member account.
2. New member receives an invite email, sets a password (compromised-password check enforced), and registers WebAuthn.
3. Administrator bulk-imports a 50-row Invigilator cohort; 48 succeed, 2 are reported with row-level errors.
4. Administrator attempts to assign an Item Writer + Moderator role to the same user; system blocks the second assignment.
5. An Item Writer logs in and attempts to call the results-publish API — receives HTTP 403; an audit entry appears in the Auditor's search within seconds.
6. Demo viewer simulates 100 bad-credential attempts from a single IP; the IP is throttled, the Security Operations Console shows the event, and System 22 receives the security alert.
7. Auditor opens the audit-chain viewer for yesterday, exports the proof, and verifies it externally with the published public key.

---