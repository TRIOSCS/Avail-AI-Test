# Durable Vendor+Part Unavailability Knowledge — Design

**Date:** 2026-06-10
**Status:** Approved (user selected "Durable knowledge record" from 3 presented options).
Amended same day with the adopted temporal policy ("Two Windows, Real Proof" — see
`docs/superpowers/specs/2026-06-10-unavailability-temporal-policy.md`, authoritative
for windows/overrides/release semantics), the silent-failure hardening dispositions,
and the three-state UI design.
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
timeline, and explicitly undoable. Suppression is **time-bounded per reason class and
releasable by real proof** per the adopted temporal policy: marks expire into a
labeled advisory state (never silently forever), and live-catalog evidence, a
buyer-routed vendor email, an incoming offer, or a ≥2.0× qty jump surface the vendor
early — always labeled, never silent.

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
| `qty_at_mark` | Integer, nullable | **per-key snapshot** at mark/re-mark: max non-NULL `qty_available` over the vendor's sightings whose `normalize_mpn_key(mpn_matched)` equals THIS record's key (rows with empty `mpn_matched` count toward the primary-key record, mirroring `apply_to_fresh_sightings`' key fallback). Never max-across-all-keys, never a cross-key fallback. **Re-mark rule: keep the old value when the new computation is NULL** (no cross-requirement clobber). |
| `released_at` | UTCDateTime, nullable | written ONLY by override O3 and the offer hook (both user-initiated paths); NULLed on re-mark |
| `release_trigger` | String(32), nullable | `'vendor_email'` \| `'offer_received'` — renders the advisory hint copy; NULLed on re-mark |
| `requirement_id` | FK `requirements.id`, nullable, **indexed**, `ondelete="SET NULL"` | **provenance** (IMPORTANT-3): the requirement the mark was made from. SET NULL (not CASCADE — knowledge outlives requirements). Lets `clear_unavailability` find records whose key no longer matches the requirement's current keys (zombie-record fix). Refreshed on re-mark. |

Unique constraint on (`vendor_name_normalized`, `normalized_mpn`). Marking again for an
existing key is an **update** (reason/note/created_by/created_at refreshed;
`released_at`/`release_trigger` NULLed; `qty_at_mark` re-snapshot with keep-old-on-NULL;
`requirement_id` refreshed), not an error.

New `UnavailabilityReason(StrEnum)` in `app/constants.py`, with display labels via a
`.label` property on the enum (single source of truth — templates/services use it):
`BOUGHT_BY_US = "bought_by_us"` ("We bought them"), `SOLD_ELSEWHERE = "sold_elsewhere"`
("Vendor sold them"), `BROKEN = "broken"` ("Broken / bad condition"),
`NOT_REALLY_THERE = "not_really_there"` ("Not really in stock"),
`DIFFERENT_PART = "different_part"` ("Different part number"), `OTHER = "other"` ("Other").

**Migrations:** 097 (`097_vendor_part_unavailability`, shipped) creates the base
table. **ONE new migration (098)** adds the four nullable columns above
(`qty_at_mark`, `released_at`, `release_trigger`, `requirement_id` — policy columns
and the provenance column ride together): autogenerate, revision id ≤32 chars, verify
single head, downgrade drops the columns. No backfill — legacy records have NULL
`qty_at_mark` so override O2 never fires for them; they ride the windows (fail-closed).

The per-sighting `Sighting.is_unavailable` column **stays** — but it is demoted to a
**render cache**. The record predicate (`is_active`) is the only authority; see
"Reader-side authority rule" below. It remains consumed by `sighting_aggregation.py`,
`material_card_service.py`, and the requisitions toggle; the durable table keeps
re-stamping it.

## Active-suppression policy (adopted: "Two Windows, Real Proof")

Authoritative detail, rationale, scenario table, and accepted limitations:
`docs/superpowers/specs/2026-06-10-unavailability-temporal-policy.md`. Normative
summary (the implementation contract):

### Reason classes and windows

| class | reasons | active window |
|---|---|---|
| LOT | `bought_by_us`, `sold_elsewhere`, `broken`, `other` | **30d** (knob `unavailability_suppress_days`) |
| LISTING | `not_really_there` | **180d** (knob `unavailability_listing_suppress_days`) |
| IDENTITY | `different_part` | **never expires** (hard-coded constant, not a knob) |

Active predicate (pure read-time, Python-computed cutoffs, no cron, no lazy writes):
`released_at IS NULL AND (reason == different_part OR created_at >= now − window(reason))`.

### Source trust classes

Module-level `Final` constants computed from `source_type`/`is_authorized` — never the
stored `evidence_tier` (NULL on 4 of 6 paths):

- **LIVE** = `source_type ∈ {digikey, mouser, element14}` OR `is_authorized is True`.
- **HUMAN_DIRECT** = `{email_attachment}` only (buyer-routed, user-initiated path).
- **LISTING-CLASS** = **default for everything else**, including `excess_list`,
  `email_auto_import`, `stock_list`, `historical`, `vendor_affinity`, `""`/unknown.
  Unknown types can only be stamped, never trigger a release.

### Suppression matrix (per fresh row matching a record, in `apply_to_fresh_sightings`)

Record **not active** (window lapsed or released): never stamp; row renders advisory.
Record **active**: **dispatch on the row's source class — NOT priority order**. The
three overrides apply to mutually exclusive classes (LIVE → O1 only, HUMAN_DIRECT →
O3 only, listing-class → O2 only), so the stronger evidence class always wins: a
HUMAN_DIRECT row whose qty also clears the O2 jump still releases the record (S7),
and O1 already subsumes any O2-shaped signal on LIVE rows.

- **O1 Live truth** (class LIVE) — `qty_available > 0` AND
  `qty_available != qty_at_mark` (NULL snapshot ⇒ passes) → leave unstamped
  (advisory+nudge). Applies to ALL reasons incl. `different_part`.
- **O2 Restock** (listing-class, the default) — `qty_at_mark` non-NULL AND fresh qty
  non-NULL AND fresh `>` snapshot AND fresh `>= snapshot × 2.0` (knob; strict-greater
  always required); snapshot 0 ⇒ any fresh > 0; NULL either side = no signal. Leave
  unstamped (advisory+nudge), **no record mutation** (stateless, self-healing).
  LOT+LISTING only.
- **O3 Vendor document** (class HUMAN_DIRECT) — `qty_available > 0` →
  **record-level release** (`released_at=now`, `release_trigger='vendor_email'`, one
  ActivityLog line), stamp nothing. LOT+LISTING only.
- **else** — a row whose class's override doesn't fire → stamp `is_unavailable=True`.

**Offer hook:** user-initiated offer proof — a person entering, saving, or approving
an offer — releases matching active records via the shared
`maybe_release_on_offer(...)` gate (`release_trigger='offer_received'`), all reasons
except `different_part`. It fires at the user-initiated sites (canonical
`create_offer` incl. the sightings route that delegates to it, manual `add_offer`,
the save-parsed-offers route, `save_freeform_offers`, and pending-review approval —
`approve_offer` plus its three approval twins: the htmx review-queue promote, the
T4→T5 API promote, and the offers-tab review approve) and never from auto-created
(inbox monitor, excess matching) or clone paths. Availability evidence (qty, email,
offer) never releases identity knowledge; only LIVE catalog evidence or manual clear
does.

### Reader-side authority rule

**The record predicate is the only authority; `Sighting.is_unavailable` is a render
cache.** One helper `is_active(record, now)` used identically by all read surfaces:

1. **Row render:** stamped row + active record → suppressed; stamped row +
   non-active/no-longer-matching record → advisory style *even though the flag is
   stale*; unstamped row + record (active or not) → advisory+nudge; no record → normal.
2. **Batch 4 pill:** vendor is `unavailable` iff `(active record matches AND vendor
   has NO unstamped row)` OR `(no record at all AND all rows flagged — true legacy)`.
3. **RFQ:** `excluded_vendor_norms` + send/preview re-checks consider **active records
   only** (Python `is_active` filter over fetched rows).

Races across writers produce at most a stale flag that every reader reinterprets
correctly on next render — no reconciliation pass, no read-path writes.

### Config knobs (`app/config.py`, validated, no 0-sentinel)

```python
unavailability_suppress_days: int = Field(30, ge=1)
unavailability_listing_suppress_days: int = Field(180, ge=1)
unavailability_qty_jump_factor: float = Field(2.0, ge=1.0)
```

## Service layer

New `app/services/vendor_unavailability.py` (all business logic here; routers stay thin):

- **Policy helpers** (module-level): `LOT_REASONS`, `LIVE_SOURCES`,
  `HUMAN_DIRECT_SOURCES` as `Final` constants; `_source_class(sighting)` (default
  listing-class); `_window_days(reason)` (reads settings; `different_part` → `None`);
  `is_active(record, now)` (THE shared predicate — status, RFQ exclusion, row render,
  apply all use it); `_override_verdict(record, sighting)` implementing the
  class-dispatched O1/O2/O3 verdict (stamp / surface / release).
- **ONE sighting-matching helper (CRITICAL-2):** all sighting matching — record,
  clear, apply, status — goes through a single Python-side helper with the NULL-norm
  fallback (`_sighting_norm` semantics: `vendor_name_normalized or
  normalize_vendor_name(vendor_name)`). **No strict-equality-only column matches
  anywhere** — a column-only filter silently misses legacy rows whose
  `vendor_name_normalized` is NULL, leaving zombie flags.
- `_keys_for_vendor(requirement, sightings) -> set[str]` (internal): the normalized MPN
  keys this vendor+requirement covers = `normalize_mpn_key(s.mpn_matched)` for each of
  the vendor's sightings with an MPN, plus `normalize_mpn_key(requirement.primary_mpn)`
  always. Empty/None keys are skipped.
- `record_unavailability(db, requirement, vendor_name, reason, note, user) -> int`:
  **raises `ValueError` if `normalize_vendor_name(vendor_name)` is empty
  (IMPORTANT-4)** and **raises `ValueError` when zero MPN keys are derivable — no
  primary-MPN key and no matched-sighting keys (CRITICAL-1)**; in both cases nothing
  is written, including **no ActivityLog entry**. Otherwise upserts one record per key
  (unique-key update semantics above, incl. per-key `qty_at_mark` snapshot with
  keep-old-on-NULL, NULLing `released_at`/`release_trigger`, refreshing
  `requirement_id` provenance); sets `is_unavailable=True` on all the vendor's
  sightings for the requirement via the shared matching helper (NOT the old route's
  `lower(trim(...))` comparison — architect finding 1 — and NOT a bare
  normalized-column equality); writes ONE `ActivityLog` entry (follow the existing
  direct-construction pattern in `app/routers/sightings.py`) with vendor, reason
  label, note, MPN — **the note never interpolates a None MPN: fall back to a matched
  MPN or `"requirement #<id>"` (MINOR-7)**. Returns number of records written. Does
  NOT commit (caller commits).
- `clear_unavailability(db, requirement, vendor_name, user) -> int`: raises
  `ValueError` on empty vendor norm (IMPORTANT-4). Deletes records matching
  `vendor_name_normalized == vendor_norm AND (normalized_mpn IN current keys OR
  requirement_id == requirement.id)` — the provenance arm (IMPORTANT-3) catches
  records whose key no longer matches the requirement's current keys (e.g. the marked
  sightings were deleted, so `_keys_for_vendor` shrank — without it the record is an
  unclearable zombie). Sets `is_unavailable=False` on the vendor's sightings for the
  requirement via the shared matching helper (this is the NULL-norm zombie-clear fix);
  writes an ActivityLog entry ("marked available again", same MINOR-7 MPN-fallback
  rule). DELETE semantics are deliberate: explicit human "forget it"; history survives
  in the activity timeline. Auto-expiry and O1/O2 never delete.
