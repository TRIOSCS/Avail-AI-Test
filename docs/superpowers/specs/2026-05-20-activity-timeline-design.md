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
- **To confirm during build:** possible subject-tag mismatch — RFQ send appears to tag
  `[ref:{req_id}]` while `app/jobs/email_jobs.py:scan_sent_folder()` looks for
  `[AVAIL-{id}]`. Open both files and verify before relying on outbound-email linkage.

The DB is intentionally empty right now — no historical backfill needed; the fix is
forward-only.

## Approach: extend `activity_log`, do not build a framework

The existing `activity_log` table (`app/models/intelligence.py:257-375`) already holds every
field a unified timeline needs. We make it canonical rather than introducing a new table.

### Canonical schema (additive migration only — non-destructive)

| Concept | Column | Notes |
|---|---|---|
| Event type | `event_type` | Canonical enum (below) |
| Source | `source` | `graph` \| `8x8` \| `system` \| `manual` (reuse `channel`) |
| Actor | `user_id` | Nullable — null for automated/system events |
| Vendor/contact | `vendor_card_id`, `vendor_contact_id`, `company_id`, `site_contact_id` | Existing |
| Scope | `requisition_id`, `requirement_id` | **Set on every write** |
| Event time | `occurred_at` | When it happened |
| Log time | `created_at` | When the row was written |
| Payload | `details` (JSON) | Type-specific extras |
| Curation | `quality_score`, `quality_classification`, `is_meaningful` | Existing (migration 081) |

**Canonical `event_type` enum:** `rfq_sent`, `email_received`, `call_logged`,
`status_changed`, `offer_created`, `offer_status_changed`, `sighting_added`, `sales_note`,
`task_completed`, `assignment_changed`, `req_archived`.

If any column needs a non-additive change, flag it before running the migration.

### Single write path

One mandatory helper in `app/services/activity_service.py`:

```python
def log_activity(db, *, event_type, source, requisition_id, requirement_id=None,
                 actor_id=None, vendor_card_id=None, vendor_contact_id=None,
                 company_id=None, occurred_at=None, summary=None, details=None):
    ...
```

Every writer — system events, webhooks, jobs — goes through this. Existing
`log_email_activity` / `log_call_activity` are refactored to delegate to it and to accept
`requisition_id`. New read helper `get_requisition_activities(db, req_id, ...)` replaces the
inlined query in `htmx_views.py`.

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
