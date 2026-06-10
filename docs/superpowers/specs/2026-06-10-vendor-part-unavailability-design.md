# Durable Vendor+Part Unavailability Knowledge — Design

**Date:** 2026-06-10
**Status:** Approved (user selected "Durable knowledge record" from 3 presented options)
**Builds on:** the sightings row-treatment PR #260 (visual layer stays as shipped).

## Problem

"Unavailable" is intended as *learned vendor intelligence*: we know the parts are gone
(we bought them, vendor sold them, broken, phantom listing, different part number, …),
so we never contact the vendor about that part again — but we keep a record of what we
learned. The current implementation is a bare `is_unavailable` boolean on scraped
`Sighting` rows, which fails that intent three ways:

1. **No record of what we learned** — no reason, note, who, when, or activity entry.
2. **Not durable** — every re-search deletes + recreates sightings for sources that
   returned (`search_service.py` connector-aware delete), and fresh rows default to
   available, so the marked vendor resurrects with full RFQ actions.
3. **"Don't call again" not enforced** — the RFQ vendor modal's suggested-vendors query
   filters only `is_blacklisted`; unavailable vendors are still suggested for the part.

## Decision

Model unavailability as a first-class fact about **(vendor, part)** that outlives any
scraped row, with reason + note + provenance, applied automatically to fresh search
results, enforced in RFQ suggestions, surfaced on the row, logged to the activity
timeline, and explicitly undoable.

## Data model

New table `vendor_part_unavailability`, model `VendorPartUnavailability` in
`app/models/vendor_part_unavailability.py` (new file, header comment per convention):

| column | type | notes |
|---|---|---|
| `id` | Integer PK | |
| `vendor_name_normalized` | String(255), not null, indexed | via `normalize_vendor_name()` (`app/vendor_utils.py`) |
| `normalized_mpn` | String(255), not null, indexed | via `normalize_mpn_key()` (`app/utils/normalization.py`) — same canonical dash-stripped key offers use |
| `reason` | String(32), not null | values from new `UnavailabilityReason` StrEnum |
| `note` | Text, nullable | free-text "what we learned" |
| `created_by_id` | FK `users.id`, nullable, `ondelete="SET NULL"` | |
| `created_at` | UTCDateTime, Python default **and** server default now (dual-default sibling pattern — avoids None-before-flush in tests) | |

Unique constraint on (`vendor_name_normalized`, `normalized_mpn`). Marking again for an
existing key is an **update** (reason/note/created_by/created_at refreshed), not an error.

New `UnavailabilityReason(StrEnum)` in `app/constants.py`, with display labels via a
`.label` property on the enum (single source of truth — templates/services use it):
`BOUGHT_BY_US = "bought_by_us"` ("We bought them"), `SOLD_ELSEWHERE = "sold_elsewhere"`
("Vendor sold them"), `BROKEN = "broken"` ("Broken / bad condition"),
`NOT_REALLY_THERE = "not_really_there"` ("Not really in stock"),
`DIFFERENT_PART = "different_part"` ("Different part number"), `OTHER = "other"` ("Other").

Alembic migration: autogenerate, revision id ≤32 chars, verify single head, include
downgrade (drop table). The per-sighting `Sighting.is_unavailable` column **stays** —
it remains the row-level display/aggregation flag (consumed by
`sighting_aggregation.py`, `material_card_service.py`, requisitions toggle); the new
table is the durable source of truth that keeps re-stamping it.

## Service layer

New `app/services/vendor_unavailability.py` (all business logic here; routers stay thin):

- `_keys_for_vendor(requirement, sightings) -> set[str]` (internal): the normalized MPN
  keys this vendor+requirement covers = `normalize_mpn_key(s.mpn_matched)` for each of
  the vendor's sightings with an MPN, plus `normalize_mpn_key(requirement.primary_mpn)`
  always. Empty/None keys are skipped.
