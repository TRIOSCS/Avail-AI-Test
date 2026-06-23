# Resell / Excess Brokerage ("Trading") Module — v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)
> or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.
> **Source of truth:** `docs/superpowers/specs/2026-06-23-resell-module-design.md` + the build prompt
> `…-resell-module-build-prompt.md`. Read both before starting. This plan is DRY against them — it does
> not restate every contract; it sequences the work and pins the tests.

**Goal:** Reshape the orphaned `excess` module into a live "Trading" workspace where the resell team
posts customer excess, collects per-line/take-all broker offers, sees best-unit-price, and builds a
clean bid back to the stock holder — with posted lines live-mirrored into `Sighting`.

**Architecture:** Reshape-in-place. Keep `ExcessList`/`ExcessLineItem`; retire `Bid`/`BidSolicitation`;
add `ExcessOffer`/`ExcessOfferLine`. Fat services (`excess_service.py` + a small `excess_mirror` and
`excess_offer_aggregation` helper) / thin router. Revive the existing `excess/` templates into a
two-panel `splitPanel()` workspace. Reuse: Requisition intake parsers, the AI parser services, the
Sighting ingest/aggregation, the Quote PDF path, the shared UI macros.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + Alembic + HTMX 2 + Alpine 3 + Jinja2 +
Tailwind + WeasyPrint + Claude (existing AI services). Tests: pytest (xdist), Playwright e2e.

## Global Constraints (verbatim from spec/CLAUDE.md — apply to every task)

- No price-based **matching** (part number only); `unit_price` is **nullable**; price is collected +
  rolled up to best-per-unit.
- Offer `scope` ∈ {`per_line`, `take_all`}; take-all carries optional `take_all_total_price`.
- Unmatched offer lines NEVER dropped — `excess_line_item_id` nullable + `mpn_raw` → review queue.
- Roles→capabilities (`can_post`, `can_offer`); **block self-offer** (`submitted_by ≠ owner_id`).
- Customer **hidden** from offerers: pure-whitelist serializer (MPN/qty/condition only).
- Mirrored-sighting `requirement_id` = a **virtual "unallocated excess" requirement per list**.
- **Lock-on-post**; edits to a posted list create a new version. Late offers accepted + flagged `late`.
- Design: inherit tokens; **reuse existing `status_badge` keys** (no new colors); shared macros only;
  `.page-fluid`; `splitPanel`; morph swaps; lazy tabs need explicit `hx-target`; `tojson` single-quoted.
- `db.get(Model, id)` (not `.query().get()`); `StrEnum` constants (not raw strings); Loguru (not print).
- Migration: autogenerate → review → up/down/up; revision id ≤32 chars; coordinate number via
  `MIGRATION_NUMBERS_IN_FLIGHT.txt`; single `alembic heads`; include rollback.
- Tests with every change; full suite green from **inside the worktree**; **live-verify on PG** (SQLite
  masks PG-invalid SQL). Run `pre-commit run --all-files` before the PR. Update APP_MAP docs in-PR.

**Pre-flight (every task):** verify the current file/line/symbol names before editing — the spec's line
numbers are from 2026-06-23 and may have drifted. Confirm `ExcessListStatus` and friends in
`app/constants.py`, the `Sighting` columns in `app/models/sourcing.py`, the Quote PDF function in
`app/services/quote_builder_service.py`, and the shared macros in
`app/templates/htmx/partials/shared/_macros.html`.

---

## Existing-code inventory addendum (reshape is surgical, not greenfield)

The excess subsystem is **functional and tested** (255 tests / 12 files). Reshape must rewrite/retire it.

**Constants (`app/constants.py`):** exist — `ExcessListStatus` (DRAFT, ACTIVE, BIDDING, CLOSED, EXPIRED),
`ExcessLineItemStatus` (AVAILABLE, BIDDING, AWARDED, WITHDRAWN), `BidStatus`, `BidSolicitationStatus`.
→ extend list/item statuses per Task 1; **drop** `BidStatus`/`BidSolicitationStatus`; **add**
`ExcessOfferStatus`, `ExcessOfferScope`, `OfferLineMatchStatus`.

