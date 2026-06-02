# Activity Feature Optimization — Design

**Date:** 2026-06-01
**Branch:** `feat/activity-optimization`
**Status:** Approved design → implementation planning

## Background

The activity feature (`activity_log` table) records interactions across the platform
(RFQs sent, emails received, calls, offers, status changes, notes, sightings) and
surfaces them on the requisition Activity tab, the company/account Activity tab, and
sightings panels. Two facts established during investigation:

1. **Inbox auto-logging already works.** `core_jobs.py:_job_inbox_scan` runs every
   `settings.inbox_scan_interval_min` (default 30 min) and calls `_scan_user_inbox` →
   `poll_inbox()` → `log_email_activity(direction="received")`. Webhooks
   (`webhook_service.py`) add near-real-time logging. There is **no missing-poller bug**.
   The real weakness is that pipeline failures (disconnected mailbox, dead token) are
   **silent** — logging just stops with no user-visible signal.
2. **AI only lightly touches records.** `activity_quality_service.py` rewrites a
   `clean_summary` for `email_received` and `sighting_added` only, within a 7-day window.
   There is no synthesis across a timeline — no "what's happening on this RFQ" view.

This effort delivers two things:
- **Feature A:** make the inbox-logging pipeline's health visible and manually recoverable.
- **Feature B:** an AI-generated timeline digest for requisitions and accounts.

Out of scope (explicitly deferred): broadening AI cleanup to more activity types,
extending the 7-day scoring window, and summarizing raw email bodies.

## Stack constraints

FastAPI + SQLAlchemy 2.0 + PostgreSQL + HTMX + Alpine.js + Jinja2 + Tailwind. No new
runtime dependencies. No React/SPA patterns. All schema changes via Alembic with a
working downgrade. Tests accompany all new code. APP_MAP docs updated in the same PR.

---

## Feature A — Inbox-logging hardening + observability

### A1. Status helper

Add `get_inbox_sync_status(user: User) -> dict` to `app/services/activity_service.py`:

```python
{
    "connected": bool,        # user.m365_connected
    "last_scan_at": datetime | None,   # user.last_inbox_scan
    "is_stale": bool,         # last scan older than 2× inbox_scan_interval_min, or never
    "token_ok": bool,         # token present and not expired (token_expires_at > now)
    "error_reason": str | None,  # user.m365_error_reason
    "health": InboxSyncHealth,  # OK | WARNING | ERROR (StrEnum, see Constants)
}
```

`health` is an `InboxSyncHealth` StrEnum value (not a raw string — see **Constants** below).
`is_stale` rule: `last_scan_at is None` OR `now - last_scan_at > 2 * inbox_scan_interval_min`.
`health` derivation: `ERROR` when `not connected or not token_ok`; else `WARNING` when
`is_stale`; else `OK`.

### A2. Mailbox-sync health card

Render a "Mailbox sync" card on the Settings → Profile tab
(`app/templates/htmx/partials/settings/...`, profile tab served by `settings_profile_tab`
in `htmx_views.py:7996`). The profile tab route computes `get_inbox_sync_status(user)`
and passes it as `inbox_status`. Card shows:
- Health dot (green `ok` / amber `warning` / red `error`) using brand palette.
- Connected state and account email.
- "Last inbox scan: {relative time}" (uses the existing `timeago` filter).
- Error reason text when present.
- A **Scan now** button (see A3).

### A3. Real "Scan now" endpoint

The existing `poll_inbox_htmx` (`htmx_views.py:2807`) is a test-mode no-op. Replace its
body with a real on-demand scan of the **current user's** mailbox:

- Route: keep `POST /v2/partials/requisitions/{req_id}/poll-inbox` for the responses tab
  flow, AND add `POST /v2/partials/settings/inbox/scan-now` for the settings card.
- Both call a shared helper that runs `_scan_user_inbox(user, db)` (from
  `app/jobs/email_jobs.py`) wrapped in `asyncio.wait_for(..., timeout=90)`.
