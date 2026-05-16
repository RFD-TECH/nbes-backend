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

**2. Create your `.env` file:**
```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(50))"
# Paste output as SECRET_KEY in .env
```

**3. Start all services:**
```bash
docker compose up
```

This starts:
- `nbes-backend` — Django API on port **8003** (direct) / **8000** via API gateway
- `worker-marking` — Celery worker for AI scoring queue
- `worker-general` — Celery worker for moderation, results, notifications
- `worker-sla` — Celery worker for SLA monitor and outbox poller
- `beat` — Celery Beat scheduler
- `db` — PostgreSQL 16 on port **5434**
- `redis` — Redis 7 on port **6381**

**4. Run migrations (first time only):**
```bash
docker compose run web python manage.py migrate
```

**5. Create an admin user (first time only):**
```bash
docker compose run web python manage.py createsuperuser
```

**API:** http://localhost:8003/api/v1/
**Docs:** http://localhost:8003/api/docs/
**Admin:** http://localhost:8003/admin/
**Via gateway:** http://localhost:8000/api/nbes/

---

## Project Structure

```
config/              Django project config (settings, urls, celery)
shared/              Cross-cutting infrastructure
  auth.py            JWT authentication (Keycloak in prod, shared-secret in dev)
  permissions.py     RBAC — HasPermission + ROLE_PERMISSION_MAP
  events.py          Transactional outbox publish()
  vault.py           AES-256-GCM vault operations
  middleware.py      AuditMiddleware — request_id, IP injection
  exceptions.py      Standard response envelope, FSM error mapping
workflow/
  guards.py          FSM transition condition functions
  signals.py         Post-transition signal handlers
  viewflow/          Board ratification + vault export flows (django-viewflow)
apps/
  users/             UserProfile — thin local profile (Keycloak owns auth)
  committee/         NBEC members, meetings, minutes, conflicts
  itembank/          Item authoring, vault, paper construction
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
| `KEYCLOAK_ENABLED` | `False` in dev (shared-secret JWT), `True` in prod |
| `KAFKA_ENABLED` | `False` in dev (outbox only), `True` in prod |
| `VAULT_DEV_MODE` | `True` in dev (software AES), `False` in prod (HSM) |
| `REDIS_URL` | Celery broker |
| `DBNAME`, `DBUSER`, `DBPASSWORD`, `DBHOST` | PostgreSQL connection |