- `record_unavailability(db, requirement, vendor_name, reason, note, user) -> int`:
  upserts one record per key (unique-key update semantics above); sets
  `is_unavailable=True` on all the vendor's sightings for the requirement, matched via
  `Sighting.vendor_name_normalized == normalize_vendor_name(vendor_name)` — NOT the
  current route's `lower(trim(...))` comparison, which misses vendors with legal
  suffixes ("X, Inc.") and is hereby replaced (architect finding 1); writes ONE
  `ActivityLog` entry
  (follow the existing direct-construction pattern in `app/routers/sightings.py`,
  e.g. the entries near the offer/RFQ actions) with vendor, reason label, note, MPN.
  Returns number of records written. Does NOT commit (caller commits).
- `clear_unavailability(db, requirement, vendor_name, user) -> int`: deletes records
  for (vendor, all keys for this requirement); sets `is_unavailable=False` on the
  vendor's sightings for the requirement; writes an ActivityLog entry ("marked
  available again"). History of what we learned survives in the activity timeline.
- `unavailability_for_requirement(db, requirement, vendor_names) -> dict[str, record]`:
  vendor display name → most-recent matching record (for rendering reason on rows).
  One batched query (no N+1).
- `apply_to_fresh_sightings(db, requirement, sightings) -> int`: given just-created
  Sighting ORM objects, one batched query over (vendor_norm, key) pairs; sets
  `is_unavailable=True` on matches. Returns count.
- `excluded_vendor_norms(db, requirements) -> set[str]`: vendor norms having a record
  whose `normalized_mpn` is in the requirements' primary-MPN keys. (Deliberate
  boundary: exclusion matches on primary MPN keys of the selected requirements —
  substitute-MPN exclusion is not attempted here.)

### Status computation

`compute_vendor_statuses` (`app/services/sighting_status.py`) Batch 4 becomes:
vendor is `unavailable` if **either** all its sighting rows are flagged (legacy row
flag — keeps the requisitions-page per-sighting toggle working) **or** a
`VendorPartUnavailability` record matches (vendor_norm, any key from that vendor's
sightings' MPNs ∪ requirement primary key). The legacy branch is re-anchored on
`Sighting.vendor_name_normalized` (grouped by normalized name) instead of raw
`vendor_name` — fixes a pre-existing silent miss when summary and sighting names
drift in case/whitespace (architect finding 2). Precedence order is unchanged:
`blacklisted > offer-in > contacted > unavailable > sighting` (offer-in still
dominates — already pinned by test).

### Re-application at EVERY sighting-persistence path

Invariant: **no fresh `Sighting` row ever contradicts a durable record.** Five code
paths persist new sightings (architect finding 3); each calls
`apply_to_fresh_sightings(...)` with its OWN session right where the rows are created:

1. `app/search_service.py` — after the fresh-`Sighting` construction loop that follows
   the connector-aware delete, **inside search's separate write session** (the
   CLAUDE.md session caveat is the trap). This is the synchronous resurrection hole.
2. `app/services/ics_worker/sighting_writer.py` — async ICS browser worker; injection
   at the end of its save loop, its own session. Without this, ICS results arriving
   after a search re-open the hole.
3. `app/services/nc_worker/sighting_writer.py` — same, NetComponents worker.
4. `app/routers/sources.py` email-attachment import.
5. `app/routers/htmx_views.py` add-to-requisition picker — deliberately included: a
   manually added sighting for a known-dead vendor+part renders flagged with its
   reason; the user can Mark available to override, so knowledge is surfaced, never
   silently bypassed. (`app/jobs/inventory_jobs.py` creates excess-list sightings —
   include it the same way; group rows per requirement before calling.)

## HTTP layer (`app/routers/sightings.py`)

- `GET /v2/partials/sightings/{requirement_id}/unavailable-form?vendor_name=…` → small
  modal partial (reason radio list from `UnavailabilityReason`, optional note textarea,
  submit + cancel). Served through the existing `open-modal` dispatch pattern.
- `POST /v2/partials/sightings/{requirement_id}/mark-unavailable` (existing route,
  extended): now accepts `reason` (required, validated against the enum) and `note`
  (optional) form fields; delegates to `record_unavailability`; keeps the existing
  `source` SSE param + `_publish_if_user_source` behavior; still re-renders the detail
  panel. 400 on missing vendor_name (unchanged) or invalid reason.
- `POST /v2/partials/sightings/{requirement_id}/mark-available` (new): vendor_name form
  field; delegates to `clear_unavailability`; same SSE publish + detail re-render.