- `TESTING` guard: when `settings.testing` / `TESTING=1`, skip the Graph call and return
  the refreshed view/status without hitting the network (so tests stay hermetic). This is
  a real implementation gated for tests — not a permanent stub.
- Settings endpoint returns the refreshed mailbox-sync card partial (so the card updates
  in place with new `last_scan_at`); requisition endpoint keeps returning the responses tab.
- Loading state via `data-loading-disable` / HTMX indicator; success toast via
  `$store.toast`.

### A4. Disconnected banner

A dismissible banner partial (`htmx/partials/shared/inbox_disconnected_banner.html`)
shown **only** when `inbox_status.health == "error"` or `is_stale`. Included at the top of
the requisitions list content partial (the template rendered by the requisitions list
route in `htmx_views.py`); the route computes `get_inbox_sync_status(user)` and passes it
as `inbox_status`. Copy: one line stating the mailbox is disconnected/stale plus a link to
Settings → Profile to reconnect. Dismissible via Alpine local state (`x-data`) for the
session; reappears on next full page load while the condition persists.

---

## Feature B — AI timeline digest (requisition + account)

### B0. Constants (new StrEnums in `app/constants.py`)

Per the CLAUDE.md non-negotiable ("always use `StrEnum` constants, never raw strings"),
add three StrEnums alongside `ActivityType`:

```python
class DigestEntityType(StrEnum):
    REQUISITION = "requisition"
    COMPANY = "company"

class DigestStatusSignal(StrEnum):   # semantic state of the entity, drives card color
    ON_TRACK = "on_track"
    STALLED = "stalled"
    NEEDS_ATTENTION = "needs_attention"

class InboxSyncHealth(StrEnum):      # Feature A health card / banner
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
```

The runtime render-state of a digest (`ready` / `insufficient` / `generating` / `error`)
is ephemeral view state returned by the service, **not** persisted; it is a small
module-level `DigestState` StrEnum in `activity_digest_service.py`, not stored on the row.

### B1. Data model — `ActivityDigest`

New model added to `app/models/intelligence.py` (alongside `ActivityLog` / `ChangeLog` /
`ProactiveMatch`), exported from `app/models/__init__.py`; Alembic migration with a working
downgrade. Datetime columns use the `UTCDateTime` type from `app/database.py` (matching
every other column in `intelligence.py`).

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `entity_type` | String(20) | `DigestEntityType` value; `@validates` guard rejects others |
| `entity_id` | int | FK-by-convention to requisitions.id / companies.id (no hard FK — polymorphic) |
| `headline` | String(300) | one-line summary |
| `narrative` | Text | 2–4 sentence summary |
| `highlights` | JSON | list of `{label, value}` bullets |
| `next_step` | String(500) | nullable suggested next action |
| `status_signal` | String(20) | `DigestStatusSignal` value |
| `generated_at` | UTCDateTime | when produced |
| `basis_last_activity_at` | UTCDateTime | max(activity.created_at) at generation |
| `basis_activity_count` | int | count of considered activities at generation |
| `cooldown_until` | UTCDateTime, nullable | regeneration suppressed until this time (see B2) |
| `model` | String(50) | model id used |

Unique constraint on `(entity_type, entity_id)` — one current digest per entity. Writes use
`INSERT … ON CONFLICT (entity_type, entity_id) DO UPDATE` (PostgreSQL upsert) so the final
write is race-safe even if two generations finish near-simultaneously.

### B2. Generation service — `app/services/activity_digest_service.py`

`async def get_or_build_digest(entity_type: DigestEntityType, entity_id: int, db: Session, force: bool = False) -> dict`

Algorithm:
1. Load existing `ActivityDigest` for `(entity_type, entity_id)`.
2. **Cooldown guard (skip when `force`):** if a row exists and `cooldown_until` is in the
   future, return it as-is (`state: "ready"`) without computing the basis or calling the
   AI. Cooldown default **120s** (`settings.digest_cooldown_seconds`); the Refresh button
   sends `force=1` and bypasses it. This caps Sonnet calls during write bursts (e.g. a
   sighting batch logging many rows) while keeping the digest near-fresh.
