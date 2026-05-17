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
| `nlems_eligibility_verified` | registration | Reads `candidate.eligibility_status == "eligible"` — ✅ implemented |
| `payment_confirmed` | registration | Reads `registration.payment_confirmed` — ✅ implemented |
| `ai_scoring_complete` | marking | Reads `marking_decision.ai_mark` — ✅ implemented |
| `is_borderline` | marking | Reads `script.borderline_flagged` — set by borderline engine task |
| `no_moderator_conflict` | marking | Check `ConflictDeclaration` for moderator vs candidate |
| `has_justification` | marking | Word-count check on `marking_decision.justification` — ✅ implemented |
| `reconciliation_required` | marking | Reads `script.reconciliation_required` — set by moderation service |
| `below_attempt_limit` | resit | Check `AttemptCounter` for candidate + paper vs §73 limit |

---

## 7. Implementation Phases

### Phase 1 — Authentication, RBAC & Foundation
**SRS Ref**: REQ-F000 | **Status**: Partially complete (shared/auth.py, shared/permissions.py, apps/audit/)

**What's done:**
- `KeycloakJWTAuthentication` (dev HS256 mode; prod JWKS TODO)
- `ROLE_PERMISSION_MAP` + `HasPermission`
- `AuditEvent` + `OutboxEvent` models with SHA-256 chain hash
- `AuditMiddleware` (request_id, IP injection)

**What's pending (Phase 1 backlog):**
- Keycloak RS256 JWKS validation in `shared/auth.py` (TODO block)
- `HasPermission.has_permission()` → record 403 audit events
- Session revocation via JTI invalidation (see IAM service pattern)
- Bulk user import endpoint (`POST /api/v1/admin/users/import`)
- Two-Administrator approval flow for high-privilege roles
- Daily hash anchor export to System 22 via Celery Beat
- Brute-force: IP-level throttle at 100 failed/min → 15 min block; 1000/24h → 24h block
- `UserProfile` management endpoints (currently has model, no views)

**Key validation rules:**
- MFA required for all internal roles
- Password min 12 chars, HIBP check before accept
- Reuse blocked for last 12 passwords
- Account locked after 5 failures (15 min cooldown)
- Step-up auth for vault export, Board ratification, results publication

---

### Phase 2 — NBEC Management Portal
**SRS Ref**: NBE-F01 | **Status**: Scaffolded (empty models/views)

**Apps affected**: `apps/committee/`

**Models to implement:**
```
NBECMember       — full_name, title, designation, appointing_authority, instrument_ref (unique),
                   tenure_start, tenure_end, status (draft/active/expired/renewed),
                   conflict_declarations (FK to ConflictDeclaration)
Meeting          — type (ordinary/extraordinary/closed), date, venue, quorum (default 5),
                   chair_id, status (draft/agenda_issued/convened/adjourned/minuted)
Agenda           — linked to Meeting, versioned, supporting papers
MinutesRecord    — linked to Meeting, immutable after Chair approval, signed_pdf_ref
ActionItem       — open/in_progress/completed/verified, owner_id, due_date, auto-escalate at +7 days
ConflictDeclaration — member_id, subject_type, subject_id, nature, effective_date, approved (bool)
```

**Key business rules:**
- Only one active Chair at any time
- Approved minutes → immutable; changes require addendum
- Active sessions revoked within 60 seconds of deactivation
- Conflict declarations auto-exclude member from related item/marking/ratification queues
- All meeting records archived to System 05 on Chair approval (via outbox event)

**Key endpoints:**
```
POST   /api/v1/committee/members/
PATCH  /api/v1/committee/members/{id}/
POST   /api/v1/committee/meetings/
POST   /api/v1/committee/meetings/{id}/convene/
POST   /api/v1/committee/meetings/{id}/minutes/
POST   /api/v1/committee/meetings/{id}/minutes/approve/
POST   /api/v1/committee/conflicts/
POST   /api/v1/committee/conflicts/{id}/decide/
```

---

### Phase 3 — Secure Item Authoring & Content Vault
**SRS Ref**: NBE-F02, NBE-N01 | **Status**: Models implemented, services/views pending

**Apps affected**: `apps/itembank/`

**Models already implemented:**
- `Item` — FSM workflow (draft → submitted → in_review → reviewed → revised → moderation_panel → approved → locked_for_use)
- `ItemVersion` — full snapshot on every save
- `ExamPaper` — per sitting/subject, blueprint validated flag
- `ItemUsage` — tracks reuse across sittings

