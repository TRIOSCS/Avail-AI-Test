# Activity Timeline — Unified Design Spec

**Date:** 2026-05-20
**Status:** Draft — awaiting user approval
**Goal:** One reliable timeline per req/part showing who did what, when, and what the vendor said back.

## Problem

The Activity tab on an active req shows "0 events / No activity recorded yet". Discovery
found this is a **write-side problem**, not a read or render bug:

- The tab query filters `ActivityLog.requisition_id == req_id` (`app/routers/htmx_views.py:1271`).
- Only **1 of 14** system event types logs activity — requirement status change
  (`app/routers/sightings.py:990`).
- Service-layer writers `log_email_activity` / `log_call_activity`
  (`app/services/activity_service.py`) take no `requisition_id`, so auto-logged rows land
  with `requisition_id = NULL` and are filtered out.
- Inbound vendor emails land in the `VendorResponse` table and never reach `activity_log`.
- 8x8 call logging is fully built (`app/services/eight_by_eight_service.py`,
  `app/jobs/eight_by_eight_jobs.py`) but gated off by `eight_by_eight_enabled=False`
  (`app/config.py:202`).
- **CONFIRMED BUG:** RFQ send tags subjects `[ref:{requisition_id}]`
  (`app/email_service.py:95`), but the sent-folder scan regex `_AVAIL_TAG_RE`
  (`app/jobs/email_jobs.py:774`) matches only `[AVAIL-(\d+)]`. Every outbound RFQ
  therefore logs with `requisition_id=NULL` and never reaches the req timeline.
  `poll_inbox` already handles both formats (`app/email_service.py:450`).

The DB is intentionally empty right now — no historical backfill needed; the fix is
forward-only.

## Approach: extend `activity_log`, do not build a framework

The existing `activity_log` table (`app/models/intelligence.py:257-375`) already holds every
field a unified timeline needs. We make it canonical rather than introducing a new table.

### Canonical schema — reuses existing columns, NO migration needed

The `activity_log` table already carries every needed column. Reading the live model
(`app/models/intelligence.py:257-375`) corrected two assumptions from the draft:

| Concept | Column | Notes |
|---|---|---|
| Event type | `activity_type` | Canonical enum (below); all values fit existing `String(20)` |
| Source / channel | `channel` | `email`/`phone`/`manual`/`system` — existing field, serves as the source axis; no separate `source` column |
| Coarse category | `event_type` | Existing `email`/`call`/`note`/`meeting`; left unchanged |
| Actor | `user_id` | Nullable — null for automated/system events |
| Vendor/contact | `vendor_card_id`, `vendor_contact_id`, `company_id`, `site_contact_id` | Existing |
| Scope | `requisition_id`, `requirement_id` | **Set on every write** |
| Event time | `occurred_at` | When it happened |
| Log time | `created_at` | When the row was written |
| Payload | `details` (JSON) | Type-specific extras |
| Curation | `quality_score`, `quality_classification`, `is_meaningful` | Existing (migration 081) |

**Canonical `activity_type` enum:** `rfq_sent`, `email_received`, `call_logged`,
`status_changed`, `offer_created`, `offer_status_changed`, `sighting_added`, `sales_note`,
`task_completed`, `assignment_changed`, `req_archived` (longest = 20 chars, fits).

**Plan 1 requires no schema migration.** Should a later build step need a new column,
it will be additive only — flag any non-additive change before running it.

### Single write path

`app/services/activity_service.py` already has `log_rfq_activity()` (line 657) — a
requisition-aware writer. Rather than duplicate it, generalize it into the canonical
helper `log_activity()` and keep `log_rfq_activity()` as a thin delegating alias so
existing callers (`sightings.py:990`) keep working:

```python
def log_activity(db, *, activity_type, channel, requisition_id, requirement_id=None,
                 user_id=None, company_id=None, vendor_card_id=None, vendor_contact_id=None,
                 description=None, summary=None, occurred_at=None, details=None):
    ...
```

Every writer — system events, webhooks, jobs — goes through this. Existing
`log_email_activity` / `log_call_activity` gain optional `requisition_id` / `requirement_id`
parameters. New read helper `get_requisition_activities(db, req_id, ...)` replaces the
inlined query in `htmx_views.py:1269-1273`.

## Hybrid AI curation

Log every event; curate what surfaces:

- **Rule-based (no AI):** inherently meaningful events — `status_changed`, `offer_created`,
  `assignment_changed`, `rfq_sent`, `call_logged`, `req_archived` — set `is_meaningful=True`
  directly. Cheap, deterministic.
- **AI-scored:** high-volume / free-text events — `sighting_added` and `email_received` —
  get a `quality_score` and `is_meaningful` flag from the existing quality-scoring pass,
  extended to these event types.
- **Aggregation:** `sighting_added` rows from one search batch collapse into a single
  timeline entry ("12 sightings added from <source>") rather than 12 rows.
- **Display:** timeline shows `is_meaningful=True` events by default with a "Show all"
  toggle to reveal the rest.

## Components & data flow

```
mutation point ──► log_activity() ──► activity_log row
  (status, offer, sighting, note, task, assignment, archive)

Graph sent-folder scan ──► log_activity(source=graph, event_type=rfq_sent)
VendorResponse create  ──► log_activity(source=graph, event_type=email_received)
8x8 CDR poll           ──► log_activity(source=8x8,  event_type=call_logged)

quality-scoring pass ──► sets quality_score / is_meaningful on AI-scored event types

Activity tab ──► get_requisition_activities() ──► activity.html timeline
```

## Build order (simplest stable wins first)

1. **Unify write path** — add `log_activity()` + `get_requisition_activities()`; refactor
   `log_email_activity`/`log_call_activity` to take `requisition_id`; confirm/fix the
   subject-tag mismatch. Smallest change that makes the feed non-empty.
2. **Wire 13 system events** — add `log_activity()` calls at each mutation point (offers,
   sightings, sales notes, task completion, assignment changes, archive/unarchive, offer
   status). Tests alongside each.
3. **Bridge inbound email** — write an `activity_log` row when a `VendorResponse` is
   created/parsed (`event_type=email_received`, `source=graph`).
4. **AI curation layer** — extend quality scoring to `sighting_added` + `email_received`;
   implement sighting-batch aggregation; default the timeline to `is_meaningful`.
5. **Enable 8x8** — set `eight_by_eight_enabled=True` + supply credentials (config/ops).
6. **Frontend timeline** — unified chronological render with source icons, vendor labels,
   meaningful-default + "Show all" toggle, date grouping.

## Testing

- Tests alongside all business logic (pytest). `log_activity()` unit tests; per-event-source
  tests asserting a row is written with correct `requisition_id` + `event_type`; read-helper
  tests; aggregation tests for sighting batches.

## Constraints

- Loguru for logging, not `print`.
- Additive migration only — flag any destructive change before running it.
- Simple over clever: one `activity_log`, one `log_activity()`, no event framework.
- Update the relevant `docs/APP_MAP*` doc(s) after implementation.
