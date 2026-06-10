# Vendor+Part Unavailability Knowledge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Durable vendor+part unavailability records (reason + note + provenance) that survive re-searches, expire/release per the adopted "Two Windows, Real Proof" temporal policy, suppress RFQ suggestions while active, render the three-state row UI, log to activity, and support undo/re-arm.

**Architecture:** `VendorPartUnavailability` model keyed (normalized vendor, normalized MPN) + policy columns (`qty_at_mark`, `released_at`, `release_trigger`) + `requirement_id` provenance; all logic in `app/services/vendor_unavailability.py` (one shared sighting-matching helper, one `is_active` predicate as the single authority — `Sighting.is_unavailable` is a render cache); `apply_to_fresh_sightings` embeds the O1/O2/O3 suppression matrix at all six persistence paths; `compute_vendor_statuses` Batch 4 implements the reader-authority rule; routes extend mark-unavailable, add reason modal + mark-available + offer-hook release, and filter RFQ with active-only exclusion.

**Tech Stack:** SQLAlchemy 2.0 + Alembic (PostgreSQL prod / SQLite tests), FastAPI thin routers, Jinja2 + HTMX + Alpine, pytest route-level render tests.

**Specs (authoritative for ALL behavior — read both in full before each task):**
- `docs/superpowers/specs/2026-06-10-vendor-part-unavailability-design.md` (feature spec: data model, service surface, routes, three-state UI, silent-failure hardening)
- `docs/superpowers/specs/2026-06-10-unavailability-temporal-policy.md` (adopted temporal policy: windows, source classes, O1/O2/O3 + offer hook, reader-authority rule, knobs, accepted limitations)

Also read `CLAUDE.md` (migration rules, Alpine landmines, response formats).

---

### Task 1: Constants, model, migration — ✅ COMPLETED (commit `531974ef`)

Shipped: `UnavailabilityReason` StrEnum (6 reasons, `.label` property) in
`app/constants.py`; `VendorPartUnavailability` model (unique vendor_norm+mpn pair,
reason/note/provenance, created_by SET NULL); migration
`097_vendor_part_unavailability` (round-tripped PG + SQLite, single head); model +
enum tests in `tests/test_vendor_unavailability.py`.

> Task 2b retrofits this model with the policy + provenance columns (migration 098).

### Task 2: Service module + status computation (v1) — ✅ COMPLETED (commit `0b7675e5`)

Shipped: `app/services/vendor_unavailability.py` v1 (record/clear/intel/apply/excluded,
`_sighting_norm` NULL-fallback helper, batched queries, ActivityLog, no commits);
`sighting_status.py` Batch 4 ORs in the durable record + normalized-name legacy
anchoring; upsert/clear/keys/ActivityLog/suffixed-vendor + status tests.

> Task 2b retrofits this service with the temporal policy, the reader-authority Batch 4
> rewrite, and the silent-failure hardening (some v1 behaviors are deliberately
> superseded — e.g. the v1 unconditional OR in Batch 4 becomes the rows-win rule).

### Task 2b: Silent-failure hardening + temporal policy (service + model + migration 098)