- `unavailability_for_requirement(db, requirement, vendor_names) -> dict[str, record]`:
  vendor display name → most-recent matching record (for rendering reason on rows).
  One batched query (no N+1). Result is annotated for templates with the computed
  policy state (`is_active`, window age, release trigger) so Jinja renders the three
  row states without re-deriving policy.
- `apply_to_fresh_sightings(db, requirement, sightings) -> int`: given just-created
  Sighting ORM objects, one batched query fetching **full records** (not bare key
  pairs) into a dict keyed `(vendor_norm, key)`. Each sighting matches on its
  **candidate-key SET `{normalize_mpn_key(mpn_matched), primary_key}` (both
  non-empty, IMPORTANT-5)** — mirroring status semantics, so a row whose
  `mpn_matched` normalizes differently from the record key still matches via the
  primary key. Per matched row, apply the suppression matrix dispatched on the row's
  source class (LIVE → O1, HUMAN_DIRECT → O3, listing → O2): non-active record →
  skip (advisory rendering happens reader-side); O3 → record release + ActivityLog;
  O1/O2 → leave unstamped; else stamp. Returns count stamped.
- `release_on_offer(db, requirement, vendor_name, user) -> int` (new): releases
  matching **active** records for the vendor across the requirement's keys —
  `released_at=now`, `release_trigger='offer_received'`, ActivityLog — all reasons
  except `different_part`. Called via the `maybe_release_on_offer(...)` gate from
  the five user-initiated offer sites. No-op (0) when nothing matches.