**Schemas (`app/schemas/excess.py`):** KEEP list/line/import; **DROP** `Bid*`, `BidSolicitation*`,
`ParseBidResponse*`, `SendBidSolicitation*`, `PolishEmail*`; **ADD** `ExcessOffer*`; **RESHAPE**
`ExcessStatsResponse` (drop bid counts → offer counts).

**Service (`excess_service.py`):** KEEP list/line CRUD + import/preview/confirm + `backfill_normalized_part_numbers`;
RESHAPE `match_excess_demand`/`get_excess_stats`; **DROP** `create_bid`/`list_bids`/`accept_bid`,
`send_bid_solicitation`+`_build_*solicitation_html`+`_find_sent_message`, `parse_bid_response`,
`list_solicitations`, `_call_claude_bid_parse`, `parse_bid_from_email`, `create_proactive_matches_for_excess`.

**Cross-module callers to remove (BREAKS otherwise):**
- `app/email_service.py` → `_handle_excess_bid_reply()` (calls `parse_bid_response`) — delete.
- `app/jobs/email_jobs.py` → `_scan_excess_bid_responses()` (calls `parse_bid_from_email`) — delete + unschedule.

**Test triage (a test change ships with its code change):**
- **DELETE whole file:** `test_excess_phase4.py`, `test_excess_phase4_email.py`, `test_excess_phase4_inbox.py`,
  `test_excess_solicitations.py` (pure email-RFQ/solicitation/archive — retired behavior).
- **REWRITE:** `test_excess_crud.py`, `test_excess_coverage.py`, `test_excess_service_comprehensive.py`,
  `test_excess_service_coverage.py`, `test_excess_service_nightly.py`, `test_excess_nightly.py`,
  `test_models_excess.py` (drop Bid/solicitation tests; keep+adapt list/line/import; add ExcessOffer tests).
- **KEEP:** `test_excess_lists.py` (concept-neutral).

## Execution chunking (subagent-sized; review between each)

- **A — Schema foundation:** Tasks 1-3 + schemas reshape + `test_models_excess.py` rewrite. One migration.
- **B — Service core:** Tasks 4-8 (capabilities, CRUD+resolve, intake, offers+scope+unmatched+self-offer,
  rollup) + rewrite the service test files.
- **C — Sighting mirror:** Task 9 (+ virtual requirement) + tests; PG-verify.
- **D — Router + nav + cross-module cleanup + dead-test deletion:** Task 11 + email_service/email_jobs removal +
  delete the 4 retired test files + rewrite router test files.
- **E — Bid-back + PDF:** Task 10 + Task 16.
- **F — Workspace UI:** Tasks 12-15, 18.
- **G — Customer hiding:** Task 17.
- **H — Docs + full xdist suite + live-verify on PG:** Tasks 19-20.

---

## Phase 1 — Schema & constants

### Task 1: Status enums
**Files:** Modify `app/constants.py`; Test `tests/test_constants.py` (or a new `tests/test_excess_models.py`).
**Interfaces — Produces:** `ExcessListStatus` (draft, open, collecting, bid_out, awarded, closed,
expired), `ExcessLineItemStatus` (available, bidding, awarded, withdrawn), `ExcessOfferStatus` (open,
won, lost, expired, withdrawn, late), `ExcessOfferScope` (per_line, take_all), `OfferLineMatchStatus`
(matched, unmatched, ambiguous). Values chosen to map onto existing `status_badge` keys where possible.

