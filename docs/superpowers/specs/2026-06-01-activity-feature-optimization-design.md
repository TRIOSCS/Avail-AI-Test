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
    "health": "ok" | "warning" | "error",  # derived: error if not connected/token bad;
                                            # warning if stale; else ok
}
```

`is_stale` rule: `last_scan_at is None` OR `now - last_scan_at > 2 * inbox_scan_interval_min`.
`health` derivation: `error` when `not connected or not token_ok`; else `warning` when
`is_stale`; else `ok`.

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

### B1. Data model — `ActivityDigest`

New table (new model in `app/models/intelligence.py`; Alembic migration with downgrade):

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `entity_type` | String(20) | `"requisition"` or `"company"` |
| `entity_id` | int | FK-by-convention to requisitions.id / companies.id (no hard FK — polymorphic) |
| `headline` | String(300) | one-line summary |
| `narrative` | Text | 2–4 sentence summary |
| `highlights` | JSON | list of `{label, value}` bullets |
| `next_step` | String(500) | nullable suggested next action |
| `status_signal` | String(20) | `on_track` / `stalled` / `needs_attention` |
| `generated_at` | UTCDateTime | when produced |
| `basis_last_activity_at` | UTCDateTime | max(activity.created_at) at generation |
| `basis_activity_count` | int | count of considered activities at generation |
| `model` | String(50) | model id used |

Unique constraint on `(entity_type, entity_id)` — one current digest per entity (upsert).

### B2. Generation service — `app/services/activity_digest_service.py`

`async def get_or_build_digest(entity_type: str, entity_id: int, db: Session, force: bool = False) -> dict`

Algorithm:
1. Load activities for the entity:
   - requisition → `get_requisition_activities(entity_id, db, meaningful_only=True)`
   - company → `get_company_activities(entity_id, db)` (meaningful filter applied in service)
2. Compute current basis: `basis_last_activity_at = max(created_at)`,
   `basis_activity_count = len(meaningful activities)`.
3. **Insufficient-activity short-circuit:** if `basis_activity_count < 2`, return a
   sentinel `{"state": "insufficient"}` WITHOUT calling the AI and without writing a row.
4. Load existing `ActivityDigest` for `(entity_type, entity_id)`. If it exists, `force` is
   False, and its `basis_last_activity_at == current` and
   `basis_activity_count == current` → return cached digest (`state: "ready"`).
5. Otherwise regenerate: build prompt (B3), call `claude_structured(schema=DIGEST_SCHEMA,
   system=<entity-specific>, model_tier="smart", max_tokens=700, cache_system=True)`,
   upsert the row (update in place if exists, else insert), commit, return
   (`state: "ready"`).
6. On AI failure (no result / Claude error): return `{"state": "error"}`; do NOT write a
   poisoned row. Errors are surfaced in the card, never silently swallowed.

This basis-comparison **is** the auto-invalidation: any new activity changes
`max(created_at)` and/or the count, so the next view regenerates. No write-path hooks
needed; the mechanism is self-healing.

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

Prompt body lists the meaningful activities (type, date, direction, contact, subject,
existing `summary`/`notes`) newest-first, plus light entity metadata
(requisition: status, part count, offer count; company: name, is_strategic).

### B4. Rendering — HTMX lazy-load

The Activity tab renders instantly with a placeholder that lazy-loads the digest so a
Claude call never blocks tab-open:

- Placeholder at the top of `requisitions/tabs/activity.html` (above the summary bar) and
  `customers/tabs/activity_tab.html`:
  ```html
  <div hx-get="/v2/partials/requisitions/{{ req.id }}/activity-digest"
       hx-trigger="load" hx-swap="innerHTML">
    <!-- skeleton -->
  </div>
  ```
- New endpoints return a shared digest-card partial
  (`htmx/partials/shared/activity_digest_card.html`):
  - `GET /v2/partials/requisitions/{req_id}/activity-digest`
  - `GET /v2/partials/customers/{company_id}/activity-digest`
  - Both accept `?force=1` for the Refresh affordance.
- Card renders by `state`:
  - `ready`: headline, status_signal color, narrative, highlight bullets, next_step,
    "Updated {relative}" + a Refresh link (`?force=1`).
  - `insufficient`: muted "Not enough activity to summarize yet."
  - `error`: muted "Couldn't generate a summary — try Refresh." (no crash, no fake data)

### B5. Model

`model_tier="smart"` (Sonnet 4.6) for both entity types. Cost stays bounded because the
basis cache regenerates only when the timeline changes.

---

## Testing (pytest, `TESTING=1`, mocked Claude + Graph)

- **Digest service:** insufficient short-circuit (<2 activities, asserts no Claude call);
  cached hit when basis unchanged (asserts no Claude call); regeneration when a newer
  activity exists; `force=1` regeneration; entity-type prompt selection; AI-failure path
  returns `error` and writes no row; upsert keeps one row per entity.
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

1. Alembic migration + `ActivityDigest` model.
2. `activity_digest_service.py` + tests.
3. Digest endpoints + shared card partial + lazy-load placeholders + tests.
4. `get_inbox_sync_status` helper + tests.
5. Settings mailbox-sync card + real Scan-now endpoints + tests.
6. Disconnected banner.
7. APP_MAP docs update.
8. Full pipeline: `pre-commit run --all-files`, full pytest, review agents.