- Detail view: fetch `unavailability_for_requirement(...)` once and pass
  `unavailable_intel` (vendor name → record) into the template context.
- RFQ vendor modal (`sightings_vendor_modal`): suggested-vendors query additionally
  excludes vendors in `excluded_vendor_norms(db, requirements)` (alongside the
  existing blacklist filter). Multi-requirement semantics: excluded if unavailable for
  ANY selected part (deliberately conservative — documented, not accidental).
- Send-time re-validation (closes the TOCTOU the modal filter alone leaves open):
  `sightings_send_inquiry` and `sightings_preview_inquiry` re-check submitted
  `vendor_names` against `excluded_vendor_norms` at request time; excluded vendors are
  dropped from the send AND visibly reported in the response (follow the existing
  skipped-vendor reporting style used by batch RFQ — never a silent drop).

## UI (`_vendor_row.html` — additive to the shipped row treatment)

- The "Mark Unavail" button switches from `hx-post`+`hx-confirm` to
  `$dispatch('open-modal', {url: '...unavailable-form?vendor_name=…'})` (same pattern
  as the offer-form button next to it).
- Unavailable rows (which currently render no action buttons): show one small action,
  `Mark available` (`hx-post` to the new endpoint, `hx-confirm`, same styling family as
  the current "Mark Unavail" link), plus the reason inline in the metrics line:
  `<span class="text-rose-400">{{ reason label }}</span>` (truncate note to one line if
  present, full text in the title attribute).
- Expanded detail panel of an unavailable row gains a "What we learned:" grid entry —
  reason label + note + date (and user name when available).
- New modal partial `app/templates/htmx/partials/sightings/unavailable_form.html`
  (header comment; single-quoted Alpine attributes where Jinja values are embedded —
  repo landmine; no double quotes inside double-quoted Alpine attrs).
- The mobile/desktop split needs no special handling (vendor rows render identically).

## Out of scope (deliberate boundaries — say so in the PR)

- The requisitions-page per-sighting toggle (`PATCH …/sightings/{id}/unavailable` in
  `app/routers/requisitions/requirements.py`) stays row-level only; the sightings
  workspace is the canonical surface for vendor+part knowledge.
- No vendor-level "never contact for anything" semantics — that's what blacklist is for.
- Substitute-MPN matching in RFQ-modal exclusion (primary-key matching only, see above).
- No backfill of reasons for rows already flagged before this ships (legacy flags keep
  working via the row-flag OR-branch in status computation).

## Testing

- **Service** (`tests/test_vendor_unavailability.py`, new): upsert semantics (second
  mark updates, not duplicates); clear deletes + unflags; `apply_to_fresh_sightings`
  re-marks a recreated sighting (simulate delete+recreate, the resurrection scenario);
  `excluded_vendor_norms` matches on primary key; keys include both matched-MPN and
  primary-MPN; ActivityLog rows written on record + clear.
- **Status** (extend existing status/router tests): durable record alone (no row flags)
  → vendor status `unavailable`; offer-in still dominates a record.
- **Routes** (`tests/test_sightings_router.py`): mark with reason+note → 200, detail
  shows rose row + reason label; invalid reason → 400; mark-available → row back to
  normal; unavailable-form renders all six reasons; RFQ vendor modal excludes the
  marked vendor for that requirement and still shows it for an unrelated requirement;
  mark works for a suffixed vendor name ("X, Inc." — the normalization fix);
  send-inquiry with an excluded vendor drops it and reports the skip in the response.
- **Async writers**: resurrection test for at least one of the ICS/NC sighting
  writers (record exists → writer saves fresh rows → rows come back flagged).
- **Migration**: upgrade → downgrade → upgrade locally; `alembic heads` single head;
  revision id length guard (existing test covers).

## Risks

- SQLite tests tolerate Postgres-invalid SQL — any new query with JSON/DISTINCT
  subtleties must be sanity-checked against live PG after deploy (known class).
- The search-session boundary: re-application MUST run in search's own write session or
  the stamps silently vanish with the session.
- `ondelete="SET NULL"` on created_by keeps records when users are removed (knowledge
  outlives accounts).