**Services to implement:**
```python
# apps/itembank/services.py
def submit_item(item_id, actor_auth) -> Item:
    # Validate metadata, call item.submit(), record AuditEvent(ITEM_SUBMITTED)

def assign_reviewer(item_id, reviewer_id, actor_auth) -> Item:
    # Set reviewer_id, call item.assign_for_review(), audit

def approve_item(item_id, actor_auth) -> Item:
    # Check has_sufficient_panel_votes, call item.approve(), then item.lock(), audit

def export_vault_items(paper_id, actor_auth) -> bytes:
    # Multi-party authorisation check, decrypt via shared.vault, produce encrypted package, audit VAULT_READ
```

**Vault encryption pattern:**
```python
from shared.vault import encrypt_item, decrypt_item
encrypted, nonce = encrypt_item(content_bytes)
item.content_encrypted = encrypted
item.vault_nonce = nonce
item.content_hash = hashlib.sha256(content_bytes).hexdigest()
```

**Key validation rules:**
- All mandatory metadata fields before submission
- MCQ: exactly 1 correct answer (fix `has_valid_mcq_config` guard)
- Media ≤ 25 MB, virus-scanned before storage
- Vault export requires 2-of-N authorisation + step-up MFA
- Every vault read logged with actor, timestamp, item_id

**Additional models needed:**
```
ItemPanelVote    — item_id, panellist_id, vote (approve/reject), justification, voted_at
ItemAnnotation   — item_id, reviewer_id, annotation_text, resolved (bool)
```

---

### Phase 4 — Sitting Configuration & Blueprint Engine
**SRS Ref**: NBE-F03 | **Status**: Models implemented, services/views pending

**Apps affected**: `apps/sitting/`

**Models already implemented:**
- `Sitting` — FSM draft/configured/locked/active/closed, T-30 auto-lock
- `SubjectPaper` — 5 papers per sitting (§71), marks allocation
- `Blueprint` — topic weights, cognitive level distribution, validated flag
- `SittingLock` — audit record of T-30 lock events

**Services to implement:**
```python
# apps/sitting/services.py
def configure_sitting(ref, data, actor_auth) -> Sitting:
    # Validate all 5 SubjectPapers configured, set status=configured, audit

def validate_blueprint(sitting_ref, actor_auth) -> Blueprint:
    # Check topic coverage, cognitive distribution, mark blueprint.validated=True, audit

def lock_sitting(sitting_ref, override=False, reason="", actor_auth=None) -> Sitting:
    # Set status=locked, create SittingLock, emit SittingLocked event
```

**Celery Beat task** (add to `apps/sitting/tasks.py`):
```python
@shared_task
def auto_lock_t30():
    """Run daily. Lock all sittings where sitting_date - today <= 30 days and status=configured."""
```

**Key validation rules:**
- Exactly 5 `SubjectPaper` records per sitting (§71)
- `ref` format: `^BAR-\d{4}-\d{2}$` (validated by `validate_sitting_ref`)
- `pass_mark` must be set before any marking can proceed
- After T-30 lock: no changes to sitting config, blueprint, or paper construction
- Override requires `sitting:lock:override` permission + audit-logged reason

---

### Phase 5 — Candidate Registration & Eligibility Gate
**SRS Ref**: NBE-F04 | **Status**: Models implemented, services partially stubbed

**Apps affected**: `apps/registration/`

**Models already implemented:**
- `Candidate` — eligibility_status, index_number (BAR-YYYY-CCCCC), disability_codes
- `Registration` — FSM draft → pending_eligibility → pending_payment → registered → withdrawn
- `EligibilityCheck` — NLEMS response log
- `RegistrationSlip` — signed PDF + QR

**Services to implement:**
```python
# apps/registration/services.py
def submit_registration(candidate_id, sitting_ref, actor_auth) -> Registration:
    # Create Registration, call registration.submit() → triggers check_eligibility_async

def check_eligibility_nlems(registration_id) -> None:
    # Call NLEMS via System 17 with 24h cache; set candidate.eligibility_status
    # On eligible: registration.mark_eligible(); on blocked: registration.block(reason)

def dg_override(candidate_id, justification, evidence_refs, actor_auth) -> Candidate:
    # DG role + step-up MFA check; set eligibility_status=eligible_override; audit

def generate_index_number(registration) -> str:
    # Format BAR-YYYY-CCCCC with advisory lock + pre-allocated sequence per year
    # Immutable once generated

def generate_slip(registration) -> RegistrationSlip:
    # Signed PDF with QR payload hash; store in MinIO; notify candidate
```

