# Buy-Plan Approval + Quality-Plan Merge — Design Spec (2026-07-02)

Phase 3 of the workflow-completion program (plan: `2026-07-02-workflow-completion-plan.md`).
Grounded in the code-architect blueprint (read-only analysis, all file:line verified).
**User-approved 2026-07-02** — build to this; no TBDs.

## Locked decisions (user)
- **Approval model = TWO gates.** Every buy plan requires the manager "Buy Plan" approval (already universal, threshold-free) THEN a deal-level "PO sign-off" before it can move to receiving. Remove the `$5k` threshold so the PO gate applies to **every** plan.
- **In-flight plans = BACKFILL.** A data migration retroactively opens a PO-approval request for every currently-ACTIVE plan that skipped the gate. Dry-run the count + get explicit go/no-go before running (bulk prod-data change).
- **QP = MERGE into the buy plan.** Fold the Quality-Plan Sales/Purchasing sections into the buy-plan detail as embedded, editable-until-approved content; retire the separate `QP_SALES`/`QP_PURCHASING` gates and the dead top-level "Submit for Review".

## Following the architect's recommendations (no further input needed)
- PO gate opens automatically when the BUY_PLAN gate approves (ACTIVE) — not buyer-initiated (D.1-A).
- PO reject → plan stays ACTIVE + "Resubmit for PO Approval"; Halt remains the only forced stop (D.3).
- QP completeness = advisory inline banner, does NOT block approval (D.5).
- Drop `QualityPlan.purchasing_po_number` (per-line `BuyPlanLine.po_number` is canonical) (D.6).
- Drop `can_approve_qp_sales`/`can_approve_qp_purchasing` columns + their Settings UI toggles (D.7).
- Delete `po_auto_approve_threshold` from config (D.8).

## Real bug to fix as part of this (architect found it)
`buyplan_workflow.check_completion()` auto-completes a plan straight from ACTIVE without checking for an open PURCHASE_ORDER gate → the gate is silently bypassed+cancelled in the common case. Add `_has_open_po_gate()` guard so a plan cannot complete while a PO request is REQUESTED. Also `approvals/service.decide()` has NO reject branch for PURCHASE_ORDER — add it.

## Build sequence (each step = shippable CI-gated PR, deployed + live-verified)
1. **Area 1 core (no migration):** delete threshold; `_maybe_open_po_gate`→`_open_po_gate` (unconditional); strip threshold from `plan_needs_approver_reason` + `_query_stuck_no_approver_plans`; add `_has_open_po_gate` + fix `check_completion`; add `_run_po_reject_side_effects` + `resubmit_po_gate` + the reject dispatch in `approvals/service.py`. Update/replace SP-3 threshold tests; add reject/resubmit/completion-race tests.
2. **Area 2 PO decide UI (no migration):** `po_request_decide` + `resubmit-po` routes (wrap `svc_decide`, origin-aware like `prepay_request_decide`); `_po_approve_rows` + My-Queue wiring (kind `po_approve`, priority 3); detail-page Approve/Reject banner (+ reject-reason modal) + resubmit banner. Closes BP-2/BP-3, makes the nav badge point at real work.
3. **Migration 175 (in-flight backfill):** raw-SQL open a PO request+step+recipients+genesis event per ACTIVE plan with no open/approved PO gate (mirrors `create_request`); skip null-total / no-eligible-approver. Dry-run count → user go/no-go → round-trip on throwaway PG → deploy with backup.
4. **Area 3 QP merge:** new `_detail_quality_plan.html` embedded in buy-plan detail (Sales/Purchasing/Serial/FRU sections, `qp_locked = bp.status not in ('draft','pending')`); eager get-or-create QP in `buy_plan_detail_partial`; delete retired QP routes/service fns/dispatch/routing/admin endpoints/Settings columns; 302 the legacy `/v2/qp/*` URLs to the parent plan.
5. **Migration 176:** cancel stray open QP-section requests; drop the 4 dead columns (`qp.status`, 2 section-approved timestamps, 2 user QP-approver flags) + `purchasing_po_number`.
6. **Verify + docs:** full suite + `pre-commit --all-files`; live-verify DRAFT→PENDING→ACTIVE→[PO decide]→INBOUND→Received→COMPLETED with an embedded QP locking at approval; refresh APP_MAP docs.

Migration numbers: claim in `MIGRATION_NUMBERS_IN_FLIGHT.txt` at build time (head was 174 → 175/176 free). Full file list + code sketches in the architect blueprint (session transcript / journal).