- `excluded_vendor_norms(db, requirements) -> set[str]`: vendor norms having an
  **active** record (fetch full rows, Python-filter with `is_active`) whose
  `normalized_mpn` is in the requirements' primary-MPN keys. (Deliberate boundary:
  exclusion matches on primary MPN keys of the selected requirements —
  substitute-MPN exclusion is not attempted here.) **When a requirement contributes
  no derivable key, log a warning (IMPORTANT-6)** — given CRITICAL-1's raise such
  records shouldn't exist, but a requirement whose `primary_mpn` was edited to an
  unkeyable value must not silently widen RFQ suggestions.

### Status computation

`compute_vendor_statuses` (`app/services/sighting_status.py`) Batch 4 becomes the
reader-authority rule: fetch the full matching record rows (vendor_norm × candidate
keys from that vendor's sightings' MPNs ∪ requirement primary key, via the shared
matching helper); vendor is `unavailable` **iff** `(an active record matches AND the
vendor has NO unstamped sighting row)` **OR** `(no matching record at all AND all rows
flagged — true legacy)`. Consequences: rows-win (one override-surfaced row flips the
pill off "unavailable"); an expired/released record's stale stamped rows no longer pin
the pill; and the legacy all-rows-flagged branch is **restricted to vendors with no
record** — a deliberate strictening of the v1 OR-semantics. **MINOR-9: the PR
description must call out this legacy-branch strictening, and a test pins the
mixed-variant case** (vendor with a record + a mix of stamped and unstamped rows →
NOT `unavailable`). The legacy branch stays anchored on the shared normalized-name
helper (architect finding 2 — fixes the case/whitespace drift miss).
**MINOR-8: log a warning when the requirement row referenced by a status computation
is missing** instead of silently treating it as key-less. Precedence order:
`blacklisted > offer-in > unavailable > contacted > sighting` — contacted is a step;
unavailable is its answer: a mark made after contacting must be visible (offer-in
still dominates everything but blacklisted — pinned by test).

