# IAM ↔ NBES — Phase 2 Hand-off

What the IAM team needs to deliver / confirm so NBES Phase 2 (NBEC Management
Portal) works end-to-end.

NBES owns: the **post-login NBEC dashboard** — member register, meetings,
agendas, minutes, COI, action items, System 05 archival.
IAM owns: **all authentication, authorisation, user creation, MFA, invites,
notification dispatch, role grants/revocations**.

---

## 1. Consume `MemberExpired` events (or expose a revoke contract)

**Where it fires.** When an NBEC member's tenure expires, NBES runs
`apps.committee.tasks.monitor_tenure_expiry` daily at 00:30 UTC. It flips
the local status to `Expired` and publishes a domain event to the Kafka
topic `nbes.committee`.

**Event schema (already published by NBES):**
```json
{
  "event_name": "MemberExpired",
  "topic": "nbes.committee",
  "payload": {
    "member_id":      "<uuid>",   // NBEC member record id
    "keycloak_sub":   "<uuid>",   // IAM user id — primary key for revocation
    "designation":    "chair | deputy_chair | member",
    "tenure_end":     "YYYY-MM-DD"
  }
}
```

**What IAM needs to do.** Subscribe to `nbes.committee`, filter for
`event_name == "MemberExpired"`, and revoke the user's NBEC client-role
grant. The role names to remove are the IAM platform roles that grant
NBEC access — at minimum `nbec_member`. If the expired member was Chair
or Deputy Chair you may also revoke `nbec_chair` / `nbec_deputy_chair`
if those roles exist in your realm.

IAM already has the right internal helper: `revoke_role_from_user(keycloak_user_id, role_name)`
in `users/services/role_bindings.py`.

**SRS requirement satisfied:** §2.2.1 — "Daily monitor moves members past
tenure end to Expired and revokes NBEC content access within 24 hours."

**Alternative (if event-bus subscription is hard right now).** Confirm
that NBES may call IAM's existing endpoint:

```
DELETE /v1/admin/users/{keycloak_user_id}/role-bindings/{role}
Authorization: Bearer <service-account JWT with role:bind>
```

If you go this route, provide NBES with:
- The IAM base URL (e.g. `https://iam.gsl.example/`)
- A long-lived service-account client-id + secret that NBES can use to
  obtain an access token with the `role:bind` permission
- The exact `{role}` slug for each NBEC platform role

---

## 2. Step-up MFA token contract (Action MFA)

**Where it fires.** NBES Chair-only endpoints that mutate immutable
state need step-up MFA per SRS §1.2.3. In Phase 2 these are:

| Endpoint | Method | Action |
|---|---|---|
| `/api/v1/nbec/minutes/{id}/sign/`     | POST | Chair seals minutes (immutable from this point) |
| `/api/v1/nbec/minutes/{id}/addendum/` | POST | Chair issues addendum to signed minutes |
| `/api/v1/nbec/coi/{id}/review/`       | POST | Chair approves / dismisses a COI |

Future phases (3, 4, 9, 10) will add more — vault export, ratification, results publication.

**What we need from IAM.**

1. **Token format.** IAM's `POST /api/auth/mfa/action/verify/` returns an
   `X-Action-MFA` token (per the AMS IAM API surface you shared). NBES
   needs to validate it on every protected endpoint. Document:
   - Header name (`X-Action-MFA`?)
   - Token type (JWT? Opaque?)
   - If JWT: signing algorithm, JWKS endpoint or public key, expected
     claims (must include `sub` matching the bearer token's `sub`,
     `scope`/`action` identifying which action it authorises, `exp` TTL)
   - If opaque: an IAM introspection endpoint NBES can call

2. **TTL.** What's the lifetime of the token (we assume ≤5 minutes per
   SRS step-up semantics).

3. **Scoping.** Can a single Action-MFA token authorise multiple actions,
   or does the client need to re-init for every sensitive call? NBES
   prefers per-action so we can audit cleanly.

4. **Library or contract only.** If you ship a Python verification
   helper, NBES will use it. Otherwise NBES will decode the JWT locally
   using the documented JWKS.

**Until this is delivered, the three endpoints above accept any
authenticated user with `committee:manage`.** That is below the SRS
acceptance bar.

---

## 3. Notifications — IAM dispatches; NBES publishes events

**Decision (from your message).** Notification dispatch (email + in-app +
SMS) is owned by IAM, not NBES. NBES publishes domain events; IAM
subscribes and dispatches.

**NBES already publishes these Phase 2 events to Kafka topic `nbes.committee`:**

| Event | When | Recipients per SRS |
|---|---|---|
| `MeetingScheduled`           | Secretariat creates a meeting       | All NBEC members |
| `MeetingAgendaPublished`     | Secretariat publishes an agenda     | All NBEC members (within 5 min per §2.2.2) |
| `MeetingAttendanceRecorded`  | Attendance captured                 | (optional) |
| `MeetingConvened`            | Meeting moves to Convened           | (optional) |
| `MeetingAdjourned`           | Meeting moves to Adjourned          | (optional) |
| `MinutesSigned`              | Chair seals minutes                 | All NBEC members |
| `MinutesAddendumIssued`      | Chair issues addendum               | All NBEC members |
| `MinutesArchived`            | System 05 confirmed archival        | Secretariat |
| `MinutesArchiveFailed`       | Permanent rejection by System 05    | Administrator (critical) |
| `MinutesArchiveIntegrityMismatch` | Daily integrity check fails    | Administrator (critical) |
| `ConflictDeclared`           | Member declares a COI               | Secretariat + Chair |
| `ConflictReviewed`           | Chair approves / dismisses a COI    | Declaring member |
| `COIRefreshDue`              | Annual COI refresh window reached   | Declaring member |
| `MemberCreated`              | Secretariat creates an NBEC member  | (none — IAM already invited the user) |
| `MemberActivated`            | Secretariat activates a member      | (none) |
| `MemberExpired`              | Tenure expired (daily monitor)      | Administrator (also drives role revoke — see §1) |
| `MemberAmended`              | Member record amended               | (audit only) |
| `ActionItemEscalated`        | Action item overdue                 | Assignee |