3. Load activities for the entity, **hard-capped at the 30 most recent**:
   - requisition → `get_requisition_activities(entity_id, db, meaningful_only=True, limit=30)`
   - company → `get_company_activities(entity_id, db, limit=30)`
4. Compute current basis: `basis_last_activity_at = max(created_at)`,
   `basis_activity_count = len(activities)`.
5. **Insufficient-activity short-circuit:** if `basis_activity_count < 2`, return
   `{"state": "insufficient"}` WITHOUT calling the AI and without writing a row.
6. **Freshness check (skip when `force`):** if the existing row's `basis_last_activity_at`
   and `basis_activity_count` both equal current → return cached (`state: "ready"`).
7. **Stampede guard:** acquire a Redis `nx` lock `lock:digest:{entity_type}:{entity_id}`
   with `ex=30` via `_get_redis()` (`app/cache/intel_cache.py`), released in a `finally` —
   the exact pattern at `core_jobs.py:121-150`. **If the lock is NOT acquired**, another
   request is already regenerating: return the existing row if present (`state: "ready"`,
   possibly stale) else `{"state": "generating"}`. Do not block, do not call the AI.
8. Holding the lock: build prompt (B3), call `claude_structured(schema=DIGEST_SCHEMA,
   system=<entity-specific>, model_tier="smart", max_tokens=700, cache_system=True)`, upsert
   the row via `ON CONFLICT DO UPDATE` (setting `cooldown_until = now + cooldown`), commit,
   return (`state: "ready"`).
9. On AI failure (no result / Claude error): return `{"state": "error"}`; do NOT write a
   poisoned row. Errors are surfaced in the card, never silently swallowed.

The basis-comparison **is** the auto-invalidation: any new activity changes
`max(created_at)` and/or the count, so the next view (past cooldown) regenerates. No
write-path hooks needed; the mechanism is self-healing. The Redis lock prevents concurrent
duplicate Sonnet calls; the cooldown prevents burst-driven repeat calls; the `ON CONFLICT`
upsert makes the final write race-safe.

### B3. Prompt + schema

One schema, two system prompts (mirrors `activity_quality_service`):

`DIGEST_SCHEMA` (required: headline, narrative, highlights, status_signal; optional: next_step):
- `headline`: string, ≤ 200 chars.
- `narrative`: string, 2–4 sentences.
- `highlights`: array of `{label: string, value: string}`, max 5 items.
- `next_step`: string or null.
- `status_signal`: enum `["on_track", "stalled", "needs_attention"]`.

System prompts:
- **Requisition** — sourcing-progress framing: vendors contacted, replies received, best
  offer, outstanding/blocked items, recommended next action. `stalled` when no inbound
  activity in a while; `needs_attention` when replies await action.
- **Account/company** — relationship framing: recent engagement, open RFQs, responsiveness
  / sentiment trend, recommended follow-up.

Prompt body lists the (capped, 30) meaningful activities newest-first, plus light entity
metadata (requisition: status, part count, offer count; company: name, is_strategic).

**Token economy — reuse the AI-cleaned summary.** For each activity, feed
`activity.summary` (the clean text `activity_quality_service` already wrote for
emails/sightings) and fall back to `activity.notes[:200]` only when `summary` is None.
Never feed raw email bodies. This makes the quality pass and the digest compound rather
than re-paying Sonnet to re-clean the same text.

**Why `claude_structured`, not `claude_text` (deliberate, overrides architect note 4a).**
The digest is rendered as a *structured card* — a one-line headline, an enum
`status_signal` that drives the card's color, scannable highlight bullets, and an explicit
next step. Reliable structured fields are exactly what makes the record "easy to process"
(the original user goal), so schema-enforced output is correct here; a prose blob would
lose that. `cache_system=True` still applies to the static system prompt.