### Re-application at EVERY sighting-persistence path

Invariant: **no fresh `Sighting` row ever contradicts an _active_ record; overrides
O1–O3, window expiry, offer release, and manual clear all land in the advisory +
verify state — labeled, never silent.** Eight code paths persist new sightings
(architect finding 3 + the two writers found in the silent-failure batch); each calls
`apply_to_fresh_sightings(...)` — which now embeds the O1/O2/O3 policy matrix — with
its OWN session right where the rows are created:

1. `app/search_service.py` — after the fresh-`Sighting` construction loop that follows
   the connector-aware delete, **inside search's separate write session** (the
   CLAUDE.md session caveat is the trap). This is the synchronous resurrection hole.
2. `app/services/ics_worker/sighting_writer.py` — async ICS browser worker; injection
   at the end of its save loop, its own session. Without this, ICS results arriving
   after a search re-open the hole.
3. `app/services/nc_worker/sighting_writer.py` — same, NetComponents worker.
4. `app/routers/sources.py` email-attachment import — note this is also the
   HUMAN_DIRECT path: a buyer-routed attachment with qty>0 triggers override O3
   (record release) instead of stamping. A RE-SENT attachment that hits the dedup
   key (requirement, vendor, MPN, source_type) refreshes the existing row's
   qty/price from the new parse and joins the apply batch, so O3 still fires —
   never a silent skip.
