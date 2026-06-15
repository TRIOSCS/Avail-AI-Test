# Bulk Cross-Requisition RFQ Composer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix cross-requisition RFQ tracking at the root (per-requisition Contact rows sharing one email) and upgrade the sightings vendor modal with coverage-ranked, affinity-suggested, searchable, and inline-creatable vendor selection.

**Architecture:** No schema change. Multiplicity moves to Contact rows: one email per vendor, one Contact per (requisition, vendor), shared graph ids, multi-token `[ref:]` subjects with reply attribution to all involved requisitions. Vendor panel becomes four sections over the existing Alpine selection state; all server logic in the sightings router + one shared coverage query; affinity via the existing `find_vendor_affinity` service.

**Tech Stack:** FastAPI thin routers, SQLAlchemy 2.0 plain-column aggregates (SQLite+PG safe), Jinja2 + HTMX partial swaps (explicit hx-target) + Alpine, pytest route-level tests.

**Spec (authoritative):** `docs/superpowers/specs/2026-06-12-bulk-rfq-composer-design.md` — read fully before each task. Also CLAUDE.md (Alpine landmines, JSON errors, response formats, htmx rules).

---

### Task 1: Cross-requisition tracking fix (the load-bearing core)

**Files (anchors architect-verified):**
- Modify: `app/routers/sightings.py` `sightings_send_inquiry` (:1445, collapse at :1476) — remove the `next(iter(requisition_ids))` collapse; group requirements per requisition; status auto-progress per involved requisition.
- Modify IN LOCKSTEP: `app/routers/sightings.py` `sightings_preview_inquiry` (:1367, same collapse at :1394) — preview subject must render ALL `[ref:]` tokens exactly as the send will.
- Modify: `app/email_service.py` `send_batch_rfq` (:79-262) — ADDITIVE param (e.g. `requisition_parts_map: dict[int, list] | None = None`) with a shim converting the legacy scalar `requisition_id` to a one-entry map; one email per vendor; subject carries one `[ref:{id}]` token per requisition; one Contact per (requisition, vendor) sharing graph_message_id/graph_conversation_id; per-Contact `parts_included` scoped to that requisition. The second caller `htmx_views.rfq_send` (`app/routers/htmx_views.py:2642-2650`, scalar req_id) stays byte-identical — do not edit it.
- Modify: `app/email_service.py` tag-propagation block (:243-260) — currently reads only the single `requisition_id`; iterate ALL involved requisitions' requirements for card ids.
- Modify (BLOCKER, spec Part 1 "Reply-matcher fan-out"): `app/email_service.py` inbox monitor — `conv_id_map` (:443-451) becomes `dict[str, list[Contact]]`; Tier-1 (:494-496) loops ALL contacts sharing the conversation id; ONE `VendorResponse` per message with `contact_id = contacts[0].id`; `_progress_contact_status` (call :572) iterates the full list. Document in code: `req_email_map` (:448/:454 setdefault) is accidentally correct post-fan-out (unique (req, email) pairs); Tier-3 `email_map` (:459) stays most-recent fallback by design.
- Modify: token regex consolidation — `AVAIL_TOKEN_RE` (`app/connectors/email_mining.py:61`) and `RFQ_SUBJECT_TAG_RE` (`app/shared_constants.py:124`) are duplicates; keep ONE shared pattern in shared_constants. Switch `.search().group(1)` extraction to `re.findall`: `app/jobs/email_jobs.py:915-917` (sent-folder ActivityLog → attribute to ALL token requisitions); `app/connectors/email_mining.py:520` is presence-only — just point it at the shared pattern.
- Test: spec "Testing → Tracking / Reply matcher / Preview lockstep / Sent-folder scan" lists verbatim (multi-requisition Contact fan-out, shared graph ids, multi-token subject, Tier-1 attribution to ALL contacts sharing a conversation id, preview multi-token subject, sent-folder scan attributing to all token requisitions, per-requisition status progression, single-requisition byte-identical regression incl. the rfq_send legacy call shape).

- [ ] Failing tests first → implement → green → commit `fix(rfq): per-requisition Contact tracking for cross-requisition sends`

### Task 2: Coverage-ranked suggested vendors

