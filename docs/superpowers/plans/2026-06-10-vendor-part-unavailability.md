# Vendor+Part Unavailability Knowledge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Durable vendor+part unavailability records (reason + note + provenance) that survive re-searches, suppress RFQ suggestions, render on the sightings row, log to activity, and support undo.

**Architecture:** New `VendorPartUnavailability` model keyed (normalized vendor, normalized MPN); all logic in a new `app/services/vendor_unavailability.py`; `compute_vendor_statuses` ORs the durable record into the `unavailable` branch; `search_service` re-stamps fresh sightings inside its own write session; routes extend the existing mark-unavailable endpoint, add a reason modal + mark-available endpoint, and filter the RFQ vendor modal.

**Tech Stack:** SQLAlchemy 2.0 + Alembic (PostgreSQL prod / SQLite tests), FastAPI thin routers, Jinja2 + HTMX + Alpine, pytest route-level render tests.

**Spec (authoritative for ALL behavior):** `docs/superpowers/specs/2026-06-10-vendor-part-unavailability-design.md` — read it in full before each task. Also read `CLAUDE.md` (migration rules, Alpine landmines, response formats).

---

### Task 1: Constants, model, migration

**Files:**
- Modify: `app/constants.py` (new `UnavailabilityReason` StrEnum with `.label` property — six members per spec)
- Create: `app/models/vendor_part_unavailability.py` (columns/constraints exactly per spec table; follow header-comment + `UTCDateTime` conventions of sibling models, e.g. `app/models/offers.py`; ensure the model is imported wherever siblings are registered so metadata + autogenerate see it)
- Create: migration via `alembic revision --autogenerate -m "vendor_part_unavailability"` — REVIEW it (only this table; strip unrelated autogen noise), revision id ≤32 chars
- Test: `tests/test_vendor_unavailability.py` (new) — model creation, unique-constraint violation on duplicate (vendor, mpn) insert

- [ ] Write failing tests (model import + create + duplicate-key IntegrityError)
- [ ] Implement enum + model + migration
- [ ] `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` against a scratch DB; `alembic heads` → single head
- [ ] Tests pass; commit `feat(unavailability): VendorPartUnavailability model + migration`

### Task 2: Service module + status computation

**Files:**
- Create: `app/services/vendor_unavailability.py` — the six functions with exact signatures and semantics from the spec's Service layer section (keys = matched-MPN keys ∪ primary key via `normalize_mpn_key`; upsert-on-conflict-update; clear deletes + unflags; sighting flagging matches on `Sighting.vendor_name_normalized` — architect finding 1, NOT `lower(trim(...))`; batched queries, no N+1; ActivityLog entries follow the direct-construction pattern already in `app/routers/sightings.py`; no commits inside the service)
- Modify: `app/services/sighting_status.py` — Batch 4 ORs in the durable record per spec AND re-anchors the legacy row-flag grouping on `Sighting.vendor_name_normalized` (architect finding 2); precedence order untouched
- Modify: `app/models/vendor_part_unavailability.py` — `created_at` gains a Python-side default alongside `server_default` (dual-default sibling pattern, architect finding 8; no migration change needed)
- Test: extend `tests/test_vendor_unavailability.py` (upsert update-not-duplicate; clear; keys composition; ActivityLog written; suffixed vendor name "X, Inc." flags correctly) + extend status tests (record alone → `unavailable`; record + Offer → `offer-in`; row-flag branch matches despite case drift between summary and sighting names)

- [ ] Failing tests first, then implement, then green
- [ ] Commit `feat(unavailability): service layer + durable status computation`

### Task 3: Re-application at every sighting-persistence path (resurrection fix)

**Files (spec section "Re-application at EVERY sighting-persistence path" is the contract — six call sites, each using its OWN session):**
- Modify: `app/search_service.py` (after the fresh-`Sighting` construction loop following the connector-aware delete — search's separate write session, the CLAUDE.md trap)
- Modify: `app/services/ics_worker/sighting_writer.py` and `app/services/nc_worker/sighting_writer.py` (end of each save loop — these async writers are the blocker finding; without them ICS/NC results re-open the hole minutes after a search)
- Modify: `app/routers/sources.py` (email-attachment import), `app/routers/htmx_views.py` (add-to-requisition picker — deliberately stamped; user can Mark available to override), `app/jobs/inventory_jobs.py` (group created rows per requirement before calling)
- Test: `tests/test_vendor_unavailability.py` — resurrection scenarios: (a) search-path delete+recreate → fresh rows flagged; (b) at least one ICS/NC writer saves fresh rows → flagged

- [ ] Failing tests first, then implement all six call sites, then green
- [ ] Commit `feat(unavailability): re-stamp fresh sightings from durable records at all persistence paths`

### Task 4: Routes, modal UI, row display, RFQ exclusion

**Files:**
- Modify: `app/routers/sightings.py` — extend `mark-unavailable` (reason required + validated, note optional, 400 on invalid; delegate to service — which replaces the route's old `lower(trim(...))` sighting filter with the normalized-column match; keep SSE param + detail re-render); add `GET …/unavailable-form` + `POST …/mark-available`; detail view passes `unavailable_intel`; `sightings_vendor_modal` excludes `excluded_vendor_norms` vendors (excluded if unavailable for ANY selected part — documented conservative semantics); `sightings_send_inquiry` + `sightings_preview_inquiry` re-validate submitted vendor_names against `excluded_vendor_norms`, drop excluded vendors, and visibly report the skip (existing skipped-vendor reporting style; never silent)
- Create: `app/templates/htmx/partials/sightings/unavailable_form.html` (reason radios from the enum, note textarea, submit/cancel; follow the existing offer-form modal partial's structure; single-quoted Alpine attrs around Jinja — landmine list in CLAUDE.md)
- Modify: `app/templates/htmx/partials/sightings/_vendor_row.html` — Mark-Unavail button → open-modal dispatch; unavailable rows get `Mark available` action + rose reason label in metrics line (truncated note in `title`); expanded panel "What we learned:" entry. Do NOT disturb the shipped tint/badge/dim treatment or its tests.
- Test: `tests/test_sightings_router.py` — per the spec's Routes test list (mark with reason renders label; invalid reason 400; mark-available restores; form renders six reasons; RFQ modal excludes for that requirement, not for an unrelated one)

- [ ] Failing tests first, then implement, then green (run the whole file — the PR #260 treatment tests must stay green)
- [ ] Commit `feat(unavailability): reason modal, undo, row intel, RFQ exclusion`

### Task 5: Docs + verification gate

- [ ] Update `docs/APP_MAP_DATABASE.md` (new table) and `docs/APP_MAP_INTERACTIONS.md` (mark/clear/re-stamp/exclusion flow) — this feature IS at APP_MAP altitude
- [ ] `TESTING=1 PYTHONPATH=. pytest tests/ -q` → green
- [ ] `pre-commit run --all-files` (run twice if docformatter mutates)
- [ ] Commit `docs: APP_MAP entries for vendor-part unavailability`