**NLEMS integration pattern:**
```python
# Call via System 17 — never call NLEMS directly
from shared.events import publish
# For sync checks: use requests + settings.NLEMS_URL + settings.NLEMS_API_KEY
# For async: task calls NLEMS, updates registration, publishes event
```

**Key validation rules:**
- Email + phone unique within the active sitting
- Photo ≤ 5 MB, biometric quality pre-check (sharpness, face detection)
- OTP: 6-digit, valid 10 min, max 5 attempts, rate-limited per phone/email/IP
- Locked fields after submission: name, DOB, national_id, llb_id, lpt_cert_number
- Withdrawal window: T-21 days (configurable); withdrawal does NOT increment attempt counter
- Index number format: `^BAR-\d{4}-\d{5}$`; generated server-side only

---

### Phase 6 — CBT Delivery (System 10B)
**Scope**: Primarily System 10B — the nbes-backend receives candidate responses and attendance.

**What this service handles:**
- Ingest CBT responses from System 10B: `POST /api/v1/scripts/ingest/cbt/`
- Ingest attendance: `POST /api/v1/registration/attendance/`
- Seat allocation updates trigger slip re-issuance (via event)

---

### Phase 7 — Identity Verification & Proctoring (System 10B)
**Scope**: Primarily System 10B. This service stores proctoring incidents forwarded by 10B.

---

### Phase 8 — Paper-Scan Failover & Accessibility (System 10B)
**Scope**: This service receives digitised PBT scripts from System 10B for AI marking.

**What this service handles:**
- Ingest PBT scripts: `POST /api/v1/scripts/ingest/pbt/`
- OCR quality gate (reject scripts below threshold → route to manual marking)

---

### Phase 9 — AI-Assisted Marking & Mandatory Human Moderation
**SRS Ref**: NBE-F05, NBE-N02 | **Status**: Models implemented, services/tasks pending

**Apps affected**: `apps/marking/`

**Models already implemented:**
- `Script` — FSM received → ai_marking → ai_complete → borderline → moderation_complete → reconciliation → final_mark_locked
- `MarkingDecision` — ai_mark, moderator_mark, second_mark, final_mark, audit_hash
- `DoubleMarkSample` — 5% random sample

**Additional models needed:**
```
ScriptScoring    — script_id, model_version, per_item_marks_json, aggregate_mark, confidence,
                   evidence_highlights_ref, scored_at
BorderlineFlag   — script_id, paper_id, ai_mark, pass_mark, distance_pct, flagged_at
```

**Services to implement:**
```python
# apps/marking/services.py
def compute_borderline_flag(script) -> None:
    # Read pass_mark from Sitting.SubjectPaper; compute distance_pct
    # If abs(ai_mark - pass_mark) / pass_mark <= 0.05: set borderline_flagged=True

def compute_audit_hash(script) -> str:
    # SHA-256 over canonical JSON of: script content + rubric + ai_inputs + ai_outputs
    # Store in marking_decision.audit_hash; commit to System 22 via outbox

def check_reconciliation_needed(script) -> None:
    # If abs(moderator_mark - ai_mark) > threshold (default 10%): set reconciliation_required=True

def assign_moderator(script, eligible_pool) -> UUID:
    # Round-robin from pool; exclude moderators with ConflictDeclaration against candidate
```

**Celery tasks in `apps/marking/tasks.py`:**
```python
@shared_task(queue="marking-high")
def run_ai_scoring(script_id): ...

@shared_task(queue="marking-high")
def run_pre_publication_verification(sitting_ref):
    # Re-verify every script hash in the sitting
    # Any mismatch → block publication + critical alert to DG + Administrator
```

**Key validation rules:**
- AI scoring is advisory only — never the final mark for borderline scripts
- Models frozen per sitting; reject new model versions mid-sitting
- Moderation adjustment requires justification ≥ 30 words
- Moderators with COI on candidate: excluded server-side
- Pre-publication verification: 100% of scripts must pass
- Sampling rate: 1–20% (default 5%), stratified by score band

---

### Phase 10 — Results, Re-Sit Management, Certificate Trigger & Go-Live
**SRS Ref**: NBE-F06, NBE-F07, NBE-F08 | **Status**: Models implemented, services/views pending

**Apps affected**: `apps/results/`, `apps/resit/`, `apps/cert_trigger/`, `apps/sla/`

**Models already implemented (results):**
- `ResultSet` — FSM drafted → normalised → board_review → board_ratified → ready_to_publish → published
- `NormalisedResult` — per-candidate normalised marks + overall_outcome (pass/fail/withheld)
- `RatificationRecord` — immutable after Chair signature
- `RemarkRequest`

**Services to implement:**