5. `app/routers/htmx_views.py` add-to-requisition picker — deliberately included: a
   manually added sighting for a known-dead vendor+part renders flagged with its
   reason; the user can Mark available to override, so knowledge is surfaced, never
   silently bypassed. (`app/jobs/inventory_jobs.py` creates excess-list sightings —
   include it the same way; group rows per requirement before calling.)
6. `app/routers/requisitions/requirements.py` `import_stock_list` — the manual
   vendor stock-list import; rows grouped per requirement (the inventory_jobs
   pattern), applied before the commit.
7. `app/services/search_worker_base/queue_manager.py` — the ICS/NC
   cross-requirement dedup clones prior sightings onto a NEW requirement;
   applied before its commit (the req object is in scope).

## HTTP layer (`app/routers/sightings.py`)

- `GET /v2/partials/sightings/{requirement_id}/unavailable-form?vendor_name=…` → small
  modal partial (reason radio list from `UnavailabilityReason`, optional note textarea,
  submit + cancel). Served through the existing `open-modal` dispatch pattern. The
  modal copy MUST include the accepted-limitation caveat: **"applies to all of this
  vendor's listings of this MPN"** (condition/variant key collapse — NEW and REFURB
  share one record). The same modal IS the re-arm affordance (see UI states 2/3).
- `POST /v2/partials/sightings/{requirement_id}/mark-unavailable` (existing route,
  extended): now accepts `reason` (required, validated against the enum) and `note`
  (optional) form fields; delegates to `record_unavailability`; keeps the existing
  `source` SSE param + `_publish_if_user_source` behavior; re-renders the detail
  panel **with an appended OOB success toast** ("Marked {vendor} unavailable —
  {reason label}"). On the 400 paths (missing vendor_name, invalid reason, the
  service's `ValueError`s — zero derivable MPN keys CRITICAL-1 / empty vendor norm
  IMPORTANT-4), **htmx callers get the re-rendered detail plus the actionable
  message as an error toast; non-htmx/API callers keep the 400 JSON error
  (`{"error": ...}` format). No ActivityLog is written on those paths.**
  Re-POSTing for an already-marked vendor is the re-arm path (upsert refresh).
- `POST /v2/partials/sightings/{requirement_id}/mark-available` (new): vendor_name form
  field; delegates to `clear_unavailability`; same error mapping; same SSE
  publish + detail re-render with the "{vendor} marked available again" toast.