**Files:**
- Modify: `app/models/vendor_part_unavailability.py` — four nullable columns per the spec's Data model table: `qty_at_mark` (Integer), `released_at` (UTCDateTime), `release_trigger` (String(32)), `requirement_id` (FK `requirements.id`, indexed, `ondelete="SET NULL"`)
- Create: migration **098** via autogenerate — ONLY these four columns, revision id ≤32 chars, downgrade drops them, `alembic heads` → single head; no backfill (NULL `qty_at_mark` ⇒ O2 never fires for legacy records — fail-closed)
- Modify: `app/config.py` — the three validated knobs (`unavailability_suppress_days` ge=1 default 30, `unavailability_listing_suppress_days` ge=1 default 180, `unavailability_qty_jump_factor` ge=1.0 default 2.0) with the retroactivity comment
- Modify: `app/services/vendor_unavailability.py` —
  - policy helpers: `LOT_REASONS`/`LIVE_SOURCES`/`HUMAN_DIRECT_SOURCES` `Final`s, `_source_class()` (listing-class default), `_window_days()` (`different_part` → None), `is_active(record, now)` (THE shared predicate), `_override(record, sighting)` (O1/O2)
  - CRITICAL-2: ONE shared sighting-matching helper with `_sighting_norm` NULL-fallback used by record/clear/apply (and exported for status) — eliminate every strict-equality-only column match
  - CRITICAL-1 + IMPORTANT-4: `record_unavailability` raises `ValueError` on zero derivable MPN keys and on empty vendor norm (no writes, no ActivityLog); `clear_unavailability` raises on empty vendor norm
  - per-key `qty_at_mark` snapshot on record/re-mark (keep-old-on-NULL); re-mark NULLs `released_at`/`release_trigger` and refreshes `requirement_id`
  - IMPORTANT-3: `clear_unavailability` delete predicate = vendor_norm AND (key IN current keys OR `requirement_id == requirement.id`)
  - IMPORTANT-5: `apply_to_fresh_sightings` matches each sighting on the candidate-key SET `{normalize_mpn_key(mpn_matched), primary_key}` (both non-empty) against full fetched records, then applies the O1/O2/O3 matrix (O3 sets `released_at`/`'vendor_email'` + ActivityLog; O1/O2 leave unstamped; non-active → skip; else stamp)
  - IMPORTANT-6: `excluded_vendor_norms` → active-only (Python `is_active` filter over full rows) + warning when a requirement contributes no key
  - MINOR-7: ActivityLog notes never interpolate None MPN (fallback: matched MPN or `"requirement #<id>"`)
  - new `release_on_offer(db, requirement, vendor_name, user)` — releases active records except `different_part`, `'offer_received'`, ActivityLog (route wiring in Task 4)
  - `unavailability_for_requirement` annotates results with computed policy state (`is_active`, age, release trigger) for the templates
- Modify: `app/services/sighting_status.py` — Batch 4 reader-authority rewrite: `unavailable` iff (active record AND no unstamped row) OR (no record AND all rows flagged); MINOR-8 warning on missing requirement row; precedence untouched
- Test: extend `tests/test_vendor_unavailability.py` + status tests — the spec's **Temporal policy** list (window expiry per class; `different_part` never expires; O1 equality-guard; O2 ratio boundary + NULL-no-signal both directions + snapshot-0; O3 via `email_attachment` but `email_auto_import`/`excess_list` stamp; unknown/empty source stamps, never releases; per-key snapshot isolation; re-mark keep-old-snapshot + `released_at` reset; offer hook releases all-but-`different_part`; Batch 4 rows-win + expired-record-doesn't-pin-pill; `excluded_vendor_norms` active-only; knob validators reject 0/negative) **PLUS the silent-failure regressions** (zero-key raise; NULL-norm zombie clear; empty-norm raise; candidate-key set; provenance clear; mixed-variant legacy pin → NOT unavailable)

- [ ] Failing tests first, then implement, then green
- [ ] `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` against a scratch DB; `alembic heads` → single head
- [ ] Commit `feat(unavailability): temporal policy + silent-failure hardening (service, model, migration 098)`

### Task 3: Re-application at every sighting-persistence path (resurrection fix)

