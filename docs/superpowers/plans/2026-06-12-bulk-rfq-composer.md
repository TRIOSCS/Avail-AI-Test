# Bulk Cross-Requisition RFQ Composer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix cross-requisition RFQ tracking at the root (per-requisition Contact rows sharing one email) and upgrade the sightings vendor modal with coverage-ranked, affinity-suggested, searchable, and inline-creatable vendor selection.

**Architecture:** No schema change. Multiplicity moves to Contact rows: one email per vendor, one Contact per (requisition, vendor), shared graph ids, multi-token `[ref:]` subjects with reply attribution to all involved requisitions. Vendor panel becomes four sections over the existing Alpine selection state; all server logic in the sightings router + one shared coverage query; affinity via the existing `find_vendor_affinity` service.

**Tech Stack:** FastAPI thin routers, SQLAlchemy 2.0 plain-column aggregates (SQLite+PG safe), Jinja2 + HTMX partial swaps (explicit hx-target) + Alpine, pytest route-level tests.

**Spec (authoritative):** `docs/superpowers/specs/2026-06-12-bulk-rfq-composer-design.md` — read fully before each task. Also CLAUDE.md (Alpine landmines, JSON errors, response formats, htmx rules).

---

### Task 1: Cross-requisition tracking fix (the load-bearing core)

**Files:**
- Read FIRST: the inbox-monitor / reply-matching parser (grep email_service.py + jobs for `ref:` parsing) — the spec mandates multi-token attribution; extend the parser root-cause if it only handles one token. Also `grep -rn "send_batch_rfq" app/` — every call site must stay behavior-identical for single-requisition use.
- Modify: `app/routers/sightings.py` `sightings_send_inquiry` (~:1444-1590) — remove the `next(iter(requisition_ids))` collapse; group requirements per requisition; status auto-progress per involved requisition.
- Modify: `app/email_service.py` `send_batch_rfq` (:79-262) — accept the requisition→parts mapping (additive/compatible signature per spec Risk note); one email per vendor; subject carries one `[ref:{id}]` token per requisition; one Contact per (requisition, vendor) sharing graph_message_id/graph_conversation_id; per-Contact `parts_included` scoped to that requisition.
- Modify (if parser needs it): the reply-matching code — attribute replies to ALL Contacts matching any token/conversation id.
- Test: spec "Testing → Tracking" list verbatim (multi-requisition Contact fan-out, shared graph ids, multi-token subject, reply attribution to all, per-requisition status progression, single-requisition byte-identical regression).

- [ ] Failing tests first → implement → green → commit `fix(rfq): per-requisition Contact tracking for cross-requisition sends`

### Task 2: Coverage-ranked suggested vendors

**Files:**
- Modify: `app/routers/sightings.py` `sightings_vendor_modal` (~:1302-1363) — replace the flat engagement query with the spec's grouped coverage query over VendorSightingSummary (coverage desc, engagement desc, cap 20); compute covered-MPN lists server-side; keep `excluded_vendor_norms` filtering exactly as today.
- Modify: `app/templates/htmx/partials/sightings/vendor_modal.html` — `N/M parts` per row + covered-MPN `title`; existing badges kept.
- Test: ranking order, exclusion, render assertions (spec "Coverage ranking").

- [ ] Failing tests first → implement → green → commit `feat(rfq): coverage-ranked vendor suggestions`

### Task 3: Affinity suggestions on demand

**Files:**
- Modify: `app/routers/sightings.py` — new `GET /v2/partials/sightings/vendor-affinity?requirement_ids=…` per spec §2.2 (merge/dedupe by vendor keep-highest-confidence, drop already-suggested + excluded, cap 10).
- Create: small partial for the affinity rows (or extend vendor_modal.html section) — bordered indigo chip + confidence % + reasoning in `title`; "Suggest more vendors" button with explicit `hx-target` swapping the section.
- Test: spec "Affinity endpoint" list; mock the L3 Claude path at the source module.

- [ ] Failing tests first → implement → green → commit `feat(rfq): affinity vendor suggestions`

### Task 4: Any-vendor autocomplete + inline vendor creation

**Files:**
- Modify: `app/templates/htmx/partials/sightings/vendor_modal.html` — autocomplete input against existing `GET /api/autocomplete/names` (filter type=="vendor" client-side; selected vendor appends a checked row; excluded vendor → rose chip + disabled checkbox); "Add new vendor" inline mini-form (Enter-Offer modal "New Vendor" pattern).
- Modify: `app/routers/sightings.py` — new `POST /v2/partials/sightings/composer-vendor` per spec §2.4 (shared duplicate-check service path, NOT HTTP; confident duplicate → existing row + notice; else minimal VendorCard + VendorContact when email given; 400 JSON on empty name).
- Test: spec "Autocomplete add" + "Inline create" lists.

- [ ] Failing tests first → implement → green → commit `feat(rfq): any-vendor picker + inline vendor creation`

### Task 5: Docs + verification gate

- [ ] `docs/APP_MAP_INTERACTIONS.md` — update the RFQ flow section (per-requisition Contact fan-out, multi-token refs, the four vendor-panel sources); `docs/APP_MAP_DATABASE.md` only if Contact semantics text exists there.
- [ ] Full suite `TESTING=1 PYTHONPATH=. pytest tests/ -q --tb=line` green; `pre-commit run --all-files` (twice if docformatter mutates).
- [ ] Commit `docs(rfq): APP_MAP updates for bulk composer + per-requisition tracking`