- **There is NO separate verify-availability endpoint.** The verify affordance in the
  UI maps onto the two existing actions: "Still unavailable" → re-arm = the
  mark-unavailable modal (upsert refresh); "It's back" → clear = mark-available.
- **Offer hook:** the canonical `create_offer` fires `maybe_release_on_offer(...)`
  itself after the offer is persisted (same transaction), so this route needs no
  hook call of its own — a user-entered offer is proof of availability and releases
  the records (except `different_part`).
- Detail view: fetch `unavailability_for_requirement(...)` once and pass
  `unavailable_intel` (vendor name → annotated record incl. `is_active`) into the
  template context.
- RFQ vendor modal (`sightings_vendor_modal`): suggested-vendors query additionally
  excludes vendors in `excluded_vendor_norms(db, requirements)` — **active records
  only** (alongside the existing blacklist filter). Expired/released/cleared → RFQ
  resumes. Multi-requirement semantics: excluded if unavailable for ANY selected part
  (deliberately conservative — documented, not accidental).
- Send-time re-validation (closes the TOCTOU the modal filter alone leaves open):
  `sightings_send_inquiry` and `sightings_preview_inquiry` re-check submitted
  `vendor_names` against `excluded_vendor_norms` (active-only) at request time;
  excluded vendors are dropped from the send AND visibly reported in the response
  (follow the existing skipped-vendor reporting style used by batch RFQ — never a
  silent drop).
- **RFQ during the active window:** override-surfaced rows (UI state 3) do NOT
  re-enable RFQ — the vendor stays in `excluded_vendor_norms` until expiry, release
  (offer/O3), or manual clear. The row-level verify affordance is the designed exit:
  "It's back" → clear → RFQ unblocks immediately. If a buyer nonetheless submits the
  vendor (e.g. typed manually), send-time re-validation drops it with the visible
  skip. One consistent answer: "you can email them when the window ends, an
  offer/email proves stock, or you clear the mark."

## UI (`_vendor_row.html` — additive to the shipped row treatment)