```python
# apps/results/services.py
def run_normalisation(sitting_ref, method, actor_auth) -> ResultSet:
    # Apply normalisation method (e.g. linear scaling) to all NormalisedResult rows
    # Set result_set.normalisation_complete=True, call result_set.mark_normalised()

def verify_hash_chain(result_set) -> None:
    # Re-verify every script hash — raises if any mismatch → blocks publication

def generate_result_pdfs(result_set) -> None:
    # Produce signed PDF per candidate; store in MinIO; reference in NormalisedResult

def publish_results(result_set, actor_auth) -> ResultSet:
    # Step-up MFA required; calls result_set.publish(); emits ResultsPublished event
    # Must complete within 21 days of sitting (SLA enforced by apps/sla/)
```

```python
# apps/resit/services.py — §73 attempt counter
def record_attempt(candidate_id, sitting_ref, papers) -> AttemptCounter:
    # Increment counter on sitting attendance (not registration)
    # Block if counter >= max_attempts AND no NBEC exception granted

def grant_exception(candidate_id, nbec_decision, actor_auth) -> NBECException:
    # NBEC Member role + step-up MFA; audit RESIT_EXCEPTION_GRANTED
```

```python
# apps/cert_trigger/services.py — System 14 integration
def trigger_certificate(candidate_id, sitting_ref, actor_auth) -> CertTrigger:
    # Validate candidate outcome == PASS; call System 14 via System 17
    # Must complete within 1 hour of results publication (SLA monitored by apps/sla/)
    # Audit CERT_TRIGGERED
```

**SLA monitoring (apps/sla/):**
- Celery Beat task runs every 15 minutes
- Checks: (a) results published within 21 days of sitting; (b) cert trigger within 1 hour of publication
- Breaches → critical alert to DG, CLET Registrar, and System Operator

---

## 8. Integration Reference

| System | Env Var | Pattern | What it does |
|---|---|---|---|
| **System 17** (API Layer) | `SYSTEM_17_URL` | Signed HTTP + replay protection | All outbound inter-system calls |
| **System 22** (Audit) | via Kafka `nbes.audit` | Outbox → Kafka | Tamper-evident audit storage |
| **NLEMS** (Eligibility) | `NLEMS_URL`, `NLEMS_API_KEY` | Sync REST via System 17 + 24h cache | LLB + LPT verification |
| **System 20** (Payment) | `SYSTEM_20_WEBHOOK_SECRET` | Inbound webhook (HMAC verify) | Fee payment confirmation |
| **System 14** (Certification) | via System 17 | Outbound REST + 1h SLA | Qualifying certificate trigger |
| **System 21** (Communications) | `SYSTEM_21_URL`, `SYSTEM_21_API_KEY` | Outbound REST | Email + SMS notifications |
| **System 10B** (CBT Engine) | via System 17 | Inbound REST | Script ingestion, attendance |

**Outbound call pattern (via System 17):**
```python
# shared/integrations.py (to be created)
def call_system_17(endpoint, payload, *, method="POST"):
    # Add nonce + timestamp + HMAC signature
    # Handle retry with exponential backoff
    # Log correlation_id in AuditEvent
```

---

## 9. Celery Queue Reference

| Queue | Workers | Purpose |
|---|---|---|
| `marking-high` | `worker-marking` | AI scoring, audit hash — exam-critical, highest priority |
| `moderation` | `worker-general` | Borderline routing, reconciliation |
| `results` | `worker-general` | Normalisation, hash verification, PDF generation |
| `cert-trigger` | `worker-general` | System 14 webhook — 1-hour SLA |
| `notifications` | `worker-general` | System 21 dispatch |
| `sla-monitor` | `worker-sla` | SLA checking — every 15 minutes |
| `vault-integrity` | `worker-sla` | Daily vault SHA-256 integrity check |
| `outbox` | `worker-sla` | Outbox poller — every 5 seconds |

Always specify the queue when defining tasks:
```python
@shared_task(queue="marking-high", bind=True, max_retries=3)
def run_ai_scoring(self, script_id): ...
```

---

## 10. Testing Strategy

**Unit tests** (`tests/test_models.py`, `tests/test_services.py`):
- Test FSM transitions: valid transitions succeed, invalid transitions raise `TransitionNotAllowed`
- Test guard conditions with explicit fixture data
- Test audit events are emitted on every state change
- Test serializer validation (missing fields, wrong format, boundary values)

**Integration tests** (`tests/test_views.py`):
- Use `APIClient` with a JWT in the correct role
- Test 401 on missing/expired token
- Test 403 on wrong role
- Test standard envelope shape on success and error
- Test FSM 400 on invalid transition

