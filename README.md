# National Bar Examination System — Backend

Django REST API for the NBES platform (System 10A).

---

## Prerequisites

- Python 3.12
- Docker Desktop
- See `system-requirements.txt` for traditional setup

---

## Quick Start (Docker)

**1. Create the shared Docker network (once per machine):**
```bash
docker network create ams-network
```

> If you run the IAM service alongside NBES, both share `ams-network`. The NBES database
> service is named `nbes-db` to avoid DNS collisions with IAM's `db` service.

**2. Create your `.env` file:**
```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(50))"
# Paste output as SECRET_KEY in .env
```

**3. Start all services:**
```bash
docker compose up -d
```

This starts:
- `nbes-backend` — Django API on port **8003** (direct) / **8000** via API gateway
- `worker-marking` — Celery worker for AI scoring queue
- `worker-general` — Celery worker for moderation, results, notifications
- `worker-sla` — Celery worker for SLA monitor and outbox poller
- `beat` — Celery Beat scheduler
- `nbes-db` — PostgreSQL 16 on port **5434**
- `redis` — Redis 7 on port **6381**

Migrations run automatically on container start. No manual step needed.

**API:** http://localhost:8003/api/v1/
**Swagger Docs:** http://localhost:8003/api/docs/
**Redoc Docs:** http://localhost:8003/api/redoc/
**OpenAPI Schema:** http://localhost:8003/api/schema/
**Admin:** http://localhost:8003/admin/
**Via gateway:** http://localhost:8000/api/nbes/

---

## Testing with Real Keycloak Tokens

The Postman collection at `docs/postman/NBES_Phase2_NBEC_Portal.postman_collection.json`
covers all Phase 2 endpoints with a step-by-step auth flow.

**Prerequisites:**
1. IAM stack running (Keycloak at `http://localhost:8080`)
2. Add `127.0.0.1 keycloak` to your system hosts file so tokens are issued under the same
   hostname the backend expects
3. In Keycloak `clet-internal` realm, create:
   - A public client `nbes-test` with **Direct Access Grants** enabled and an audience
     mapper that adds `nbes-api` to the token
   - A test user `testsecretary` with the `nbec_secretariat` client role under `nbes-api`

**Import the collection → Run "Step 1 — Get Token" → all subsequent requests use the token automatically.**

---

## Implemented Endpoints

### Phase 1 — Auth, RBAC & Audit

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/me/permissions/` | Current user's resolved permissions |
| GET | `/api/v1/admin/rbac/roles/` | List roles |
| POST | `/api/v1/admin/rbac/roles/` | Create role |
| GET/PATCH/DELETE | `/api/v1/admin/rbac/roles/{id}/` | Role detail |
| GET | `/api/v1/audit/search/` | Search audit log |
| GET | `/api/v1/audit/chain/{date}/` | Verify daily hash chain |

### Phase 2 — NBEC Management Portal

Mounted at `/api/v1/nbec/`. Requires `committee:manage` permission.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/nbec/members/` | List NBEC members |
| POST | `/api/v1/nbec/members/` | Create member (DRAFT) |
| PATCH | `/api/v1/nbec/members/{id}/` | Amend member |
| POST | `/api/v1/nbec/members/{id}/activate/` | Activate member (DRAFT → ACTIVE) |
| POST | `/api/v1/nbec/coi/` | Declare conflict of interest |
| POST | `/api/v1/nbec/coi/{id}/review/` | Approve or dismiss COI |
| GET | `/api/v1/nbec/policy/coi/` | Check active conflicts for a member |
| POST | `/api/v1/nbec/meetings/` | Schedule meeting |
| POST | `/api/v1/nbec/meetings/{id}/agenda/` | Publish agenda (versioned) |
| POST | `/api/v1/nbec/meetings/{id}/attendance/` | Record attendance |
| POST | `/api/v1/nbec/meetings/{id}/convene/` | Convene (quorum check) |
| POST | `/api/v1/nbec/meetings/{id}/adjourn/` | Adjourn — auto-creates draft minutes |
| POST | `/api/v1/nbec/minutes/{id}/sign/` | Chair signs minutes (immutable) |
| POST | `/api/v1/nbec/minutes/{id}/addendum/` | Issue post-signing addendum |

### Phase 3 — Item Authoring

Mounted at `/api/v1/itembank/`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/itembank/items/` | List items |
| POST | `/api/v1/itembank/items/` | Create item (DRAFT) |
| GET/PATCH | `/api/v1/itembank/items/{id}/` | Item detail / amend |
| POST | `/api/v1/itembank/items/{id}/submit/` | Submit for review |
| POST | `/api/v1/itembank/items/{id}/approve/` | Approve item |
| POST | `/api/v1/itembank/items/{id}/reject/` | Reject item |
| POST | `/api/v1/itembank/items/{id}/asset/` | Upload asset |

---

## Project Structure

```
config/              Django project config (settings, urls, celery)
shared/              Cross-cutting infrastructure
  auth.py            JWT authentication (Keycloak RS256 in prod, HS256 in dev)
  permissions.py     RBAC — HasPermission + ROLE_PERMISSION_MAP
  events.py          Transactional outbox publish()
  vault.py           AES-256-GCM vault operations
  middleware.py      AuditMiddleware — request_id, IP injection
  exceptions.py      Standard response envelope + success_response/error_response helpers
workflow/
  guards.py          FSM transition condition functions
  signals.py         Post-transition signal handlers
  viewflow/          Board ratification + vault export flows (django-viewflow)
apps/
  users/             UserProfile — thin local profile (Keycloak owns auth)
  committee/         NBEC members, meetings, minutes, conflicts  ← Phase 2
  itembank/          Item authoring, vault, paper construction   ← Phase 3
  sitting/           Exam cycle config, blueprint, T-30 lock
  registration/      Candidate registration, NLEMS gate, index numbers
  marking/           AI scoring, borderline flagging, moderation
  results/           Normalisation, Board ratification, publication
  resit/             Attempt counter, Section 73 limit, exceptions
  cert_trigger/      Trigger System 14 on confirmed PASS
  notifications/     Notification orchestration to System 21
  audit/             Append-only audit trail, outbox, chain hash
  sla/               SLA monitor — 21-day publication, cert trigger
  reporting/         KPI aggregation, audit exports
docs/
  postman/           Postman collections for manual testing
```

---

## Architecture Reference

See `SYSTEM_ARCHITECTURE.md` for the full domain documentation including:
- Workflow state machines (django-fsm + django-viewflow)
- Kafka topic design
- Data schemas
- Security and RBAC matrix
- External system integrations (NLEMS, System 14, 17, 20, 21, 22)

---

## Environment Variables

See `.env.example` for all variables. Key ones:

| Variable | Description |
|---|---|
| `SECRET_KEY` | Django secret key |
| `KEYCLOAK_ENABLED` | `False` in dev (HS256 shared-secret JWT), `True` in prod (RS256) |
| `KEYCLOAK_REALM_URL` | e.g. `http://keycloak:8080/realms/clet-internal` |
| `KAFKA_ENABLED` | `False` in dev (outbox only), `True` in prod |
| `VAULT_DEV_MODE` | `True` in dev (software AES), `False` in prod (HSM) |
| `REDIS_URL` | Celery broker |
| `DBNAME`, `DBUSER`, `DBPASSWORD`, `DBHOST` | PostgreSQL connection |