A vendor row now renders one of **three states**, keyed off the reader-authority rule
(`is_active` + the row's render-cache flag + override evaluation) via the annotated
`unavailable_intel` context — plus the no-record normal state. The "Mark Unavail"
button on normal rows switches from `hx-post`+`hx-confirm` to
`$dispatch('open-modal', {url: '...unavailable-form?vendor_name=…'})` (same pattern
as the offer-form button next to it).

### State 1 — Suppressed (active record, row stamped) — extends shipped treatment

- Row: `bg-rose-50/60 hover:bg-rose-50/80`; name/qty/score `text-gray-400`; status
  pill `bg-rose-100 text-rose-700` (all as shipped in PR #260).
- Metrics line adds: reason label `<span class="text-rose-400">{{ reason.label }}</span>`;
  note `<span class="text-rose-300 italic truncate max-w-[28ch] min-w-0"
  title="{{ full note }}">` (truncated, full text in `title`); age span
  `text-rose-300`; **"Mark available"** button — `text-[10px] text-gray-400
  hover:text-emerald-600 font-medium`, `hx-post` to mark-available, `hx-vals` with
  `vendor_name`, `hx-target` `#sightings-detail` innerHTML, `hx-confirm`,
  `data-loading-disable`, `@click.stop`.
- Expanded detail grid, first entry `col-span-2`: **"What we learned:"** — reason
  `text-rose-600 font-medium` + note italic + `· {user}, {age}`.
- Actions: RFQ / Convert / Mark Unavail **hidden**; the only action is Mark available.

### State 2 — Expired/released advisory (record not active)

- Row: **fully normal** (no tint, no dimming, normal pill).
- Metrics line: history hint `<span class="text-gray-400 italic truncate max-w-[36ch]
  min-w-0">Marked unavailable {age} — {reason label, lowercased}</span>`, `title` =
  full history incl. user + note (and release trigger when `release_trigger` is set —
  e.g. "released by offer"); **"Verify availability"** affordance `text-amber-600
  hover:text-amber-800`. The affordance maps to the two existing actions per the
  policy: "Still unavailable" → re-arm (opens the mark-unavailable modal, whose upsert
  refreshes the window) / "It's back" → clear (mark-available POST). No new endpoint.
- Expanded grid: `col-span-2` **"History:"** gray entry (reason, note, user, age,
  release trigger if any).
- Actions: **full trio restored** (RFQ / Convert / Mark Unavail); Mark Unavail doubles
  as the re-arm. RFQ genuinely works here — the record is not active, so the vendor is
  out of `excluded_vendor_norms`.

### State 3 — Possible restock (active record, row left unstamped by O1/O2)

- Row: normal, **NO tint**.
- Line 1, chip after the status pill: bordered `bg-emerald-50 text-emerald-700 border
  border-emerald-200 rounded-full` **"Possible restock"**, `title` = the evidence
  (e.g. "qty 120 → 500, live distributor stock").
- Metrics line: qty delta `{old} → {new}` in `font-mono text-emerald-600`; compressed
  history echo `text-gray-400 italic truncate max-w-[24ch]` — `was: {reason label,
  lowercased}, {age}`; **"Verify restock"** link `text-emerald-700
  hover:text-emerald-900` — same two-action mapping as state 2 ("Still unavailable" →
  re-arm modal; "It's back" → clear).
- Expanded grid: the History entry + **"Changed:"** qty old→new (emerald mono) + seen
  age.
- Actions: full trio + verify; Mark Unavail doubles as the false-alarm re-arm (upsert
  re-snapshots `qty_at_mark` to the just-seen qty — one click buys a full quiet
  window). **RFQ caveat:** the record is still ACTIVE, so the vendor remains
  RFQ-excluded (modal suggestion + send re-validation with visible skip) until the
  buyer clears, an offer/email releases, or the window ends — the verify affordance is
  the designed path (see HTTP layer).

### Action-button matrix

| state | RFQ | Convert | Mark Unavail | Mark available | Verify |
|---|---|---|---|---|---|
| normal (no record) | ✓ | ✓ | ✓ (opens reason modal) | — | — |
| 1 suppressed | — | — | — | ✓ (only action) | — |
| 2 expired advisory | ✓ | ✓ | ✓ (= re-arm) | — | ✓ "Verify availability" (amber) |
| 3 possible restock | ✓* | ✓ | ✓ (= false-alarm re-arm) | — | ✓ "Verify restock" (emerald) |

\* rendered, but vendor stays excluded from RFQ suggestion + send while the record is
active; send re-validation reports the skip visibly.

### Color-collision resolution

- **Amber:** the OOO/contacted treatment owns the amber pill and stale-price owns
  amber inside the price span. The advisory hint is therefore **gray italic**; amber
  appears ONLY on the verify action link (`text-amber-600 hover:text-amber-800`).
  Never an amber pill or amber price-slot for unavailability states.
- **Emerald:** offer-in owns the solid `bg-emerald-100` pill + row tint. The restock
  chip is the **bordered 50-shade** (`bg-emerald-50 border-emerald-200`) with no row
  tint — visually distinct at a glance.

### Tailwind literals checklist (verify in built CSS post-deploy / safelist)

`text-rose-400 text-rose-300 text-rose-600 hover:text-emerald-600 text-amber-600
hover:text-amber-800 text-emerald-700 hover:text-emerald-900 text-emerald-600
bg-emerald-50 border-emerald-200 max-w-[28ch] max-w-[36ch] max-w-[24ch] min-w-0
col-span-2`

Mobile: truncation + `title` attributes carry the overflow; no layout fork (vendor
rows render identically on the mobile/desktop split).

New modal partial `app/templates/htmx/partials/sightings/unavailable_form.html`
(header comment; single-quoted Alpine attributes where Jinja values are embedded —
repo landmine; no double quotes inside double-quoted Alpine attrs; includes the
all-listings-of-this-MPN caveat copy).

## Out of scope (deliberate boundaries — say so in the PR)

- The requisitions-page per-sighting toggle (`PATCH …/sightings/{id}/unavailable` in
  `app/routers/requisitions/requirements.py`) stays row-level only; the sightings
  workspace is the canonical surface for vendor+part knowledge.
- No vendor-level "never contact for anything" semantics — that's what blacklist is for.
- Substitute-MPN matching in RFQ-modal exclusion (primary-key matching only, see above).
- No backfill of reasons for rows already flagged before this ships (legacy flags keep
  working via the no-record all-rows-flagged branch in status computation).
- **Condition-aware `qty_at_mark` snapshots (v2)** — `normalize_mpn_key` collapses
  condition/variant; NEW + REFURB share one record (accepted limitation ① in the
  policy doc; the modal caveat copy is the mitigation).

## Testing

- **Service** (`tests/test_vendor_unavailability.py`): upsert semantics (second mark
  updates, not duplicates); clear deletes + unflags; `apply_to_fresh_sightings`
  re-marks a recreated sighting (simulate delete+recreate, the resurrection scenario);
  `excluded_vendor_norms` matches on primary key; keys include both matched-MPN and
  primary-MPN; ActivityLog rows written on record + clear.
- **Temporal policy** (new/extended): window expiry flips stamp→advisory per reason
  class; `different_part` never expires; O1 equality-guard (identical distributor echo
  stays stamped; changed qty surfaces); O2 ratio boundary + NULL-no-signal both
  directions + snapshot-0; O3 releases via `email_attachment` but
  `email_auto_import`/`excess_list` stamp; source-class dispatch (a HUMAN_DIRECT row
  whose qty also clears the O2 jump RELEASES the record — O2 never shadows O3; a LIVE
  qty jump takes the O1 path, no record mutation); unknown/empty source_type stamps
  and never releases; per-key snapshot isolation (two keys, different qtys); re-mark keeps old
  snapshot when no qty visible and resets `released_at`; offer hook releases
  all-but-`different_part`; Batch 4 rows-win + expired-record-doesn't-pin-pill;
  `excluded_vendor_norms` active-only; knob validators reject 0/negative.
- **Silent-failure regressions**: zero-key `record_unavailability` raises (and route
  → 400, no ActivityLog); NULL-norm zombie clear (legacy sighting with NULL
  `vendor_name_normalized` gets unflagged via the shared helper); empty vendor norm
  raises in record/clear; candidate-key SET matching in apply (row matching only via
  primary key still stamped); provenance clear (record whose key no longer matches the
  requirement's keys is still deleted via `requirement_id`); mixed-variant legacy pin
  (record + mixed stamped/unstamped rows → NOT `unavailable`).
- **Status** (extend existing status/router tests): durable record alone (no row
  flags) → vendor status `unavailable`; offer-in still dominates a record.
- **Routes** (`tests/test_sightings_router.py`): mark with reason+note → 200, detail
  shows rose row + reason label; invalid reason → 400; zero-key mark → 400 JSON error;
  mark-available → row back to normal; unavailable-form renders all six reasons + the
  caveat copy; RFQ vendor modal excludes the marked vendor for that requirement and
  still shows it for an unrelated requirement; expired record → vendor back in RFQ
  modal; mark works for a suffixed vendor name ("X, Inc." — the normalization fix);
  send-inquiry with an excluded vendor drops it and reports the skip in the response;
  three-state row rendering (suppressed / advisory hint+verify / restock chip).
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
  outlives accounts); same for `requirement_id` (knowledge outlives requirements).
- **PR description must note** (MINOR-9) the legacy Batch-4 branch strictening
  (all-rows-flagged now only applies to vendors with NO record) — pinned by the
  mixed-variant test.