**Files (spec section "Re-application at EVERY sighting-persistence path" is the contract — six call sites, each using its OWN session; `apply_to_fresh_sightings` now embeds the O1/O2/O3 policy matrix, so every path gets policy behavior for free):**
- Modify: `app/search_service.py` (after the fresh-`Sighting` construction loop following the connector-aware delete — search's separate write session, the CLAUDE.md trap)
- Modify: `app/services/ics_worker/sighting_writer.py` and `app/services/nc_worker/sighting_writer.py` (end of each save loop — async writers; without them ICS/NC results re-open the hole minutes after a search)
- Modify: `app/routers/sources.py` (email-attachment import — also the HUMAN_DIRECT/O3 release path), `app/routers/htmx_views.py` (add-to-requisition picker — deliberately stamped; user can Mark available to override), `app/jobs/inventory_jobs.py` (group created rows per requirement before calling)
- Test: `tests/test_vendor_unavailability.py` — resurrection scenarios: (a) search-path delete+recreate → fresh rows flagged while the record is active; (b) at least one ICS/NC **async writer** saves fresh rows → flagged; (c) expired record → fresh rows NOT stamped; (d) O3 path: `email_attachment` row with qty>0 releases the record instead of stamping

- [ ] Failing tests first, then implement all six call sites, then green
- [ ] Commit `feat(unavailability): policy-predicate re-stamping at all persistence paths`

### Task 4: Routes, 3-state UI, offer hook, RFQ exclusion

**Files:**
- Modify: `app/routers/sightings.py` —
  - extend `mark-unavailable` (reason required + validated, note optional; 400 on invalid reason; service `ValueError`s → 400 JSON error, no ActivityLog on that path; keep SSE param + detail re-render; re-POST = re-arm)
  - add `GET …/unavailable-form` + `POST …/mark-available` (same 400-on-ValueError mapping). **NO new verify endpoint** — the verify affordance maps to re-arm (mark-unavailable modal) and clear (mark-available)
  - **offer hook:** call `release_on_offer(...)` from the offer-creation route after the offer persists (same transaction)
  - detail view passes annotated `unavailable_intel`; `sightings_vendor_modal` excludes `excluded_vendor_norms` vendors (**active-only**; excluded if unavailable for ANY selected part — documented conservative semantics); `sightings_send_inquiry` + `sightings_preview_inquiry` re-validate submitted vendor_names against active-only `excluded_vendor_norms`, drop excluded vendors, and visibly report the skip (existing skipped-vendor reporting style; never silent)
- Create: `app/templates/htmx/partials/sightings/unavailable_form.html` (reason radios from the enum, note textarea, submit/cancel; **the all-listings-of-this-MPN caveat copy**; follow the existing offer-form modal partial's structure; single-quoted Alpine attrs around Jinja — landmine list in CLAUDE.md)
- Modify: `app/templates/htmx/partials/sightings/_vendor_row.html` — the spec's **three-state UI** section is the contract: state 1 suppressed (reason/note/age + Mark available only), state 2 expired advisory (gray italic hint + amber verify link + full trio, Mark Unavail = re-arm), state 3 possible restock (bordered emerald chip + qty delta + emerald verify link + full trio; RFQ stays gated server-side while active). Mark-Unavail button → open-modal dispatch. Do NOT disturb the shipped tint/badge/dim treatment or its tests. Verify the Tailwind literals checklist lands in built CSS.
- Test: `tests/test_sightings_router.py` — the spec's Routes test list (mark with reason renders label; invalid reason 400; zero-key 400 JSON; mark-available restores; form renders six reasons + caveat copy; RFQ modal excludes for that requirement, not an unrelated one; expired record → back in RFQ modal; suffixed vendor name; send-inquiry visible skip; three-state row rendering)

- [ ] Failing tests first, then implement, then green (run the whole file — the PR #260 treatment tests must stay green)
- [ ] Commit `feat(unavailability): reason modal, 3-state row UI, offer-hook release, active-only RFQ exclusion`

### Task 5: Docs + verification gate

- [ ] Update `docs/APP_MAP_DATABASE.md` (table + policy/provenance columns) and `docs/APP_MAP_INTERACTIONS.md` (mark/re-arm/clear/expiry/release flow, O1-O3 matrix, reader-authority rule, RFQ exclusion) — this feature IS at APP_MAP altitude
- [ ] `TESTING=1 PYTHONPATH=. pytest tests/ -q` → green
- [ ] `pre-commit run --all-files` (run twice if docformatter mutates)
- [ ] PR description: note the legacy Batch-4 strictening (MINOR-9), the conservative ANY-part RFQ exclusion, and the deliberate out-of-scope boundaries
- [ ] Commit `docs: APP_MAP entries for vendor-part unavailability`
