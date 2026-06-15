# Bulk Cross-Requisition RFQ Composer (Track B) — Design

**Date:** 2026-06-12
**Status:** Approved feature (user selected Track B from the next-build options; this spec
resolves the design). Track A (Offers tab) and durable unavailability (PR #270) are shipped.
**Origin:** deferred as "separate spec" in `2026-06-05-sightings-offers-tab-design.md` §11.

## Problem

The sightings vendor modal already composes/previews/sends a multi-part RFQ
(`vendor_modal.html`, `sightings_vendor_modal` / `preview-inquiry` / `send-inquiry`),
invoked from the table's multi-select. But:

1. **Cross-requisition tracking is broken (load-bearing bug):** `send-inquiry` collapses
   the selected requirements' requisitions to ONE via
   `requisition_id = next(iter(requisition_ids))` (undefined set order) and passes it to
   `send_batch_rfq`, which writes every `Contact` row, every `[ref:{requisition_id}]`
   subject token, and reply-matching state against that single requisition. Outreach
   spanning requisitions A+B+C records history only on one of them — silently.
2. **Vendor selection is dumb:** a flat engagement-ordered top-20 of vendors that have
   any sighting on the selected parts. No part-coverage ranking, no way to reach a vendor
   with no sighting (affinity), no picking an arbitrary DB vendor, no adding a new vendor.

## Decision summary

Fix tracking at the root (per-requisition `Contact` rows sharing one email), and upgrade
the modal's vendor panel: coverage-ranked suggestions, affinity suggestions on demand,
any-vendor autocomplete, inline vendor creation. Reuse the existing compose→preview→send
skeleton, `send_batch_rfq`, and the unavailability exclusion. **No new tables; no
migration** (Contact keeps its singular `requisition_id` — multiplicity moves to rows).

## Part 1 — Cross-requisition tracking fix

**Semantics: one email per vendor covering ALL selected parts** (that is the point of a
bulk composer), with full per-requisition tracking:

- `sightings_send_inquiry` stops collapsing requisitions. It groups the selected
  requirements per requisition and passes the full mapping down.
- **Preview in lockstep:** `sightings_preview_inquiry` (`app/routers/sightings.py:1394`)
  has the SAME `next(iter(requisition_ids))` collapse as send (`:1476`) — fix both in
  the same change. The preview subject must show ALL `[ref:]` tokens exactly as the
  send will produce them (no preview/send divergence).
- `send_batch_rfq` (or a thin evolution of it — keep ONE canonical send path) sends one
  Graph email per vendor, then writes **one `Contact` row per (requisition, vendor)
  pair**, all sharing that email's `graph_message_id` / `graph_conversation_id`. Each
  Contact's `parts_included` holds only that requisition's parts.
- **Signature compatibility (verified second caller):** `htmx_views.rfq_send`
  (`app/routers/htmx_views.py:2642-2650`) calls `send_batch_rfq` with a single scalar
  `requisition_id=req_id` and must stay byte-identical. The new shape is an **additive
  parameter** (e.g. `requisition_parts_map: dict[int, list] | None = None`) with a shim
  that converts the legacy single-id form into a one-entry map internally.
- **Subject ref tokens:** the subject carries one `[ref:{id}]` token per involved
  requisition (e.g. `RFQ — 6 parts [ref:12] [ref:34]`).
- **Reply-matcher fan-out (architect-verified, BLOCKER):** the inbox monitor's
  `conv_id_map` (`app/email_service.py:443-451`) is `dict[conv_id] = contact` —
  last-writer-wins, so per-(requisition, vendor) Contact rows sharing one
  `graph_conversation_id` would silently drop all but one. It MUST become
  `dict[str, list[Contact]]`:
  - Tier-1 conversation-id matching (`:494-496`) loops ALL contacts sharing the
    conversation id.
  - The `VendorResponse` row is still created **once** per message, with
    `contact_id = contacts[0].id` (the row is per-message, not per-requisition).
  - `_progress_contact_status` (call site `:572`) iterates the **full list** so every
    involved Contact progresses.
  - `req_email_map` (`:448`, populated via `setdefault` at `:454`) is *accidentally
    correct* after this feature: (requisition_id, email) pairs become unique under the
    per-requisition fan-out, so `setdefault` never collides. Document this in code; no
    structural change needed.
  - Tier-3 `email_map` (`:459`) stays most-recent-contact fallback **by design**
    (user-scoped heuristic for untokenized replies) — document, don't change.
- **Token regex consolidation:** the pattern is duplicated — `AVAIL_TOKEN_RE`
  (`app/connectors/email_mining.py:61`) vs `RFQ_SUBJECT_TAG_RE`
  (`app/shared_constants.py:124`), byte-identical. Consolidate to the ONE shared
  pattern in `shared_constants.py`. Consumers that extract the requisition id via
  `.search().group(1)` switch to `re.findall` so multi-token subjects attribute to ALL
  requisitions: `app/jobs/email_jobs.py:915-917` (sent-folder ActivityLog scan — one
  attribution per token requisition). `app/connectors/email_mining.py:520` is
  presence-only detection (no `.group(1)`) — multi-token safe as-is; just switch it to
  the shared pattern. Tier-2 inbox matching (`app/email_service.py:500-508`) iterates
  all found tokens when resolving `(req_id, email)` pairs.
- **Tag propagation:** the block in `send_batch_rfq` (`app/email_service.py:243-260`)
  reads only the single `requisition_id` — it must iterate ALL involved requisitions'
  requirements when collecting `material_card_id`s.
- Activity logging already iterates per requirement (`log_rfq_activity(rfq_id=
  r.requisition_id, requirement_id=r.id)`) — keep, now consistent with Contacts.
- Status auto-progress (OPEN→SOURCING) applies per involved requisition, not just one.
- `X-RFQ-*` headers unchanged in meaning (per-vendor counts).

## Part 2 — Vendor panel upgrade (`vendor_modal.html` + `sightings_vendor_modal`)

The modal's vendor list becomes four sections in one selectable checklist:

1. **Suggested (coverage-ranked).** One grouped query over `VendorSightingSummary`
   filtered to the selected requirement ids: per vendor — covered-part count, avg
   summary score, engagement score. Order: coverage desc, then engagement desc; keep the
   20-row cap. **Join (architect-verified):** enrichment data joins via
   `VendorSightingSummary.vendor_card_id` (existing FK, indexed `ix_vss_vendor_card`) —
   NOT the legacy `lower(trim(vendor_name)) == normalized_name` join. Add a route
   comment that VSS rows with NULL `vendor_card_id` are excluded by design (the modal
   suggests known vendors). Each row shows `N/M parts` (M = selected part count) plus
   the existing response-rate/engagement badges; `title` lists the covered MPNs
   (computed in the route, template stays dumb). Vendors in `excluded_vendor_norms`
   stay excluded from this list (as today).
2. **Affinity (on demand).** A "Suggest more vendors" button (`hx-get`, htmx partial
   swap into the section — explicit `hx-target`) calls a new
   `GET /v2/partials/sightings/vendor-affinity?requirement_ids=…` which runs
   `find_vendor_affinity(mpn, db)` for each selected requirement's primary MPN,
   merges/dedupes by vendor (keep highest confidence), drops vendors already in the
   suggested list or excluded by unavailability, and returns rows rendered with a
   bordered indigo "affinity" chip + confidence % + the service's reasoning string in
   `title`. L3 (the Claude call) stays enabled inside the service as designed (the
   button gate makes the latency opt-in). Cap: 10 rows (the service's own cap).
   **Threading (architect-verified, required):** `find_vendor_affinity`
   (`app/services/vendor_affinity_service.py:271`) is SYNC with a blocking Anthropic
   call inside (L3, `anthropic.Anthropic(...).messages.create` at `:207-213`). The
   async endpoint MUST wrap each per-MPN call in `asyncio.to_thread(...)`, gathered
   under an `asyncio.Semaphore(3)` — never call it bare from the async route (it would
   block the uvicorn worker 3-12s for 6 parts).
   **Idempotent UI:** a second click must not duplicate rows — the swap replaces the
   button with the response (button lives inside the `hx-target` region), so it
   disappears once results render; a test pins no-duplicate-on-double-request.
3. **Find any vendor.** An autocomplete input reusing `GET /api/autocomplete/names`
   (vendors only — filter `type == "vendor"` client-side from the existing response;
   do NOT fork the endpoint). Selecting a result appends a checked row to the list. If
   the picked vendor is unavailability-excluded for the selected parts, the row renders
   with the rose "marked unavailable" chip and a **disabled** checkbox (the send-time
   re-validation remains the backstop).
4. **Add vendor on the fly.** An inline mini-form (name required; website + contact
   email optional) behind an "Add new vendor" toggle — the Enter-Offer modal's "New
   Vendor" pattern. POST to a new `POST /v2/partials/sightings/composer-vendor`
   endpoint that: runs the duplicate check; on a confident duplicate, returns the
   existing vendor row instead (with a "matched existing vendor" notice); otherwise
   creates the minimal `VendorCard` (the crm/offers.py:331 pattern: normalized_name,
   display_name, optional domain) plus a `VendorContact` when an email was given, and
   returns the new checked row.
   **Duplicate-check extraction (architect-verified — no shared service exists today):**
   the duplicate logic currently lives inside the route function
   (`app/routers/vendors_crud.py:83-123`, with module-private fuzzy helpers
   `_fuzzy_match_pg_trgm`/`_fuzzy_match_python` at `:35`/`:56`). Extract an importable
   `check_vendor_duplicate(name, db)` into a module under `app/services/` (the
   CLAUDE.md thin-routers rule's preferred home) and have BOTH the existing
   `/api/vendors/check-duplicate` route AND the new composer endpoint call it — never
   loopback HTTP, never copy-paste.
   **Post-create enrichment:** new-card creation fires `_background_enrich_vendor`
   (`app/utils/vendor_helpers.py:157`) after commit, identical to the existing
   patterns (`app/routers/materials.py:889`, `app/routers/vendor_contacts.py:616`).
   Vendors created here without an email show the existing "no contact email"
   treatment and are reported via `X-RFQ-Skipped` at send (current behavior,
   unchanged).

Selection state stays in the modal's existing Alpine component; all new rows join the
same `vendor_names` form field. Tailwind: full literal classes only; follow the modal's
existing chip/badge vocabulary (indigo = informational chip family, rose = unavailable).

**HTMX swap targets (architect-verified constraint):** every HTMX swap for the new
sections targets a stable-id sub-container INSIDE the
`x-data='rfqVendorModal(...)'` wrapper — NEVER the wrapper itself. Re-initializing the
Alpine component (`rfqVendorModal` factory, `app/static/htmx_app.js:1734`) would wipe
runtime-added vendor selection state. `_form()` reads `Object.keys(this.selectedVendors)`,
so rows added at runtime (affinity / autocomplete / inline-create) flow into
`vendor_names` automatically — no extra wiring. The vendor panel gets a stable container
id (the template currently has none); the affinity section, autocomplete result list,
and inline-create form each swap into their own stable-id child.

## Out of scope (deliberate)

- No change to email composition itself (plain-text body → `_build_html_body`).
- No per-vendor part subsetting in the composer (every selected vendor is asked about
  all selected parts; the per-requisition Contact split is a tracking concern, not a
  content concern).
- No affinity persistence/caching beyond the service's own behavior.
- No vendor-modal redesign outside the vendor panel (steps/preview/send UX unchanged).
- The requisitions-page RFQ flows (if any exist outside this modal) are untouched.

## Testing

- **Tracking (the core):** send spanning 2 requisitions × 2 vendors → exactly 4
  `Contact` rows, each with its own requisition_id and only that requisition's
  `parts_included`, sharing graph ids per vendor; subject contains both ref tokens;
  reply-matching attributes a simulated reply to both requisitions' contacts; both
  requisitions auto-progress OPEN→SOURCING; ActivityLog rows per requirement.
  Single-requisition sends behave byte-identically to today (regression), including
  the `htmx_views.rfq_send` legacy scalar-`requisition_id` call shape.
- **Reply matcher:** Tier-1 attributes a reply to ALL Contacts sharing a conversation
  id (the `dict[str, list[Contact]]` fan-out); exactly one `VendorResponse` per
  message; `_progress_contact_status` advanced every contact in the list.
- **Preview lockstep:** preview of a 2-requisition selection renders BOTH `[ref:]`
  tokens, identical to the send subject.
- **Sent-folder scan:** a sent message with two tokens attributes activity records to
  both token requisitions (`re.findall` path in `app/jobs/email_jobs.py`).
- **Coverage ranking:** vendor with 3/4 parts ranks above 1/4 with higher engagement;
  excluded vendor absent; `N/M parts` rendered.
- **Affinity endpoint:** returns merged/deduped rows; drops already-suggested and
  excluded vendors; renders chip + confidence; L3 path mocked at the source module;
  a second request does not duplicate rows already in the panel (button swapped away
  with the response).
- **Autocomplete add:** excluded vendor renders disabled + rose chip.
- **Inline create:** duplicate → existing card returned, no new row in DB; new vendor →
  card + contact created, row checked; empty name → 400 JSON error format.
- **Headers/regression:** X-RFQ-Sent/Total/Skipped/Unavailable semantics unchanged.

## Risks

- Reply-matching parser: RESOLVED by architect review — the required changes are now
  explicit requirements in Part 1 (conv_id_map list semantics, Tier-1 fan-out, token
  regex consolidation + `re.findall`). No remaining unknowns; build to the spec.
- `send_batch_rfq` is shared — both call sites are traced (`sightings.py:1514`,
  `htmx_views.py:2642-2650`); the additive `requisition_parts_map` parameter + legacy
  shim in Part 1 keeps the scalar-`requisition_id` caller byte-identical.
- SQLite tests vs PG: the coverage GROUP BY uses plain columns/aggregates only.

## Architect-review notes (verified against code, recorded for the builder)

- `compute_vendor_statuses` (`app/services/sighting_status.py:44`) consumes `Contact`
  rows requisition-scoped — the per-(requisition, vendor) fan-out is safe for it; no
  change needed (verified).
- `htmx_views.rfq_send` independently falls back to a DB-only token mode on send
  failure (`app/routers/htmx_views.py:2660`) — a pre-existing divergence from the
  sightings path; deliberately untouched by this feature.
