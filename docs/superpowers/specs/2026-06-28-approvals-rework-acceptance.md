# Approvals Tab — FROZEN SCOPE (build-ready, minimal)

> **Frozen by the user 2026-06-28. SUPERSEDES the earlier multi-phase / two-gate design** in this
> folder (`…-vision.md`, `…-phased-plan.md`). Do **not** add anything not listed here.
>
> **AVAIL does not create Sales Orders or Purchase Orders — Acctivate does.** AVAIL builds and tracks
> the **buy plan**. The buy plan is the vehicle: one object, moving end to end.

## Flow (one object — the buy plan)

1. **Convert RFQ → buy plan.** The plan is a saved record from the moment it's built; edits save to it.
   (A crash can't lose work — the record already exists.)
2. **Add the Acctivate SO#** to the buy plan.
3. **Submit → ONE approval.** The manager approves the buy plan **+ SO# together**. Single gate, single
   right (`can_approve_buy_plans`).
4. **On approval → notify the buyers.**
5. **Buyers cut POs in Acctivate** and enter the **PO#(s)** back on the plan for tracking. **PO# is
   per line/vendor** — each `BuyPlanLine` carries its own `po_number` (a plan can have several POs, one per
   vendor); this is already the schema.

## NOT in scope — do NOT build any of these

- a second approval gate or separate SO approval
- SLA timers, reminders, escalation, out-of-office delegation
- returned-for-correction status or flow
- soft-delete, undo stack, per-keystroke autosave, navigate-away popups
- any code path that creates an SO or PO

## Naming

Stop calling a quote-less buy plan a **"Sales Order."** It is a **buy plan** with a captured **SO# field**.
Keep the `sales_order_number` / `po_number` fields.

## Maps to existing code (what changes vs. what already exists)

**Already exists (reuse, don't rebuild):**
- Build buy plan from offers → persists a `DRAFT` (`create_sales_order_from_offers`, `buyplan_builder.py`).
- `BuyPlan.sales_order_number`; submit → `BUY_PLAN` engine gate → `can_approve_buy_plans`
  (`approvals/service.py`, `buyplan_workflow.py`).
- Approve → `ACTIVE` + buyer "cut PO" tasks (`_run_approve_side_effects` / `_generate_buyer_tasks`).
- Buyers enter `BuyPlanLine.po_number` per line (`confirm-po` route).

**The whole job (changes only):**
1. **Remove auto-approve** — delete `_should_auto_approve` (`buyplan_workflow.py:906`) + the
   `buyplan_auto_approve_threshold` (`config.py:251`); both call sites (`submit_buy_plan`,
   `resubmit_buy_plan`) always open the one `BUY_PLAN` gate. Every plan goes to the manager.
2. **One approval only** — fold any separate SO-verification step (`so_status` / "verify-SO") into the
   single manager approval; do not keep two approvals. The manager has the SO# on the plan and approves
   plan + SO# together.
3. **Rename** "Sales Order" → "buy plan" on the builder surface + labels (`_sales_order_new.html`, hub
   labels). Keep the `sales_order_number` / `po_number` columns and existing routes.
4. **Verify** the plan persists from build and the SO# saves to it (no new autosave machinery), and that
   the approval notifies the buyers.

## Acceptance (testable)

- No plan reaches `ACTIVE` without a manager approval (auto-approve gone).
- Exactly one approval gate fires per plan (no separate SO gate).
- The builder and labels say "buy plan," not "Sales Order"; the SO#/PO# fields persist.
- Building a plan yields a saved `DRAFT`; entering the SO# persists it; approval notifies the buyers.
