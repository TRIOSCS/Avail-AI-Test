# Approvals Module — SP-2: Sales Order → Manager Gate (Design Spec)

**Date:** 2026-06-27
**Status:** Approved design, **revised after Phase-0 verification + exhaustive edit-site discovery** — SP-2 of the Approvals-module program
**Builds on:** SP-1 shell (`2026-06-27-approvals-module-sp1-shell-design.md`, shipped PR #535),
QP+Approvals Phase 1 (`2026-06-25-qp-approvals-phase1-design.md`, engine = migration 157).
**Grounded by:** adversarial grounding verification + a 5-agent edit-site sweep against the live repo
(file:line refs inline + the exhaustive **Appendix A** edit-site inventory). Current alembic head =
`162_resource_and_cancellations`; SP-2 claims migration number **163**.

---

## 1. Goal

Formalize the **front half of the deal lifecycle** as a **Sales Order** without inventing a new
entity or a new approval gate. The Sales Order is the **front framing of the existing buy-plan
submission + approval flow** — "SO approval *blends and flows into* buy-plan submission and approval"
(user, 2026-06-27). SP-2 adds a way to **originate** a deal directly from RFQ offers ("New Sales
Order"), surfaces the front gate under the **Sales Orders** lifecycle tab, makes the **SO number
canonical** on the buy plan, and **de-collides** the Quality-Plan sales-section gate that currently
squats on the `sales_order` gate name.

## 2. Decisions locked (user, 2026-06-27)

| # | Decision | Choice |
|---|---|---|
| 1 | SO gate vs buy-plan approval | **One front gate** — the SO *is* the buy plan at its front stage; SO approval = the existing buy-plan approval. |
| 2 | QP sales-section relationship | **De-collide + share one SO#** — keep the QP section gate separate but renamed; do NOT merge it into the SO approval. |
| 3 | "New Sales Order" origination | **Direct from offers** — build the buy plan from selected RFQ offers, reusing the quote-builder offer-picker UI; no separate quote step required. |
| 4 | Ops "verify SO" check | **Keep as-is** (post-ACTIVE ops completion checkpoint); SP-2 only points it at the canonical SO#. |
| 5 | Per-approver $ caps on the SO gate | **No** — keep the existing global auto-approve threshold only. |
| 6 | Engine gate naming (deal front gate) | **No rename** — `gate_type` stays `buy_plan`; "Sales Order" is a UI framing, not a new gate type. |
| 7 | `BuyPlan.quote_id` blocker | **Make `quote_id` nullable** (root-cause; user-approved 2026-06-27). A directly-originated SO genuinely has no customer quote. This **supersedes** the earlier derived "no column changes" assumption. |
| 8 | Editable SO# in the QP Sales section | **Remove the editable input** — SO# entry is owned by the buy-plan submit flow; the QP renders it **read-only** from the buy plan. Single canonical write surface. |
| 9 | Sales Orders vs Buy Plans tab work surfaces | **Lifecycle split via a backward-compatible board filter** — Sales Orders tab shows DRAFT/PENDING SOs; Buy Plans tab shows ACTIVE/HALTED (+ existing COMPLETED archive). Realized by adding an **optional `statuses` param** to the shared board whose default is unchanged (so the standalone `/board` route + Supervise do not regress). |

**Derived (locked, no separate question):**
- **No new entity.** The Sales Order is the `BuyPlan` row at its front lifecycle stage (DRAFT/PENDING).
- **Canonical SO# = `BuyPlan.sales_order_number`** (`app/models/buy_plan.py:82`, `String(100)`). The QP's
  own `sales_so_number` (`app/models/quality_plan.py:67`, `String(255)`) is **retired**; the QP reads the
  linked buy plan's SO#. Divergence becomes structurally impossible.

## 3. Grounded reuse (Phase-0 — reuse, do not rebuild) — VERIFIED

Every reference below was re-verified against the live code (✅ = confirmed exact; line numbers current).

- **Submit + auto-approve:** `submit_buy_plan` (`app/services/buyplan_workflow.py:36-88`) ✅;
  `_should_auto_approve` (`:906`, returns `total < threshold and not has_critical` at `:917`) ✅ uses
  `settings.buyplan_auto_approve_threshold` (`=5000`, `app/config.py:251`) ✅.
- **Open the gate:** `_open_engine_request_for_plan` (`:245-276`) cancels stale requests then calls
  `create_request(gate_type=BUY_PLAN, amount=plan.total_cost, subject=plan, …)` (`:267-274`) ✅.
- **Engine:** `create_request` (`app/services/approvals/service.py:65-129`) ✅, `decide` (`:132-264`,
  `.with_for_update()` at `:165`, status-guard idempotency at `:173-174`, per-recipient authz at
  `:177-185`) ✅, `route_request` (`app/services/approvals/routing.py:37-134`) ✅; BUY_PLAN routes to all
  `User.can_approve_buy_plans` (`:58-68`, no amount cap) ✅.
- **Side effects (in the decide transaction):** approve → `_run_approve_side_effects`
  (`buyplan_workflow.py:136-170`, sets ACTIVE `:164` + `_generate_buyer_tasks` `:169`) ✅; reject →
  `_run_reject_side_effects` (`:173-192`, back to DRAFT `:187`) ✅; halt → `_cancel_open_engine_requests_for_plan`
  (`:195-242`) ✅.
- **Build-from-quote:** `build_buy_plan(quote_id, db)` (`app/services/buyplan_builder.py:35`) ✅,
  quote-gated WON/SENT (`:59-60`) ✅; `_quote_chosen_offers` (`:124`) ✅. Quote-less duplicate guard gap:
  the existing dup guard keys on `quote_id` (`:62-74`) — see §6.
- **Offer-picker UI + service:** quote-builder routes (`app/routers/quote_builder.py:51-206` — modal `:51`,
  data `:144`, **save `:177-206`** ⚠️ *corrected from the prior `:51-174`*); `quote_builder_service.py`
  (`get_builder_data`, `apply_smart_defaults`, `best_costs_for` `:40-70`, `save_quote_from_builder` `:426-567`) ✅.
- **Shell + queue:** `TAB_ORDER`/`TAB_GATE`/`TAB_LABEL`/`DEFAULT_TAB` (`app/services/approvals/queue.py:48-61`) ✅,
  `build_queue_view` (`:108`) + `RowVM` (`:77`) ✅; tab handler `approvals_tab_partial` + `_TAB_APPROVE_ATTR`
  (`:11044`) + `_default_lens` (`:11052-11063`) + `_resolve_deal_scope` (`:11019`) in `htmx_views.py` ✅;
  `_pending_section.html`, `_tab_sales_orders.html` (under `templates/htmx/partials/approvals/`),
  `hub.html` (under `templates/htmx/partials/buy_plans/`) ✅.
- **Ops verify-SO:** `verify_so` (`buyplan_workflow.py:306-363`, reads no quote ✅), `BuyPlan.so_status`
  (`buy_plan.py:87`), completion gate `check_completion` (`:699-735`, requires `so_status=APPROVED` `:721`) ✅.

## 4. Scope

**In SP-2**
1. **`quote_id` → nullable** (migration + the new builder) so an SO can be originated without a quote.
2. **New Sales Order origination** — `create_sales_order_from_offers(...)`: build a DRAFT BuyPlan
   (`quote_id=None`) directly from selected RFQ offers, with a requisition-keyed duplicate guard.
3. **Requisition fallbacks** in the 3 display/notification helpers so SO-origin plans render a customer.
4. **Sales Orders tab re-mapping** — the tab surfaces the BUY_PLAN front gate + the New SO button +
   the DRAFT/PENDING SO work surface.
5. **Buy Plans tab** — loses the pending-approval section; its board filters to ACTIVE/HALTED
   (+ existing COMPLETED archive) via the new optional `statuses` param.
6. **Canonical SO#** — retire `QualityPlan.sales_so_number`; QP reads `BuyPlan.sales_order_number`;
   remove the editable SO# input from the QP Sales section (decision #8).
7. **QP sales-section de-collision** — rename gate `ApprovalGateType.SALES_ORDER` → `QP_SALES`
   (value `"sales_order"` → `"qp_sales"`, **+ a data migration** for existing rows) and toggle
   `can_approve_sales_orders` → `can_approve_qp_sales`; move its approve/reject **inline into the QP view**.
8. **Per-role landing** — `_default_lens` unchanged is acceptable (sales/traders still land on the
   `buy_plans` deal board); the **New SO** button is the origination entry point on the Sales Orders tab.

**Out of SP-2 (named so nothing is silently dropped)**
- Deal-level **PO gate** + renaming the QP **purchasing** gate (`purchase_order` → `qp_purchasing`) +
  the `purchase_orders` tab → **SP-3**. SP-2 leaves the `purchase_orders` tab exactly as SP-1 shipped it;
  the QP-sales↔QP-purchasing naming asymmetry (`qp_sales` renamed, `purchase_order` not yet) is a
  **known temporary state** resolved in SP-3.
- **Merging** the QP sales section into the SO approval (decision #2 chose de-collide, not merge).
- **Per-approver $ caps** (decision #5).
- **Receiving / fall-down / re-source** → SP-3 / SP-4.
- **Acctivate write-back** — still record-only.
- **Renaming the `set_sales_order_approver` route / handler / `can_approve_sales_orders`-grant UI label** —
  the admin grant now governs the `qp_sales` gate; a copy/label pass to call it "QP Sales-section approval"
  is a follow-up (tracked in §13), NOT a launch blocker. The **column** is renamed; the **route name** is not.

## 5. Data model

No new tables. One migration (rev **163**, chained after `162_resource_and_cancellations`) with five
operations; migrations 160 and 161 are **immutable shipped history — never edited**.

**(a) `BuyPlan.quote_id` → nullable** (`app/models/buy_plan.py:78`)
- `nullable=False` → `nullable=True`; keep `ForeignKey("quotes.id", ondelete="CASCADE")`. The relationship
  (`:141`) and index `ix_bpv3_quote` (`:165`) need no change. Dropping NOT NULL on PG 16 is a catalog-only
  change (no table rewrite). A NULL FK references nothing, so no cascade fires for SO-origin plans.

**(b) Engine gate enum** (`app/constants.py:1004`)
- `ApprovalGateType.SALES_ORDER` (value `"sales_order"`) → `QP_SALES` (value `"qp_sales"`). Still exactly
  4 members; `BUY_PLAN`/`PREPAYMENT`/`PURCHASE_ORDER` unchanged. The deal front gate **remains** `BUY_PLAN`.
- **Mandatory data migration:** `approval_requests.gate_type` is `String(50)` with **no CHECK constraint**,
  so persisted `"sales_order"` rows must be rewritten or they fall out of routing/queue/section dispatch.

**(c) User toggle** (`app/models/auth.py:77`)
- `can_approve_sales_orders` → `can_approve_qp_sales` (via `op.alter_column(... new_column_name=...)`).
  `can_approve_buy_plans` (`:68`) and `can_approve_pos` (`:80`) — **unchanged**. The renamed toggle governs
  the `qp_sales` gate routing (`routing.py:94`). Naming asymmetry (gate value `qp_sales` ↔ column
  `can_approve_qp_sales`, but route/handler still `sales_order(_approver)`) is intentional for this scope.

**(d) Quality Plan** (`app/models/quality_plan.py:67`)
- `sales_so_number` (`String(255)`) — **dropped after backfill** onto the linked `BuyPlan.sales_order_number`
  when the buy plan's is blank. The QP view + the Sales-section completeness check read
  `buy_plan.sales_order_number`. **Length guard:** the backfill copies `String(255)`→`String(100)`; SO#s are
  short TSO codes, but the migration **pre-checks** `max(length(sales_so_number))` and aborts loudly if any
  value exceeds 100 (no silent truncation). All other QP fields unchanged.

**(e) Buy Plan** — no further column changes. `sales_order_number` (`:82`) is the canonical SO#.

## 6. Sales Order origination (direct from offers)

**New builder** `create_sales_order_from_offers(requisition_id, selections, sell_prices, db, user) -> BuyPlan`
in `app/services/buyplan_builder.py`, a **sibling** of `build_buy_plan` (which stays quote-required —
do NOT relax its WON/SENT guard at `:59-60`). To avoid duplication, extract the offer-scoring +
buyer-assignment + margin/AI-summary core that both paths share into a helper
`_assemble_buy_plan(requisition, chosen_offers, sell_prices, customer_region, db) -> BuyPlan` (unsaved):
- `build_buy_plan(quote_id)` calls it via the quote's chosen offers + `quote.customer_site`-derived region.
- `create_sales_order_from_offers(...)` calls it with `quote_id=None`, sourcing **customer/region from the
  requisition** (`requisition.customer_name`, `requisition.customer_site` if present; region `None` → geo
  flag skipped, acceptable), persists a **DRAFT** BuyPlan, returns it.
- **Duplicate guard (new):** before persisting, block if a non-terminal (DRAFT/PENDING/ACTIVE) BuyPlan
  already exists for this `requisition_id` (the existing `quote_id`-keyed guard cannot see quote-less plans).
  Surface the existing SO rather than creating a second.

**Requisition fallbacks (so SO-origin plans don't render blank)** — `quote_id=None` never crashes (every
reader is already guarded), but three helpers return blank/Unknown and need a `requisition`-derived fallback:
- `buyplan_hub._customer_name` (`:65-77`) — falls to em dash on every Deal Hub card / triage row / card title.
- `buyplan_workflow.generate_case_report` (`:1108-1118`) — Customer="Unknown", Quote="—".
- `buyplan_notifications._plan_context` (`:63-82`) — blank `customer_name`/`quote_number` in submit/approve/
  reject/cancel emails + Teams cards.
Add `joinedload(BuyPlan.requisition)→customer_site→company` alongside the existing
`joinedload(BuyPlan.quote)…` at the ~11 hub query sites to keep SO-origin cards N+1-free.
The reference pattern is already in `buy_plans/detail.html:87-104` and `qp/detail.html:42-44` (requisition-first).

**UI flow** (reuses the quote-builder picker, new save action):
1. **New Sales Order** button on the Sales Orders tab → `GET /v2/partials/approvals/sales-orders/new`
   → a requisition picker (open requisitions that have offers — predicate: `RequisitionStatus in
   {open, rfqs_sent, offers, quoted}` AND ≥1 requirement with a selectable offer), then launches the
   existing quote-builder picker pointed at the chosen requisition (`get_builder_data`/`apply_smart_defaults`).
2. **Create Sales Order** → `POST /v2/partials/approvals/sales-orders/create` with per-requirement offer
   selections + sell prices → `create_sales_order_from_offers` → DRAFT BuyPlan → render the buy-plan detail
   (which already carries the submit form), swapped inline.
3. **Submit** uses the existing path (`buy_plan_submit_partial` → `submit_buy_plan`): enter the canonical
   SO# + customer PO# + notes → auto-approve under $5k (straight to ACTIVE) or open the `BUY_PLAN` gate → PENDING.
4. **Approve** uses the existing `POST /v2/approvals/requests/{id}/decision` → ACTIVE + buyer tasks.

The customer-facing Quote Builder tab is untouched and remains the path for customer quotes.

## 7. Tab / lens re-mapping (SP-1 shell already exists)

**`app/services/approvals/queue.py`** (exact edits — see Appendix A.5):
- `TAB_ORDER` (`:48`): drop `"buy_plans"` → `["sales_orders", "purchase_orders", "prepayments"]` (every
  `TAB_ORDER` key must remain a key of `TAB_GATE`/`TAB_LABEL`, else the pill comprehension at `:146` and the
  `_smart_default_tab` loop at `:190-191` `KeyError`).
- `TAB_GATE` (`:49-54`): remove `"buy_plans"`; repoint `"sales_orders"` → `ApprovalGateType.BUY_PLAN`
  (this is the core remap — the Sales Orders tab now surfaces buy-plan-gate requests).
- `TAB_LABEL` (`:55-60`): remove the `"buy_plans"` key (keys-match invariant).
- `DEFAULT_TAB` (`:61`): `"buy_plans"` → `"sales_orders"`.
- Module + `_smart_default_tab` docstrings: "four tabs" → "three gate tabs"; "tie/zero → Buy Plans" → "→ Sales Orders".

**`app/routers/htmx_views.py`** (Appendix A.5):
- `_TAB_APPROVE_ATTR` (`:11044-11049`): remove `"buy_plans"`; repoint `"sales_orders"` → `"can_approve_buy_plans"`.
- Handler `approvals_tab_partial` (`:11128-11132`): guard the queue build behind the attr map so the
  gate-less `buy_plans` lens is skipped without `KeyError`:
  `if lens in _TAB_APPROVE_ATTR: ctx["view"] = build_queue_view(db, user, lens); ctx["show_pending"] = bool(getattr(user, _TAB_APPROVE_ATTR[lens], False))`.
  `supervise` returns earlier (`:11125`); `buy_plans` falls through to its own branch (`:11134-11151`).
- `_default_lens` (`:11052-11063`): **unchanged** (decision #8). `_APPROVALS_TABS` (`:11040`) keeps
  `buy_plans` + `supervise` (still routable, just gate-less).

**Templates:**
- `_tab_buy_plans.html`: **delete the `_pending_section` include** (`:9`) — root-cause removal of the gate
  from this tab (not relying on Jinja `Undefined` short-circuiting); update its header comment.
- `_tab_sales_orders.html`: New SO button + the DRAFT/PENDING SO work surface (board filtered via the new
  `statuses` param, role-scoped) + the existing `_pending_section.html` (now showing BUY_PLAN-gate pending,
  labeled "Sales Orders awaiting approval"), shown to `can_approve_buy_plans` approvers.

**Shared board `statuses` param (decision #9):** add an optional `statuses` arg to `deals_board`
(`buyplan_hub.py:448`) / the board partial. **Default = today's full set** (DRAFT/PENDING/ACTIVE/HALTED;
COMPLETED via `completed_archive`; CANCELLED excluded) so the standalone `GET /v2/partials/buy-plans/board`
route (`htmx_views.py:11212`) and Supervise are **regression-free**. Tabs pass filtered sets:
- Sales Orders work surface → `statuses=[DRAFT, PENDING]`.
- Buy Plans tab board → `statuses=[ACTIVE, HALTED]` (+ existing COMPLETED archive section).
- CANCELLED stays excluded from both boards (unchanged behavior).

Final tab map:

| Tab | Pending section (gate) | Work surface (board `statuses`) |
|---|---|---|
| Sales Orders | `buy_plan` ("Sales Orders awaiting approval") | New SO button + DRAFT/PENDING SOs |
| Buy Plans | — | ACTIVE/HALTED (+ COMPLETED archive); standalone `/board` unchanged |
| Purchase Orders | `purchase_order` (QP purchasing — unchanged, SP-3) | unchanged (SP-3) |
| Vendor Prepayments | `prepayment` | prepayments |
| Supervise | manager triage | overview (unchanged) |

## 8. QP sales-section de-collision (avoid orphaning) — ORDERING-CRITICAL

When the `sales_orders` tab repoints to `BUY_PLAN`, the renamed `qp_sales` gate leaves the lifecycle tabs
entirely. To avoid orphaning in-flight QP-sales approvals, the **QP-view inline action must ship in the
same PR as (and is verified before) the tab remap** — hard ordering constraint.

- **Enum/value renames** (Appendix A.2): `app/constants.py:1004`; enum-member refs in `quality_plans.py`
  (`:193,196,397,453,454,503`), `service.py:256` (the `decide` section-dispatch tuple), `routing.py:87`,
  `queue.py:51` (value only); bare value strings `"sales_order"` in `quality_plans.py:440`,
  `quality_plan_service.py:42,235,267`, and the audit detail in `admin/users.py:422`.
- **Column rename** (Appendix A.1): `auth.py:77`; `routing.py:94`; `admin/users.py:162,412,415`;
  `htmx_views.py:11046` (string via `getattr` — **fails soft** if missed, silently hiding the pending
  section); `settings/users.html:146,150` (dict key ↔ template `row.*` must move together).
- **QP view inline action:** add an inline Approve/Reject affordance to the QP Sales-section header for
  eligible pending recipients (reuse the `approval_row` macro + `POST /v2/approvals/requests/{id}/decision`).
  The `qp_sales` request's `subject_type=QUALITY_PLAN`; verify `approval_row` renders a QP-subject row
  (subject label/href, `can_act`) — if not, add a QP-subject variant (the queue already builds QP-subject
  RowVMs, so the macro path is expected to work).
- **`sales_so_number` retirement** (Appendix A.3): drop `quality_plan.py:67`; remove the editable input
  `_section_sales.html:95` and repoint the read-only display `:34` to `qp.buy_plan.sales_order_number`
  (or omit — the QP header already shows it at `detail.html:90`); drop the `_SALES_FIELDS` whitelist entry
  `quality_plans.py:67`; replace the `_SALES_REQUIRED` tuple entry `quality_plan_service.py:185` with a
  dedicated buy-plan-sourced completeness check (`(qp.buy_plan.sales_order_number or "").strip()`, None-guarded);
  add `joinedload(QualityPlan.buy_plan)` where the sales section now reads the buy plan.

## 9. Completeness, authz, notifications (reused)

- **Completeness:** existing `submit_buy_plan` validation gates SO submission (SO# required, etc.). The QP
  Sales-section completeness now reads `buy_plan.sales_order_number` (§8).
- **Authz:** server-side per-recipient check in `decide` (`service.py:177-185`) reused unchanged; the Sales
  Orders pending section shows act buttons only to `can_approve_buy_plans` recipients.
- **Notifications:** the idempotent `approval_outbox` (in-app + email) reused unchanged; SO-origin plans get
  a requisition-derived customer name (§6).
- **Ops verify-SO:** unchanged; reads the canonical `BuyPlan.sales_order_number`.

## 10. Migration (rev 163, chained after `162_resource_and_cancellations`)

One Alembic migration; revision id ≤32 chars; claim **163** in `MIGRATION_NUMBERS_IN_FLIGHT.txt` in the
same commit; `alembic heads` must show a single head. **upgrade():**
1. `op.alter_column("buy_plans_v3", "quote_id", existing_type=sa.Integer(), nullable=True)`.
2. `op.alter_column("users", "can_approve_sales_orders", new_column_name="can_approve_qp_sales")`.
3. `UPDATE approval_requests SET gate_type='qp_sales' WHERE gate_type='sales_order'` (data, reversible).
4. Length pre-check, then backfill:
   `... ABORT if any quality_plans.sales_so_number > 100 chars`; then
   `UPDATE buy_plans_v3 SET sales_order_number = q.sales_so_number FROM quality_plans q
   WHERE q.buy_plan_id = buy_plans_v3.id AND (buy_plans_v3.sales_order_number IS NULL OR ='')
   AND q.sales_so_number IS NOT NULL`.
5. `op.drop_column("quality_plans", "sales_so_number")`.

**downgrade()** reverses 1–3 and re-adds the dropped column (`String(255)`, nullable). It is **partially
lossy and order-coupled**, documented in the migration:
- Step 1's reverse (`quote_id` → NOT NULL) **fails if any SO-origin (`quote_id IS NULL`) rows exist** — a
  downgrade must first delete/backfill those rows. Never run a bare downgrade on a DB that has used origination.
- The re-added `sales_so_number` comes back **empty** (per-QP values were moved to the buy plan, not restored).
- Because deployed code reads the new names, a schema downgrade without rolling back the app breaks the app —
  **roll back code and schema together.**

Round-trip on a **throwaway** Postgres (`docker run --rm -d … postgres:16-alpine`), never the staging `db`.
Verify on real PG (SQLite masks PG-invalid `UPDATE…FROM`).

## 11. Testing (write alongside)

- **Nullable contract:** new `tests/test_buy_plan_models.py` case — `BuyPlan(quote_id=None, requisition_id=…)`
  persists and `plan.quote is None`.
- **Origination:** `create_sales_order_from_offers` produces a correct DRAFT BuyPlan from selected offers
  (scoring + buyer assignment match the quote path via the shared `_assemble_buy_plan`); the requisition-keyed
  duplicate guard blocks a second open SO for the same requisition.
- **Requisition fallbacks:** `_customer_name`, `generate_case_report`, `_plan_context` render the requisition
  customer (not em dash / "Unknown" / blank) for a `quote_id=None` plan.
- **Blended approval:** New SO → submit → auto-approve under $5k (ACTIVE, no gate); over $5k → PENDING →
  `BUY_PLAN` gate → approve → ACTIVE + buyer tasks; reject → DRAFT.
- **Canonical SO#:** QP reads `buy_plan.sales_order_number`; the backfill copies a QP-only SO# onto a blank
  buy-plan SO#; the length pre-check aborts on a >100-char value; the dropped column round-trips on downgrade.
- **De-collision:** `qp_sales` gate routes to `can_approve_qp_sales`; the QP-view inline approve/reject
  resolves the section and stamps `sales_section_approved_at`; cross-user decide → 403 on real Postgres.
- **Tab re-mapping:** Sales Orders tab renders BUY_PLAN-gate pending rows + New SO; Buy Plans tab renders the
  ACTIVE/HALTED board with no pending section and no `KeyError`; the standalone `/board` route is unchanged.
- **Existing-test migration:** update `tests/test_approvals_queue.py`, `tests/test_c2a_gates.py`,
  `tests/test_c2b_sections.py`, `tests/test_approval_constants.py`, `tests/test_approvals_module_shell.py`
  per Appendix A (gate/column renames, tab arg `"buy_plans"`→`"sales_orders"`, SO# fixture moves to the buy plan).
- Run the authz/cross-user + migration cases on real Postgres (SQLite masks PG-invalid SQL).

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **Split-deploy** strands in-flight `sales_order`-gate rows (won't resolve via `decide` §256 tuple) | Ship the gate-value data migration **with** the code; deploy is atomic (entrypoint runs `alembic upgrade head` before app start). |
| `getattr` string miss at `htmx_views.py:11046` **fails soft** → pending section silently hidden | Explicit edit-site in Appendix A.1; covered by `test_approvals_module_shell.py` (section-present-for-approver). |
| Dict-key ↔ template `row.*` drift → toggle silently unchecked | Rename `users.py:162` key + `users.html:146,150` atomically (Appendix A.1). |
| SO-origin plans render blank customer (silent, high-visibility) | Requisition fallback in the 3 helpers (§6) + N+1-avoiding joinedload. |
| Orphaning QP-sales approvals when the gate leaves the tabs | QP-view inline Approve/Reject ships in the same PR, verified first (§8 ordering constraint). |
| `build_buy_plan` refactor regressing the quote path | Shared `_assemble_buy_plan` covered by tests on **both** entry points; quote path keeps its WON/SENT guard. |
| Dropping `sales_so_number` loses data / truncates on backfill | Backfill onto the canonical SO# first with a length pre-check; downgrade re-adds the column (empty, documented). |
| Admin "Sales Orders approval" grant now governs `qp_sales`, not the SO tab → operator confusion | Tracked label/copy follow-up (§13); functionally correct (column renamed, routing intact). |
| Buy-plan approvers don't auto-land on the Sales Orders tab (their queue) | Accepted per decision #8; flagged for product — a `_default_lens` tweak is a cheap SP-3 follow-up if desired. |

## 13. Deferred / follow-ups

- Deal-level PO gate + QP purchasing-gate rename (`purchase_order` → `qp_purchasing`) + `purchase_orders` tab → **SP-3**.
- Receiving / mark-complete / fall-down → re-source → **SP-3 / SP-4**.
- Rename the `set_sales_order_approver` route/handler + the admin grant UI label to "QP Sales-section approval"
  (the column is renamed in SP-2; the route/label is cosmetic) — small copy PR.
- Optional `_default_lens` tweak so `can_approve_buy_plans` approvers land on the Sales Orders tab.
- Whether the QP sales/purchasing section approvals ever want a dedicated queue lens (currently QP-view inline).

---

## Appendix A — Exhaustive edit-site inventory (from the 5-agent discovery sweep)

Grounding for the implementation plan. "MANDATORY" = code breaks if missed; "DOC" = accuracy-only.
Verify after edits: `grep -rn can_approve_sales_orders app/ alembic/ tests/` should return **only** migration
160 (immutable); `grep -rn '"sales_order"' app/` should return no gate-type uses.

### A.1 — `User.can_approve_sales_orders` → `can_approve_qp_sales`
- **MANDATORY:** `app/models/auth.py:77` (def); `app/routers/admin/users.py:162` (read + dict key),
  `:412,:415` (read/write); `app/routers/htmx_views.py:11046` (string via `getattr` — *fails soft*);
  `app/services/approvals/routing.py:94` (query filter); `app/templates/htmx/partials/settings/users.html:146,150`
  (`row.*`, coupled to the `users.py:162` dict key); tests `test_c2a_gates.py:62,73,163,164,165,178,179,192,206,224,256,273,335,342,347,373,418`,
  `test_c2b_sections.py:48,55,142,156,204,349`, `test_approvals_queue.py:548`.
- **NEW migration** `op.alter_column("users","can_approve_sales_orders",new_column_name="can_approve_qp_sales")` (+ reverse).
- **DOC:** `admin/users.py:149,404`; `routing.py:15,88`; `quality_plan_service.py:289`; test docstrings;
  `docs/APP_MAP_DATABASE.md:38`, `docs/APP_MAP_INTERACTIONS.md:1344,1975,5196,5271`.
- **DO NOT TOUCH:** migration `160` (history); route `/sales-order-approver` + handler `set_sales_order_approver`
  (`admin/users.py:393-394`); `can_approve_buy_plans`/`can_approve_pos`.

### A.2 — `ApprovalGateType.SALES_ORDER` (`"sales_order"`) → `QP_SALES` (`"qp_sales"`)
- **MANDATORY (member):** `app/constants.py:1004` (def); `quality_plans.py:193,196,397,453,454,503`;
  `service.py:256` (decide section-dispatch tuple); `routing.py:87`; `queue.py:51` (value only);
  tests `test_approval_constants.py:25,34`, `test_approvals_queue.py:168,227,346,419,462,552`,
  `test_c2a_gates.py:168,181,208,227,229,260,264,275,395,436`, `test_c2b_sections.py:132,145,158,159,170,189`.
- **MANDATORY (bare value string):** `quality_plans.py:440`; `quality_plan_service.py:42` (`_SECTION_LABEL` key),
  `:235,:267`; `admin/users.py:422` (audit detail); `test_approvals_queue.py:185`.
- **NEW data migration:** `UPDATE approval_requests SET gate_type='qp_sales' WHERE gate_type='sales_order'` (+ reverse).
- **FALSE POSITIVES (leave):** `sales_order_number`, `can_approve_sales_orders`, the `"sales_orders"` lens/tab keys,
  `set_sales_order_approver`/`/sales-order-approver`, `CARD_KIND_SALES_ORDER="SO"`.

### A.3 — Retire `QualityPlan.sales_so_number` → read `BuyPlan.sales_order_number`
- **MANDATORY:** drop `quality_plan.py:67`; `_section_sales.html:34` (repoint read or remove), `:95`
  (**remove editable input**, decision #8); `quality_plans.py:67` (drop `_SALES_FIELDS` entry);
  `quality_plan_service.py:185` (replace `_SALES_REQUIRED` entry with buy-plan-sourced check at `:216-218`);
  tests `test_c2a_gates.py:124` + `test_c2b_sections.py:95` (move SO# to `bp.sales_order_number`), verify
  `test_c2b_sections.py:113-117,366`. Add `joinedload(QualityPlan.buy_plan)` to the sales-section read path.
- **NEW migration:** backfill-then-drop (§10 steps 4–5); downgrade re-adds `String(255)` (empty).
- **DO NOT EDIT:** migration `161` (history; `:37` is the authoritative `String(255)` for the downgrade).
- **DOC:** `docs/APP_MAP_DATABASE.md:359` (18→17 cols). No change at `qp/detail.html:45,90` (already reads the buy plan).

### A.4 — `BuyPlan.quote_id` nullable — consumer audit
- **MANDATORY:** `buy_plan.py:78` (nullable=True) + NEW migration (§10 step 1).
- **FALLBACK edits (§6):** `buyplan_hub.py:65-77` (`_customer_name`) + joinedload at `:106,178,239,483,554,646,661,673,696,712,728`;
  `buyplan_workflow.py:1108-1118` (`generate_case_report`); `buyplan_notifications.py:63-82` (`_plan_context`).
- **NO CHANGE (already None-safe, confirmed):** `buyplan_builder.py:329-332`; `crm/quotes.py:356-358`;
  `proactive_service.py:631,680,533-539`; `htmx_views.py:11336`; `buy_plans/detail.html:87-104`; `qp/detail.html:42-44`.
- **NEW builder:** `create_sales_order_from_offers` + `_assemble_buy_plan` helper + requisition-keyed dup guard
  (`build_buy_plan:35-96,62-74` stays quote-required).
- **NOTE:** `seed_sample_data.py:1660` sweeps via `quote_id` — a future SO-origin sample row needs a
  requisition-based sweep (not a blocker; no SO-origin rows are seeded).

### A.5 — Queue/tab restructure (no `KeyError`, no shared-route regression)
- **`queue.py`:** `:48` TAB_ORDER drop `buy_plans`; `:49-54` TAB_GATE remove `buy_plans` + `sales_orders`→`BUY_PLAN`;
  `:55-60` TAB_LABEL remove `buy_plans`; `:61` DEFAULT_TAB→`sales_orders`; docstrings `:5-6,11-12,187`.
- **`htmx_views.py`:** `:11044-11049` `_TAB_APPROVE_ATTR` remove `buy_plans` + `sales_orders`→`can_approve_buy_plans`;
  `:11128-11132` guard the queue build behind `if lens in _TAB_APPROVE_ATTR`.
- **Template:** delete `_pending_section` include at `_tab_buy_plans.html:9` (+ header comment).
- **Board param:** optional `statuses` on `deals_board` (`buyplan_hub.py:448`) defaulting to current behavior;
  tabs pass `[DRAFT,PENDING]` (Sales Orders) / `[ACTIVE,HALTED]` (Buy Plans). **Do not** change `deals_board`'s
  default or `_board.html` (shared with standalone `/board` `htmx_views.py:11212` + Supervise).
- **Tests:** `test_approvals_queue.py:154-185,219,235,297,329,338-355,405-444,447-479,501,539,544-559,4`
  (rebind to BUY_PLAN gate / `"sales_orders"` tab; move QP-subject coverage to `purchase_orders`);
  `test_approvals_module_shell.py:197-205` (URL → `/v2/partials/approvals/sales-orders`), `:187-194` (move assertion).