**What IAM needs to do.**

1. Subscribe a notification dispatcher to `nbes.committee`.
2. For each event above, decide channel (email / in-app / SMS) and template.
3. Look up the recipient's email/phone from IAM's own user record (NBES
   does not store phone numbers for NBEC members — only the `contact`
   email field per SRS §2.5.1).

**SRS deadlines:** Agenda publish notifications must reach all members
**within 5 minutes** (§2.2.2). Other notifications: no hard SLA but
should fire promptly.

**What NBES will NOT do.** NBES will not send email, will not send SMS,
will not maintain notification templates for these events. NBES's
existing `apps/notifications/` module is reserved for any
NBES-internal-only signal that doesn't need to leave the service.

---

## 4. User contact lookup (optional)

NBES currently stores `contact` (a single email) on each `NBECMember`
record per the SRS data model (§2.5.1). This is used for display in the
member register only — NBES never sends mail to that address itself.

If IAM exposes a clean user-lookup endpoint (e.g.
`GET /v1/admin/users/{keycloak_user_id}` returning at minimum
`{email, phone, first_name, last_name}`), NBES could drop the `contact`
column entirely and resolve it on demand.

**Not blocking** — happy to keep `contact` if the lookup endpoint isn't
on your roadmap. Just want to flag it so we don't store the same email
twice without a clear refresh story.

---

## What IAM does NOT need to do for Phase 2

| Concern | Owner | Notes |
|---|---|---|
| Invite emails on user creation | IAM | Already handled — IAM sends invite when admin creates the IAM user. NBES Secretariat then attaches an NBEC member record to the existing `keycloak_sub`. |
| First-time password setup, MFA enrolment | IAM | Standard IAM flow. NBES never sees passwords or MFA secrets. |
| Issuing JWTs with NBES client roles | IAM | NBES reads `resource_access[nbes-api].roles` (default `nbes-api` client id). |
| Session revocation on role change | IAM | NBES caches role decisions for ≤60s; cache invalidates on role-change events. |
| System 05 archival | NBES | NBES owns this — implemented in `shared/integrations/system05.py` + `apps/committee/tasks.archive_minutes_to_system05`. |
| COI policy enforcement (filter conflicted members from queues) | NBES | NBES internal — does not cross the IAM boundary. |
| Quorum / one-active-Chair / tenure-end > tenure-start validation | NBES | Domain rules in `apps/committee/`. |

---

## What's done on the NBES side as of this hand-off

- ✅ Field names match SRS §2.5.1 (`designation`, `tenure_start`, `tenure_end`, `contact`).
- ✅ Designation set matches §2.2.1 (Chair / Deputy Chair / Member only — no "Secretary" inside the member register).
- ✅ Daily tenure-expiry monitor: flips local status + publishes `MemberExpired` event (`apps/committee/tasks.monitor_tenure_expiry`).
- ✅ All NBEC events publish through the transactional outbox to Kafka topic `nbes.committee`.
- ✅ System 05 archive bridge for signed Minutes (`shared/integrations/system05.py`, `archive_minutes_to_system05` Celery task with exponential backoff up to ~24h).
- ✅ Daily integrity verification of archived Minutes (`verify_archive_integrity`).
- ✅ Annual COI refresh monitor publishing `COIRefreshDue` (`monitor_coi_refresh_due`).
- ✅ No direct Keycloak Admin API calls from NBES (`shared/keycloak_admin.py` deleted).

---

## Settings NBES expects from ops (not from IAM team)

```
SYSTEM_05_URL=...        # Regulator archive base URL
SYSTEM_05_API_KEY=...    # Bearer token for System 05

# Existing (unchanged):
KEYCLOAK_REALM_URL=...   # IAM realm — NBES decodes JWTs from this issuer
NBES_CLIENT_ID=nbes-api  # NBES's client id in the IAM realm
KAFKA_BOOTSTRAP_SERVERS=...  # event bus IAM will subscribe to
```

In dev (`SYSTEM_05_URL` empty) the archive call is a logged no-op so
local environments work without a running System 05.

---

## Open questions for the IAM team

1. **Pick one for §1:** event consumer on `nbes.committee` topic, or
   issue NBES a service-account credential for `DELETE /v1/admin/users/{kc_sub}/role-bindings/{role}`?
2. **§2 token contract:** when can NBES expect the Action-MFA validation
   spec? Until then, Chair-signing endpoints don't have step-up MFA.
3. **§3 dispatcher:** is the notification dispatcher already subscribed
   to `nbes.committee`, or does NBES need to also POST to a System 21
   webhook? (NBES has a `SYSTEM_21_URL` setting from earlier work — let
   us know if that's the path you want.)
4. **§4 contact lookup:** is `GET /v1/admin/users/{kc_sub}` worth
   exposing, or should NBES keep its `contact` column?