**Files:**
- Modify: `app/routers/sightings.py` `sightings_vendor_modal` (:1303-1363) — replace the flat engagement query with the spec's grouped coverage query over VendorSightingSummary (coverage desc, engagement desc, cap 20); join enrichment data via `VendorSightingSummary.vendor_card_id` (existing FK, indexed `ix_vss_vendor_card`) — NOT the legacy `lower(trim(vendor_name)) == normalized_name` join; add a route comment that VSS rows with NULL `vendor_card_id` are excluded by design (modal suggests known vendors); compute covered-MPN lists server-side; keep `excluded_vendor_norms` filtering exactly as today.
- Modify: `app/templates/htmx/partials/sightings/vendor_modal.html` — `N/M parts` per row + covered-MPN `title`; existing badges kept.
- Test: ranking order, exclusion, render assertions (spec "Coverage ranking").

- [ ] Failing tests first → implement → green → commit `feat(rfq): coverage-ranked vendor suggestions`

### Task 3: Affinity suggestions on demand

**Files:**
- Modify: `app/routers/sightings.py` — new `GET /v2/partials/sightings/vendor-affinity?requirement_ids=…` per spec §2.2 (merge/dedupe by vendor keep-highest-confidence, drop already-suggested + excluded, cap 10). THREADING: `find_vendor_affinity` (`app/services/vendor_affinity_service.py:271`) is SYNC with a blocking Anthropic L3 call inside — wrap each per-MPN call in `asyncio.to_thread(...)`, gathered under `asyncio.Semaphore(3)`; NEVER call it bare from the async route (blocks the uvicorn worker 3-12s for 6 parts).
- Create: small partial for the affinity rows (or extend vendor_modal.html section) — bordered indigo chip + confidence % + reasoning in `title`; "Suggest more vendors" button with explicit `hx-target` swapping a stable-id sub-container INSIDE the `x-data='rfqVendorModal(...)'` wrapper (never the wrapper — re-init wipes runtime selection state); the button sits inside that target so the response swaps it away — second click cannot duplicate rows.
- Test: spec "Affinity endpoint" list (incl. the no-duplicate-on-second-request pin); mock the L3 Claude path at the source module.

- [ ] Failing tests first → implement → green → commit `feat(rfq): affinity vendor suggestions`

### Task 4: Any-vendor autocomplete + inline vendor creation

**Files:**
- Modify: `app/templates/htmx/partials/sightings/vendor_modal.html` — give the vendor panel a stable container id (the template has none today); autocomplete input against existing `GET /api/autocomplete/names` (filter type=="vendor" client-side; selected vendor appends a checked row; excluded vendor → rose chip + disabled checkbox); "Add new vendor" inline mini-form (Enter-Offer modal "New Vendor" pattern). All swaps target stable-id sub-containers INSIDE the `x-data='rfqVendorModal(...)'` wrapper, never the wrapper — `_form()` reads `selectedVendors` keys, so runtime-added rows flow into `vendor_names` automatically.
- Create: extract `check_vendor_duplicate(name, db)` into an importable function under `app/services/` (per CLAUDE.md thin-routers rule) — the logic currently lives in the route at `app/routers/vendors_crud.py:83-123` with module-private fuzzy helpers (`_fuzzy_match_pg_trgm` :35 / `_fuzzy_match_python` :56); there is NO shared service today. BOTH the existing `/api/vendors/check-duplicate` route AND the new composer endpoint call the extracted function.
- Modify: `app/routers/sightings.py` — new `POST /v2/partials/sightings/composer-vendor` per spec §2.4 (calls the extracted service function, NOT HTTP; confident duplicate → existing row + notice; else minimal VendorCard + VendorContact when email given; fire `_background_enrich_vendor` (`app/utils/vendor_helpers.py:157`) after commit, identical to `materials.py:889` / `vendor_contacts.py:616`; 400 JSON on empty name).
- Test: spec "Autocomplete add" + "Inline create" lists; existing check-duplicate route behavior unchanged after extraction.

- [ ] Failing tests first → implement → green → commit `feat(rfq): any-vendor picker + inline vendor creation`

### Task 5: Docs + verification gate

- [ ] `docs/APP_MAP_INTERACTIONS.md` — update the RFQ flow section (per-requisition Contact fan-out, multi-token refs, the four vendor-panel sources); `docs/APP_MAP_DATABASE.md` only if Contact semantics text exists there.
- [ ] Full suite `TESTING=1 PYTHONPATH=. pytest tests/ -q --tb=line` green; `pre-commit run --all-files` (twice if docformatter mutates).
- [ ] Commit `docs(rfq): APP_MAP updates for bulk composer + per-requisition tracking`