- [ ] Step 1: Write a failing test asserting each enum exists with the exact members above and is a `StrEnum`.
- [ ] Step 2: Run it; expect ImportError/AttributeError.
- [ ] Step 3: Add/extend the `StrEnum`s in `constants.py` (extend `ExcessListStatus` if it already exists; don't duplicate).
- [ ] Step 4: Run the test; expect PASS.
- [ ] Step 5: Commit `feat(trading): excess/offer status enums`.

### Task 2: Reshape models
**Files:** Modify `app/models/excess.py`; Test `tests/test_excess_models.py`.
**Interfaces — Produces:** `ExcessOffer` (excess_list_id, submitted_by, offerer_company_id,
offerer_vendor_card_id, scope, take_all_total_price nullable, valid_until, notes, status, created_at);
`ExcessOfferLine` (offer_id, excess_line_item_id nullable, mpn_raw, quantity, unit_price nullable,
lead_time_days nullable, terms_text, match_status); `ExcessLineItem` + `material_card_id`,
`best_offer_unit_price`, `best_offer_id`, `offer_count`; `ExcessList` + `version` (int, default 1).
Remove `Bid`, `BidSolicitation` and their relationships.

- [ ] Step 1: Write failing tests: construct an `ExcessOffer(scope='take_all')` with no lines; a
  `per_line` offer with one `ExcessOfferLine` (matched) and one unmatched (`excess_line_item_id=None`,
  `mpn_raw` set); assert `unit_price` may be None; assert validators (quantity>0) hold; assert `Bid`
  and `BidSolicitation` no longer importable from the module.
- [ ] Step 2: Run; expect failures.
- [ ] Step 3: Implement the model changes (keep file-header comment; keep indexes; cascade lines with offer).
- [ ] Step 4: Run; expect PASS.
- [ ] Step 5: Commit `feat(trading): reshape excess models (ExcessOffer/Line, retire Bid)`.

### Task 3: Alembic migration
**Files:** Create `alembic/versions/<rev>_trading_module.py`; Modify `MIGRATION_NUMBERS_IN_FLIGHT.txt`.
- [ ] Step 1: Reserve the next number in `MIGRATION_NUMBERS_IN_FLIGHT.txt`.
- [ ] Step 2: `alembic revision --autogenerate -m "trading module reshape"` (rev id ≤32 chars).
- [ ] Step 3: Review: drops `bids`/`bid_solicitations`; alters `excess_line_items` (+cols); creates
  `excess_offers`/`excess_offer_lines`; writes a complete `downgrade`.
- [ ] Step 4: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`; `alembic heads` = 1.
- [ ] Step 5: Commit `feat(trading): migration for excess reshape`.

---

## Phase 2 — Services (TDD-heavy backend)

### Task 4: Capabilities helper
**Files:** Modify `app/services/excess_service.py` (or `app/dependencies.py` for a dep); Test `tests/test_excess_capabilities.py`.
**Interfaces — Produces:** `can_post(user) -> bool`, `can_offer(user) -> bool` derived from role
(sales/trader → post; buyer/trader → offer).
- [ ] TDD: test each role's capabilities → implement → pass → commit.

### Task 5: List/line CRUD + MaterialCard resolve
**Files:** Modify `app/services/excess_service.py`; Test `tests/test_excess_service.py`.
**Interfaces — Produces:** `create_excess_list(...)`, `add_line(list_id, row)` that calls
`resolve_material_card()` and sets `normalized_part_number` via `normalize_mpn_key()`.
- [ ] TDD: adding a line resolves+links a MaterialCard and normalizes the MPN → implement → pass → commit.

### Task 6: Intake funnel (tabular / freeform / single-line)
**Files:** Modify `app/services/excess_service.py`; Test `tests/test_excess_intake.py`.
**Interfaces — Consumes:** `parse_tabular_file()`, `parse_freeform_intake(text, mode="offer")`.
**Produces:** `intake_lines(list_id, *, rows|text|single) -> IntakeResult` (matched/created counts).
- [ ] TDD: paste text → N lines created; CSV bytes → N lines; single-line → 1 line. Mock AI parser at source.

### Task 7: Offer intake + scope + unmatched queue + self-offer guard
**Files:** Modify `app/services/excess_service.py` (or new `app/services/excess_offer_service.py`); Test `tests/test_excess_offers.py`.
**Interfaces — Produces:** `submit_offer(list_id, user, scope, lines|take_all_total) -> ExcessOffer`;
raises/returns a typed error when `user.id == list.owner_id` (self-offer) or user lacks `can_offer`;
per-line rows match on `normalize_mpn_key()`; misses → `match_status='unmatched'`, queued.
- [ ] TDD: take_all offer binds list, no lines; per_line offer matches known MPN, queues unknown MPN;
  self-offer blocked; non-can_offer blocked → implement → pass → commit.

### Task 8: Best-price rollup
**Files:** Create `app/services/excess_offer_aggregation.py`; Test `tests/test_excess_rollup.py`.
**Interfaces — Produces:** `recompute_line_rollup(db, excess_line_item_id)` setting
`best_offer_unit_price`=min(unit_price over that line's offers), `best_offer_id`, `offer_count`; called
after offer create/update/withdraw. Mirror the shape of `sighting_aggregation.rebuild_vendor_summaries()`.
- [ ] TDD: 3 offers (12.40/9.90/None) → best=9.90, count=3; withdrawing the 9.90 → best=12.40 → implement → pass → commit.

### Task 9: Sighting live-mirror
**Files:** Create `app/services/excess_mirror.py`; Test `tests/test_excess_mirror.py`.
**Interfaces — Consumes:** `sighting_ingest.sighting_from_row()`, `normalize_mpn_key()`.
**Produces:** `ensure_virtual_requirement(db, excess_list) -> Requirement`;
`mirror_line(db, line)` (upsert by `(source_company_id, material_card_id)`) and `retire_line(db, line)`;
a single `sync_list_mirror(db, list)` that owns the dual-write. Sets `source_type='customer_excess'`,
`source_company_id=list.company_id`, synthesized internal `vendor_name`, `requirement_id`=virtual req.
- [ ] TDD: posting a list creates the virtual requirement + one Sighting per line with the exact fields;
  qty change updates the row; award/expire retires it; re-publish does NOT wipe siblings (upsert key) →
  implement → pass → commit. **Add a PG-specific assertion** (run against the Postgres test path if available).

### Task 10: Bid-back assembly + clean export data
**Files:** Modify `app/models/excess.py` (add `CustomerBid` + `CustomerBidLine`) or reuse Quote per
spec §Customer Bid — **decide by reading `quotes.py`**; extend the Phase-1 migration or add a follow-up
migration if a new table is chosen; Modify `app/services/excess_service.py`; Test `tests/test_excess_bid_back.py`.
**Interfaces — Produces:** `build_bid_back(list_id, selections) -> CustomerBid` seeded from
`best_offer_unit_price`; `bid_back_export_context(bid)` that **strips trader names/sources** (enforced,
not template-incidental).
- [ ] TDD: assembling a bid seeds line prices from best-per-unit; export context contains NO
  trader/vendor/source fields → implement → pass → commit.

---

## Phase 3 — Router + nav

### Task 11: Thin router + nav registration
**Files:** Modify `app/routers/excess.py`; Modify `app/templates/htmx/partials/shared/mobile_nav.html`;
Test `tests/test_excess_router.py` (+ a route-smoke test).
**Interfaces — Produces (HTML/partials):** list/workspace, detail, create-modal, add-line, import-preview,
offer-entry, offer-comparison, build-bid, award; nav item `('trading','Trading','/v2/trading', …)`.
Remove the solicit endpoint(s). Routers stay thin — all logic via services. Drop `_call_claude_bid_parse`
and `send_bid_solicitation` usage.
- [ ] TDD: each route returns 200 + the right partial for a seeded list; offerer-facing route omits
  customer fields; nav contains Trading → implement → pass → commit.

---

## Phase 4 — UI (reshape the excess templates; reuse table in spec)

> Each UI task: reshape the named existing template, wire HTMX/Alpine per the toolkit, add a render/e2e
> test, verify against the live design system (pills/cards/spacing read identically to Sightings/CRM).
> Follow the spec's **Reuse** table and **Design consistency** section exactly.

### Task 12: Workspace shell — lens switch + stat strip
**Files:** Reshape `…/excess/list.html` → Trading workspace; Test `tests/e2e/` workflow spec.
- [ ] `splitPanel()` container; lens switch (My Lists / Open to Me) via buy-plans-hub pill pattern
  (`hx-get` + `hx-push-url`, Alpine `:class`); `stat_card` triage strip (Open · Offers to review ·
  Take-all · Bids out · Awarded $), each a filter shortcut. TDD render → commit.

### Task 13: Left list (opportunity-table-v2 row + filters)
**Files:** Reshape `…/excess/row.html` + a list partial; Test render/e2e.
- [ ] `status_dot` + name + `coverage_meter` (offer coverage) + `time_text` (close date) + `filter_pill`
  stages + search; default sort = needs-attention. TDD → commit.

### Task 14: Adaptive detail (tabs; table / single-card / take-all banner)
**Files:** Reshape `…/excess/detail.html` + `…/excess/line_item_row.html`; Test render/e2e.
- [ ] Slim header + breadcrumb + chips; lazy tabs Lines·Offers·Build Bid·Activity (explicit `hx-target`);
  density rule: 1 line → `.card`, 2+ → compact table; take-all → pinned banner card. **Drop "Solicit
  Bids".** TDD the three shapes → commit.

### Task 15: Offer entry + comparison table
**Files:** Reshape `…/excess/bid_form.html` → offer entry; `…/excess/bid_list.html` → comparison;
clone `…/quote_builder/modal.html` comparison table; Test render/e2e.
- [ ] Offer form: company / optional unit_price / qty / lead / notes + scope toggle (per-line/take-all)
  + the Paste/Upload/single-line AI entry → `import_preview.html` preview/confirm. Comparison table:
  best highlighted (emerald), price-spread bar, **no auto-select**. TDD → commit.

### Task 16: Bid-back builder + PDF
**Files:** Clone `…/documents/quote_report.html` → `bid_report.html`; add a builder partial; Test PDF gen.
- [ ] Builder seeds prices from best-per-unit; PDF renders part/qty/condition/our-price only (no trader
  identities); suppress the Vendor column in any Excel export. TDD the export context cleanliness → commit.

### Task 17: Customer-hiding offerer view
**Files:** Create an offerer-facing partial/serializer; filter `customer_excess` sightings for non-owners; Test.
- [ ] TDD: offerer "Open to Me" render contains MPN/qty/condition and NOT company name/site; a non-owning
  user's sightings query excludes the seller identity → commit.

### Task 18: Remove dead code
**Files:** Delete `…/excess/solicit_modal.html`; remove solicit Alpine factory + `send_bid_solicitation`/
`_call_claude_bid_parse`; Test that nothing references them.
- [ ] grep clean; suite green → commit.

---

## Phase 5 — Docs & verify

### Task 19: APP_MAP docs
**Files:** Modify `docs/APP_MAP_DATABASE.md`, `_ARCHITECTURE.md`, `_INTERACTIONS.md`.
- [ ] Document the reshaped tables, the Trading workspace, the Sighting-mirror data flow. Commit.

### Task 20: Live-verify on PG
- [ ] `pre-commit run --all-files`; full `pytest` from inside the worktree (xdist) green.
- [ ] Deploy the branch to staging (overlay + `deploy.sh --no-commit`, per the deploy-branch recipe) OR
  run against the live PG; drive: intake → post (assert Sighting rows) → submit per-line + take-all
  offers (assert unmatched queue + best-price rollup) → build bid-back PDF (assert no trader names) →
  offerer view hides customer. Capture results.

---

## Self-review (done at write time)
- **Spec coverage:** schema (T1-3), capabilities/self-offer (T4,7), intake+AI (T5,6,15), scope+unmatched
  (T7), best-price rollup (T8), Sighting mirror+virtual req (T9), bid-back+clean export (T10,16), router+nav
  (T11), workspace/aggregate/adaptive UI (T12-14), comparison table (T15), hiding (T17), dead-code (T18),
  docs+verify (T19-20). All spec sections mapped.
- **Open forks:** all four resolved in the spec are encoded (virtual req T9, lock/version T2, late-flag
  T1/T7, strict-hide T17).
- **Type consistency:** enum/member names, `recompute_line_rollup`, `sync_list_mirror`,
  `ensure_virtual_requirement`, `submit_offer`, `build_bid_back` are used consistently across tasks.