**Test JWT helper pattern:**
```python
import jwt
from django.conf import settings

def make_jwt(role="nbec-member", sub="test-sub-uuid"):
    return jwt.encode(
        {"sub": sub, "email": "test@example.com", "role": role, "roles": [role]},
        settings.JWT_SECRET_KEY, algorithm="HS256"
    )
```

**Acceptance criteria (Given/When/Then)** — trace every test back to a SRS requirement ID
(e.g. `# SRS-NBE-F02-01` in the docstring).

---

## 11. Coding Patterns: Adding a New Feature

When adding a feature (e.g. "implement committee meeting creation"):

1. **Model** (`models.py`): Add model, set `db_table`, add FSM field + transitions if needed.
2. **Migration**: `python manage.py makemigrations <app>`.
3. **Guards** (`workflow/guards.py`): Add guard functions for any FSM conditions.
4. **Service** (`services.py`): Business logic. `@transaction.atomic`. Call `AuditEvent.record()`. Call `publish()`.
5. **Serializer** (`serializers.py`): Input validation + nested representation.
6. **View** (`views.py`): `permission_classes = [IsAuthenticated, has_permission("committee:manage")]`. Call service. Return `success_response(data, status_code)`.
7. **URL** (`urls.py`): Register the view.
8. **Events** (`events.py`): Add the event name constant (e.g. `MEETING_CONVENED = "MeetingConvened"`).
9. **Tests**: Model, service, view tests with role-correct JWTs.

---

## 12. Key Validation Rules (Cross-Cutting)

- `sitting_ref`: `^BAR-\d{4}-\d{2}$` (e.g. BAR-2026-05)
- `index_number`: `^BAR-\d{4}-\d{5}$` (e.g. BAR-2026-00001) — server-generated only
- Ghana phone: `^(\+233|0)\d{9}$`
- Justification text (moderation, override, exceptions): minimum 30 words enforced server-side
- Tenure end > tenure start; only one active Chair
- Items: all metadata required before `submit()` transition
- MCQ: exactly one correct answer
- Pass mark must be set before marking, borderline flagging, or normalisation can run
- Withdrawal: T-21 cut-off; does not increment attempt counter
- Vault export: multi-party authorisation + step-up MFA; every read audited
- Pre-publication: 100% script hash verification required

---

## 13. Risks and Open Questions

| Risk | Impact | Mitigation |
|---|---|---|
| NLEMS unavailability | Blocks all candidate registrations | 24h cache + async retry + DG override path |
| Keycloak RS256 JWKS not implemented | Auth broken in production | Implement before any production deployment |
| `has_sufficient_panel_votes` guard stubbed | Items can be approved without proper quorum | Implement `ItemPanelVote` model + guard |
| Vault HSM integration not wired | Prod exam content at risk | `VAULT_DEV_MODE=False` path + PKCS11 untested |
| AI scoring engine not connected | Phase 9 is blocked | Define AI service interface contract; mock first |
| viewflow Board ratification commented out | Results publication is blocked | Uncomment + configure `viewflow` in settings |
| `System 17` integration substrate not implemented | All inter-system calls will fail in prod | Implement `shared/integrations.py` first |
| Multiple roles per user not supported | Single-role model may be too restrictive | Product decision required before Phase 2 ships |
| Attempt counter logic for §73 | Legal compliance risk | Must be exact match to statute; get legal sign-off |
| 21-day publication SLA | Statutory obligation | SLA monitor must be tested end-to-end before UAT |

---

## 14. Implementation Order

```
Phase 1 (Auth substrate)  →  Phase 2 (Committee)  →  Phase 3 (Item vault)
       ↓
Phase 4 (Sitting config)  →  Phase 5 (Registration)  →  Phase 6-8 (CBT ingestion)
       ↓
Phase 9 (Marking)  →  Phase 10 (Results + Re-sit + Cert + Go-Live)
```

Dependencies:
- Phase 2 requires Phase 1 (RBAC + audit)
- Phase 3 requires Phase 2 (conflict declarations gate item review)
- Phase 4 requires Phase 3 (blueprint uses item bank)
- Phase 5 requires Phase 4 (registration references sitting)
- Phase 9 requires Phase 4 (pass_mark from sitting) + Phase 5 (candidate records)
- Phase 10 requires Phase 9 (final marks) + Phase 5 (candidate eligibility_override flag)

**Current priority**: Complete Phase 1 guard/service stubs → Phase 2 committee models →
Phase 3 item vault services → Phase 4 sitting services → Phase 5 registration services.
