# Approvals Module — SP-2: Sales Order → Manager Gate (Design Spec)

**Date:** 2026-06-27
**Status:** Approved design — SP-2 of the Approvals-module program
**Builds on:** SP-1 shell (`2026-06-27-approvals-module-sp1-shell-design.md`, shipped PR #535),
QP+Approvals Phase 1 (`2026-06-25-qp-approvals-phase1-design.md`, engine = migration 157).
**Grounded by:** Phase-0 discovery this session against the live repo (file:line refs inline).

---

## 1. Goal

Formalize the **front half of the deal lifecycle** as a **Sales Order** without adding a new
entity or a new approval gate. The Sales Order is the **front on-ramp/framing of the existing
buy-plan submission + approval flow** — "SO approval *blends and flows into* buy-plan submission
and approval" (user, 2026-06-27). SP-2 adds a way to **originate** a deal directly from RFQ offers
("New Sales Order"), surfaces the front gate under the **Sales Orders** lifecycle tab, makes the
**SO number canonical**, and **de-collides** the Quality-Plan sales-section gate that currently
squats on the `sales_order` gate name.

## 2. Decisions locked (user, 2026-06-27)

| # | Decision | Choice |
|---|---|---|
| 1 | SO gate vs buy-plan approval | **One front gate** — the SO *is* the buy plan at its front stage; SO approval = the existing buy-plan approval. |
| 2 | QP sales-section relationship | **De-collide + share one SO#** — keep the QP section gate separate but renamed; do NOT merge it into the SO approval. |
| 3 | "New Sales Order" origination | **Direct from offers** — build the buy plan from selected RFQ offers, reusing the quote-builder offer-picker UI; no separate quote step required. |
| 4 | Ops "verify SO" check | **Keep as-is** (post-ACTIVE ops completion checkpoint); SP-2 only points it at the canonical SO#. |
| 5 | Per-approver $ caps on the SO gate | **No** — keep the existing global auto-approve threshold only. |
| 6 | Engine gate naming | **No rename** — `gate_type` stays `buy_plan`; "Sales Order" is a UI framing, not a new gate type. |

**Derived (locked, no separate question):**
- **No new entity.** The Sales Order is the `BuyPlan` row at its front lifecycle stage (DRAFT/PENDING).
- **Canonical SO# = `BuyPlan.sales_order_number`** (`app/models/buy_plan.py:82`). The QP's own
  `sales_so_number` (`app/models/quality_plan.py:67`) is **retired**; the QP reads the linked buy
  plan's SO#. Divergence becomes structurally impossible.
- **Buy Plans tab = execution surface** for ACTIVE plans; **Sales Orders tab = the front of the flow**
  (originate + submit + pending approvals).

## 3. Grounded reuse (Phase-0 discovery — reuse, do not rebuild)

The entire approval mechanism already exists and is proven live for the buy-plan gate:

- **Submit + auto-approve:** `submit_buy_plan` (`app/services/buyplan_workflow.py:36-88`);
  `_should_auto_approve` (`:906`) uses `settings.buyplan_auto_approve_threshold` (=`5000`,
  `app/config.py:251`). Under threshold + no critical AI flags → straight to ACTIVE; else → PENDING.
- **Open the gate:** `_open_engine_request_for_plan` (`:245-276`) cancels stale requests then calls
  `create_request(gate_type=BUY_PLAN, amount=total_cost, subject=plan, …)`.
- **Engine:** `create_request` (`app/services/approvals/service.py:65-129`), `decide` (`:132-264`,
  `SELECT … FOR UPDATE` first-responder-wins, idempotent), routing `route_request`
  (`app/services/approvals/routing.py:37-134`); BUY_PLAN routes to all `User.can_approve_buy_plans`
  (`:58-68`, `app/models/auth.py:68`), no amount cap.
- **Side effects (in the decide transaction):** approve → `_run_approve_side_effects`
  (`buyplan_workflow.py:136-170`) sets ACTIVE + `_generate_buyer_tasks`; reject →
  `_run_reject_side_effects` (`:173-192`) back to DRAFT. Halt path cancels open requests
  (`_cancel_open_engine_requests_for_plan`, `:195-242`).
- **Build-from-quote:** `build_buy_plan(quote_id, db)` (`app/services/buyplan_builder.py:35`),
  quote-gated at `:59-60` (must be WON/SENT); seeds chosen offers via `_quote_chosen_offers` (`:124`),
  scores all offers, assigns buyers, computes margins + AI summary.
- **Offer-picker UI + service:** quote-builder modal/data/save (`app/routers/quote_builder.py:51-174`,
  `app/services/quote_builder_service.py` — `get_builder_data`, `apply_smart_defaults`,
  `best_costs_for` `:40-70`, `save_quote_from_builder` `:426-567`). Offers keyed per requirement
  with `unit_price`, best-cost highlight, sell-price seed.
- **Shell + queue:** `TAB_ORDER`/`TAB_GATE`/`TAB_LABEL` (`app/services/approvals/queue.py:48-62`),
  `build_queue_view` + `RowVM`; tab handler + `_TAB_APPROVE_ATTR` + `_default_lens` +
  `_resolve_deal_scope` (`app/routers/htmx_views.py:11019-11171`); `_pending_section.html`,
  `_tab_sales_orders.html`, `hub.html`.
- **Ops verify-SO:** `verify_so` (`buyplan_workflow.py:306-363`), `BuyPlan.so_status`
  (`buy_plan.py:87`), completion gate `check_completion` (`:699-735`, requires `so_status=APPROVED`).

## 4. Scope

**In SP-2**
1. **New Sales Order origination** — build a buy plan directly from selected RFQ offers (no quote required).
2. **Sales Orders tab re-mapping** — the tab surfaces the front buy-plan gate + the New SO button + draft/pending SOs.
3. **Canonical SO#** — retire `QualityPlan.sales_so_number`; QP reads `BuyPlan.sales_order_number`.
4. **QP sales-section de-collision** — rename gate `sales_order` → `qp_sales` and toggle
   `can_approve_sales_orders` → `can_approve_qp_sales`; move its approve/reject **inline into the QP view**.
5. **Buy Plans tab** — becomes the ACTIVE-plans execution board (loses the gate pending-section, which
   moves to Sales Orders).
6. **Per-role landing** — `_default_lens` updated so sales/traders land on `sales_orders`.

**Out of SP-2 (named so nothing is silently dropped)**
- Deal-level **PO gate** + renaming the QP **purchasing** gate (`purchase_order` → `qp_purchasing`) +
  the `purchase_orders` tab → **SP-3**. (SP-2 leaves the `purchase_orders` tab exactly as SP-1 shipped
  it — it still maps to the QP purchasing gate; the QP-sales↔QP-purchasing naming asymmetry is a
  **known temporary state** resolved in SP-3.)
- **Merging** the QP sales section into the SO approval (decision #2 chose de-collide, not merge).
- **Per-approver $ caps** (decision #5).
- **Receiving / fall-down / re-source** → SP-3 / SP-4.
- **Acctivate write-back** — still record-only.

## 5. Data model

No new tables. Changes are additive/rename + one drop-after-backfill.

**Engine gate enum** (`app/constants.py:995-1005`)
- `ApprovalGateType.SALES_ORDER` (value `"sales_order"`) is **renamed** to `QP_SALES` (value
  `"qp_sales"`). `BUY_PLAN`, `PREPAYMENT`, `PURCHASE_ORDER` are **unchanged**.
- The deal front gate **remains** `BUY_PLAN`.

**User toggles** (`app/models/auth.py`)
- `can_approve_sales_orders` (`:77`, migration 160) → **renamed** `can_approve_qp_sales`.
- `can_approve_buy_plans` (`:68`) — **unchanged** (it is the SO/front-gate approver right).
- `can_approve_pos` (`:80`) — **unchanged** (SP-3).

**Quality Plan** (`app/models/quality_plan.py`)
- `sales_so_number` (`:67`) — **dropped** after backfilling any non-null value onto the linked
  `BuyPlan.sales_order_number` when the buy plan's is blank (data migration; QP→BuyPlan link is the
  FK at `:53`). The QP view + the Sales-section completeness check read `buy_plan.sales_order_number`.
- All other QP fields unchanged.

**Buy Plan** — no column changes. `sales_order_number` (`:82`) is the canonical SO#.

## 6. Sales Order origination (direct from offers)

**Refactor** `build_buy_plan` (`app/services/buyplan_builder.py`): extract the offer-scoring +
buyer-assignment + margin/AI-summary core into a shared helper
`_assemble_buy_plan(requisition, chosen_offers, sell_prices, db) -> BuyPlan` (unsaved). The existing
`build_buy_plan(quote_id, db)` keeps its WON/SENT guard and calls the helper via the quote's chosen
offers. A new entry point `create_sales_order_from_offers(requisition_id, selections, db, user)`
calls the same helper **without** a quote, persists a **DRAFT** BuyPlan, and returns it.

**UI flow** (reuses the quote-builder picker, new save action):
1. **New Sales Order** button on the Sales Orders tab → `GET /v2/partials/approvals/sales-orders/new`
   → a requisition picker (open requisitions with offers), then launches the existing quote-builder
   picker component pointed at the chosen requisition (`get_builder_data` / `apply_smart_defaults`).
2. **Create Sales Order** action → `POST /v2/partials/approvals/sales-orders/create` with the
   per-requirement offer selections + sell prices → `create_sales_order_from_offers` → DRAFT BuyPlan
   → render the buy-plan detail (which already carries the submit form).
3. **Submit** uses the existing path (`buy_plan_submit_partial` → `submit_buy_plan`): enter the
   canonical SO# + customer PO# + notes → auto-approve under $5k or open the `BUY_PLAN` gate → PENDING.
4. **Approve** uses the existing `POST /v2/approvals/requests/{id}/decision` → ACTIVE + buyer tasks.

The customer-facing Quote Builder tab is untouched and remains the path for customer quotes.

## 7. Tab / lens re-mapping (SP-1 shell already exists)

`app/services/approvals/queue.py`
- `TAB_GATE['sales_orders']` = `ApprovalGateType.BUY_PLAN` (was the QP sales gate).
- `TAB_GATE['buy_plans']` is **removed** — the Buy Plans tab has no pending-approval section.
- The renamed `qp_sales` gate is **not** in `TAB_GATE` (it leaves the lifecycle tabs entirely).

`app/routers/htmx_views.py`
- `_TAB_APPROVE_ATTR['sales_orders']` = `can_approve_buy_plans`; remove the `buy_plans` entry.
- Sales Orders tab body (`_tab_sales_orders.html`): replace the placeholder with the New SO button +
  a list of the user's draft/pending SOs (draft/pending buy plans, role-scoped) + the existing
  `_pending_section.html` (now showing BUY_PLAN-gate pending = "SOs awaiting approval").
- Buy Plans tab body: the deal board (today's `_board.html` with the #534 role-scoped
  `scope_toggle_url`), filtered to **post-approval statuses** (ACTIVE / HALTED / COMPLETED) — the
  DRAFT/PENDING front of the flow lives on the Sales Orders tab; no pending section.
- `_default_lens` (`:11052-11063`): SALES/TRADER → `sales_orders`; BUYER → `buy_plans`;
  manager/ops (`_can_supervise`) → `supervise`.

Final tab map:

| Tab | Pending section (gate) | Work surface |
|---|---|---|
| Sales Orders | `buy_plan` (SOs awaiting approval) | New Sales Order + draft/pending SOs |
| Buy Plans | — | post-approval (ACTIVE/HALTED/COMPLETED) execution board (role-scoped) |
| Purchase Orders | `purchase_order` (QP purchasing — unchanged, SP-3) | unchanged (SP-3) |
| Vendor Prepayments | `prepayment` | prepayments |
| Supervise | manager triage | overview |

## 8. QP sales-section de-collision (avoid orphaning)

- `app/services/quality_plan_service.py`: the gate string compares (`str(gate_type) == "sales_order"`
  at `:235,:267`) and the label map (`:42-43`) update to `"qp_sales"`. Validators + `_on_section_approved`
  (`:242-281`, stamps `sales_section_approved_at`) keep working unchanged otherwise. The Sales-section
  completeness check reads the linked `buy_plan.sales_order_number` instead of `qp.sales_so_number`.
- `app/services/approvals/routing.py:87-97`: the QP-sales routing reads `can_approve_qp_sales`.
- **QP view inline action:** add an inline Approve/Reject affordance to the QP Sales-section header for
  eligible pending recipients (reuse the `approval_row` macro + `POST /v2/approvals/requests/{id}/decision`),
  so the section gate remains actionable now that it no longer appears on a lifecycle tab.

## 9. Completeness, authz, notifications (reused)

- **Completeness:** existing `submit_buy_plan` validation gates SO submission (SO# required, etc.).
- **Authz:** server-side per-recipient check in `decide` (`service.py:177-185`) is reused unchanged;
  the Sales Orders pending section shows act buttons only to `can_approve_buy_plans` recipients.
- **Notifications:** the idempotent `approval_outbox` (in-app + email) is reused unchanged.
- **Ops verify-SO:** unchanged; reads the canonical `BuyPlan.sales_order_number`.

## 10. Migration

One Alembic migration, chained after head `161_qp_native_sections`. Revision id ≤32 chars; claimed in
`MIGRATION_NUMBERS_IN_FLIGHT.txt`.
1. Rename column `users.can_approve_sales_orders` → `can_approve_qp_sales` (`op.alter_column`).
2. `UPDATE approval_requests SET gate_type='qp_sales' WHERE gate_type='sales_order'` (data, reversible).
3. Backfill: `UPDATE buy_plans_v3 SET sales_order_number = q.sales_so_number FROM quality_plans q
   WHERE q.buy_plan_id = buy_plans_v3.id AND (buy_plans_v3.sales_order_number IS NULL OR ='')
   AND q.sales_so_number IS NOT NULL`.
4. Drop column `quality_plans.sales_so_number`.
Fully reversible downgrade (re-add the column, reverse the gate_type update, rename the toggle back).
Safe on the staging sample data; round-trip on a **throwaway** Postgres (never the staging `db`).

## 11. Testing (write alongside)

- **Origination:** `create_sales_order_from_offers` produces a correct DRAFT BuyPlan from selected
  offers (scoring + buyer assignment match the quote path); the shared helper is exercised by both paths.
- **Blended approval:** New SO → submit → auto-approve under $5k (ACTIVE, no gate); over $5k → PENDING
  → `BUY_PLAN` gate → approve → ACTIVE + buyer tasks; reject → DRAFT.
- **Canonical SO#:** QP reads `buy_plan.sales_order_number`; the backfill migration copies a QP-only SO#
  onto a blank buy-plan SO#; the dropped column round-trips on downgrade.
- **De-collision:** `qp_sales` gate routes to `can_approve_qp_sales`; the QP-view inline approve/reject
  resolves the section and stamps `sales_section_approved_at`; cross-user decide → 403 on real Postgres.
- **Tab re-mapping:** Sales Orders tab renders BUY_PLAN-gate pending rows + New SO; Buy Plans tab renders
  the ACTIVE board with no pending section; `_default_lens` lands each role on the right tab.
- Run on real Postgres for the authz/cross-user and migration cases (SQLite masks PG-invalid SQL).

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Gate-type data migration on live sample data | Reversible `UPDATE`; round-trip on throwaway PG; no rows lost (gate_type is a string column). |
| Orphaning QP-sales approvals when they leave the tab | Inline Approve/Reject added to the QP view (§8) before the tab re-map ships. |
| Refactor of `build_buy_plan` regressing the quote path | Shared helper covered by tests on **both** entry points; quote path keeps its WON/SENT guard. |
| Dropping `sales_so_number` loses data | Backfill onto the canonical buy-plan SO# first; downgrade re-adds the column. |
| Buy Plans tab losing its pending section confuses approvers | Pending approvals move to Sales Orders ("SOs awaiting approval"); Supervise still aggregates for managers. |

## 13. Deferred / needs user (later SPs)

- Deal-level PO gate + QP purchasing-gate rename + `purchase_orders` tab → **SP-3**.
- Receiving / mark-complete / fall-down → re-source → **SP-3 / SP-4**.
- Optional per-approver $ caps on the SO gate (revisit if routing needs it).
- Whether the QP sales/purchasing section approvals ever want a dedicated queue lens (currently QP-view inline).