### B4. Rendering — HTMX lazy-load

The Activity tab renders instantly with a placeholder that lazy-loads the digest so a
Claude call never blocks tab-open:

- Placeholder at the top of `requisitions/tabs/activity.html` (above the summary bar) and
  `customers/tabs/activity_tab.html`:
  ```html
  <div hx-get="/v2/partials/requisitions/{{ req.id }}/activity-digest"
       hx-trigger="load" hx-target="this" hx-swap="innerHTML">
    <!-- skeleton -->
  </div>
  ```
  `hx-target="this"` is required so the swap replaces the placeholder, not the parent
  `#main-content` / `#tab-content` target it would otherwise inherit (pattern:
  `vendors/detail.html` lazy-loaded sections).
- New endpoints return a shared digest-card partial
  (`htmx/partials/shared/activity_digest_card.html`):
  - `GET /v2/partials/requisitions/{req_id}/activity-digest`
  - `GET /v2/partials/customers/{company_id}/activity-digest`
  - Both accept `?force=1` for the Refresh affordance.
- Card renders by `state`:
  - `ready`: headline, status_signal color, narrative, highlight bullets, next_step,
    "Updated {relative}" + a Refresh link (`?force=1`).
  - `insufficient`: muted "Not enough activity to summarize yet."
  - `generating`: muted "Summary is being prepared…" with a self-retry
    (`hx-trigger="load delay:3s"`) so the card fills in once the in-flight generation
    commits. Only reachable on a cold cache during a concurrent first view.
  - `error`: muted "Couldn't generate a summary — try Refresh." (no crash, no fake data)

### B5. Model

`model_tier="smart"` (Sonnet 4.6) for both entity types. Cost stays bounded because the
basis cache regenerates only when the timeline changes.

---

## Testing (pytest, `TESTING=1`, mocked Claude + Graph)

- **Digest service:** insufficient short-circuit (<2 activities, asserts no Claude call);
  cached hit when basis unchanged (asserts no Claude call); regeneration when a newer
  activity exists; `force=1` regeneration; **cooldown** suppresses regen within the window
  but `force=1` overrides; **lock-miss** returns the stale row / `generating` without a
  Claude call; 30-activity cap enforced; prompt uses `summary` over raw `notes`;
  entity-type prompt selection; AI-failure path returns `error` and writes no row; upsert
  keeps one row per entity.
- **Inbox status helper:** `ok` / `warning` (stale) / `error` (disconnected, bad token)
  derivations; never-scanned → stale.
- **Scan-now endpoints:** `TESTING` guard returns refreshed partial without Graph;
  settings endpoint returns the card; requisition endpoint returns responses tab.
- **Migration:** upgrade → downgrade → upgrade clean; single `alembic heads`.

## Documentation

- `docs/APP_MAP_DATABASE.md`: add `ActivityDigest` table.
- `docs/APP_MAP_INTERACTIONS.md`: add the digest build/cache/invalidate flow and the
  inbox-sync observability surface.

## Build sequence

1. Constants: add `DigestEntityType`, `DigestStatusSignal`, `InboxSyncHealth` to
   `app/constants.py`; add `digest_cooldown_seconds` (default 120) to `config.py`.
2. `ActivityDigest` model in `app/models/intelligence.py` + export in
   `app/models/__init__.py`; then `alembic revision --autogenerate`, review, and test
   upgrade → downgrade → upgrade; confirm single `alembic heads`.
3. `activity_digest_service.py` (basis/cooldown/lock/upsert) + tests.
4. Digest endpoints + shared card partial + lazy-load placeholders + tests.
5. `get_inbox_sync_status` helper + tests.
6. Settings mailbox-sync card + real Scan-now endpoints + tests.
7. Disconnected banner on the requisitions list.
8. APP_MAP docs update (`APP_MAP_DATABASE.md`, `APP_MAP_INTERACTIONS.md`).
9. Full pipeline: `pre-commit run --all-files`, full pytest, then the PR-review agents.
