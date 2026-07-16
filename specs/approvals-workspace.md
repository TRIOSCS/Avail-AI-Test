# Spec: Approvals Workspace

**Status:** v4 — vision locked, grounded in TRIO's real process
**Owner:** Mike | **Date:** July 2026
**Replaces (the four-tool stack):** Teams Approvals (TSO / TPO / Prepayment forms), the per-TSO Quality Plan Excel workbooks on SharePoint, and the Microsoft Planner buy-plan boards — plus the current `/v2/approvals` hub and the old Buy Plans hub page in AVAIL.
**Save as:** `specs/approvals-workspace.md`

> Backend names come from the July 2026 code sweep; process facts come from TRIO's live Teams approvals log and QP workbooks. Claude Code must verify every code name against the repo (Step 0). Where a real name differs, the real name wins.

---

## 1. The concept

One page, four tabs — each tab replaces one of today's tools, and all four are lenses on the same pipeline rooted at the sales order:

| Tab | Replaces |
|---|---|
| Sales Orders | Teams "TSO Approval Request" + the QP workbook's SALES section |
| Buy Plans | Microsoft Planner buy-plan boards + the QP workbook's internal buy-plan sheet |
| Purchase Orders | Teams "TPO Approval Request" + the QP workbook's PURCHASING sections |
| Prepayments | Teams "Prepayment Approval" |

The buy plan is created as part of the sales order and shares its **single manager approval** (already how the backend works — SO verification is stamped by the buy-plan approval). The tabs are views, not separate sign-offs.

The detail pane is a **working editor** at every stage. Sales dials the deal in; the manager can amend before forwarding; every change is audit-logged. **Opening a sales order shows a kanban of the POs filling it** — walk the whole deal at a glance.

**The approvals engine does not change.** No new gates, no two-gate split, no role complexity.

## 2. Roles — deliberately simple

Four roles: **sales, buyer, manager, admin.** Managers/admins can perform any approval and fill in for each other (matches reality: Aniket runs TSO/buy-plan/PO approvals with Mike and Marcus backing up). Eligibility reads the existing per-user flags (`can_approve_buy_plans`, `can_approve_purchase_orders` + limit, `can_approve_prepayments` + limit); dollar limits still gate PO verify and prepayment approval.

**Prepayments:** managers approve in AVAIL; accounting (Myrna, Katy) confirm payment via the existing tokenized pay link — no AVAIL logins required. (Flippable: they can be given accounts + approve rights later via the existing flags.)

## 3. Order types — from TRIO's own approvals log

The TSO flow covers five order types: **New, Revision, Testing Service, Comps Program, Stock Sale/Excess Sale.** The type drives the path:

- **Sourcing types (New; Revision when sourcing changes):** full path — buy plan, lines from offers, PO kanban.
- **Non-sourcing types (Stock Sale, Testing Service, Comps Program — and in-house-stock cases like "no buyplan needed, ship immediately"):** a **lite approval** — the SO record with its fields and QP data, submitted, approved, and tracked, with no buy-plan lines and no kanban.
- **Revision** is the existing edit → resubmit flow made first-class: resubmission carries a reason, and the field-by-field audit-log summary IS the revision record.

Step 0 must find where order type can be stored on the plan/SO. If nothing exists, this is the **one sanctioned additive migration** — stop and confirm with Mike before creating it.

## 4. Quality plan — folded into the SO, exactly as the workbook is structured

The QP dies as a document; every field survives. The live workbooks have a two-section anatomy, and the fold follows it:

- **SALES section → fields on the sales order** (sales fills at draft; optional at submit; manager completes at approval): condition, quantity, FW/HW/REV/date & week codes, product commodity, testing required + option, testing specifics/test location, serial preapproval required, third-party packaging acceptable, packaging requirements, authorized to ship early, authorized to ship partial, routing/prescreening warehouse, acceptable subs / BOM notes, plus customer-approval tracking (customer PO, submitted-to-customer date, customer approved + date).
- **PURCHASING section → fields on each PO line** (buyer fills when cutting the PO; manager reviews at verify): per-PO condition/quantity/date codes, testing, packaging, **traceability verification, counterfeit risk, risk level, CoC available, vendor rating**, will TPO ship complete, TPO notes/shipping schedule, has SN previously been received, serial numbers.

These fields are AS9120B-relevant — none get dropped. The exact columns come from `app/models/quality_plan.py` at Step 0; the workbook is the coverage checklist. **Do not invent fields — map them.** The `qp_sales`/`qp_purchasing` gates never surface in the UI (the code sweep confirmed they're section-review stamps, not approvals). The workbook's internal buy-plan sheet (with BACK-UP OFFERS and RESOURCE REQUIRED columns) is already modeled by AVAIL's offer universe and re-sourcing pool.

## 5. Layout

Four tabs: **Sales Orders · Buy Plans · Purchase Orders · Prepayments.** Split view on every tab:

- **LEFT** — the work list: live-work default (draft/pending/active), completed/cancelled behind a filter, search (customer code, SO#, part number), Mine/All toggle, **age shown on every row, oldest first in decision queues**.
- **RIGHT** — detail pane with full context, inline editing, and the action at the bottom. On sourcing orders, the centerpiece is the PO kanban.
- HTMX in-place updates. No modals, no navigation to edit or decide. Mobile (<900px): panes stack.
- **Acctivate numbers are always visible and one-tap copyable:** every SO# and PO#, wherever it appears — pane headers, kanban cards, queue rows, prepayment cards — renders as a copy chip (tap → clipboard + a brief "Copied" flash). These numbers are the bridge to Acctivate; nobody retypes one.
- **PO decision vocabulary:** the UI says **Approve / Approved / Pending approval** everywhere users see it (matching the Teams "TPO Approval" language the team already uses). Backend names stay `verify_po` / `pending_verify` / `verified` — display mapping only, never a code rename.
- Tab badges = items waiting on the viewer.

## 6. The SO hub — PO kanban (sourcing orders)

Columns, mapped to real line lifecycle: **Awaiting PO → Pending approval → Paid · awaiting delivery → Approved → Received** (UI labels; backend statuses remain `pending_verify`/`verified`), plus a **Re-sourcing** lane (the existing `resourcing` claim pool).

- **Paid · awaiting delivery** is the risk lane: a line whose prepayment is PAID but goods haven't arrived lands here **regardless of verify state** (placement precedence: paid-and-not-received outranks verified). It covers **any advance method — wire, PayPal, credit card, ACH** — money out before goods is the same fire regardless of rail; COD lines never enter it. The card shows the **amount and payee** on its face and ages green → amber (3d) → red (7d).
- Every card carries an age chip. Prepayment state (requested/approved/paid) is a badge on the card, never its own column.
- **Cards move only by the real action** (confirm PO, verify, mark received) — never by drag. The action is the gate.
- Card face: part number, vendor, qty × unit cost, PO# (copy chip), est ship, **payment method chip**, prepayment badge, notes/file count, age, edited-by marker.
- **Received:** TRIO's process already tracks "OPS Received (Y/N)." Step 0 finds the backend receiving event; if none exists, a manual "mark received" action (ops/manager) backs the column. Tapping a card opens that line's inline detail/edit.

## 7. Inline editing — the rules

| Plan status | Who edits fields | Scope |
|---|---|---|
| draft | Owning salesperson (managers too) | Everything: lines, offer-swap vendors, qty/price, SO + QP-sales fields, notes |
| pending | **Manager only** (sales keeps notes) | Manager may amend anything before deciding |
| active+ | Header locked | Line changes via the PO stage; managers per existing line-edit matrix |

**Approve is always a two-part action** — decide *and* choose the handoff, no change-detection:
1. **Notify and proceed** — activates now; buyers get cut-PO tasks; sales gets the change summary.
2. **Send back for sign-off** — reuses the existing reject→draft transition with the summary attached; the manager's edits persist; sales reviews and resubmits.

**The change summary is the audit log, filtered** — every edit already writes ActivityLog (who / field / old → new / when); the summary is those rows since submission, rendered "was X → now Y." Empty if nothing changed. No diff engine.

- **Every reject or send-back carries a note to the fixer.** SO reject, send-back-for-sign-off, PO send-back, prepayment reject — all prompt the decider for a note addressed to the person who must fix it (required on reject — the engine already enforces the comment; optional on send-back, since the auto change-summary always rides along; the PO send-back route gains the note parameter if it lacks one). The note posts into the item's notes thread **tagged with the decision**, the fixer gets an in-app notification, and the full back-and-forth history — submit → rejected with note → fixed → resubmitted → approved — stays on the item permanently, across every cycle.
- **Vendor edits are offer swaps, never free text** (protects the sourcing chain and the prepayment payee snapshot). No matching offer = send back to buyer to source.
- **PO stage:** buyer edits their own line freely before submitting (PO#, est ship, QP-purchasing fields). At verify the manager may edit **anything — including qty, unit cost, PO number, dates** (Mike's call: audit covers it). Guardrails: a one-line "edits here do not change Acctivate" warning, ActivityLog on every edit, a visible "edited by manager" marker.
- **Money/vendor edits are always free** for managers — logged, never friction-gated. (Flippable.)
- **Approve is reachable from both the Sales Orders and Buy Plans tabs** — same object, same single approval; never "the button is on the other tab."
- **Prepayments are not editable** — void / re-request — with one pre-approval exception: the **payment-method dropdown**, adjustable by the approver on the card before deciding, logged like any edit.
- **Plan lifecycle controls on the manager pane:** halt, resume, cancel, reset-to-draft (existing backend actions + permissions). Required for the old hub to retire.
- **Stale-edit guard:** every edit endpoint carries `updated_at`; stale writes are rejected with "this changed — refresh."
- **Notes & attachments — on every item.** The sales order, each PO line, and each prepayment carry their own **notes thread and file attachments** (customer POs, quotes, CoCs, test reports, wire confirmations, photos). Anyone involved can add either at any stage — field locks never lock notes or files. Counts show on cards and rows; uploads and removals are ActivityLog'd (remove = uploader or manager). One shared mechanism: Step 0 hunts for any existing attachment/upload model to reuse (the engine's polymorphic subject pattern is the template); notes reuse ActivityLog manual notes if the pattern fits; if either needs something new, stop and propose.

## 8. The four tabs

**Sales Orders** — create with **order type**; the draft is a working editor including the QP-sales fields; Revision resubmits carry a reason; non-sourcing types take the lite path (no lines, no kanban). **Every SO pane follows one anatomy, top to bottom:** header → **approval block** (a clear "Awaiting your approval" banner with Approve/Reject when pending; an "Approved by X · date" stamp after) → **Quality — sales section** (always present on the SO, collapsible once complete) → lines → PO kanban (active sourcing orders) → notes. The left list groups **"Needs your approval"** first for eligible approvers, and the tab default-selects the oldest pending one — opening the tab lands the manager on a decision, never a hunt. Reuse the existing "New sales order" picker (`sales_order_new` / `create_sales_order_from_offers`). Track via `buy_plan_tracking_rows` + customer code, SO#, type, status, value, age.

**Buy Plans** — the Planner replacement: manager approval queue (existing `decide()` → `_run_approve_side_effects`: PENDING→ACTIVE, SO stamped, per-buyer Cut-PO tasks), editable pane, notify/send-back handoff, `plan_needs_approver_reason` stall warnings, and the same SO kanban.

**Purchase Orders** — buyer: "My assigned lines" (`awaiting_po`) with confirm-PO form (PO# + est ship + **payment method: wire / PayPal / credit card / ACH / COD**, mirroring the terms on the Acctivate PO → `confirm_po`) **plus the QP-purchasing fields**, and the claimable **Re-sourcing pool**. Manager: org-wide `pending_verify` oldest-first (`po_queue`, `can_verify_po_line` + dollar limit), **Approve** (backend `verify_po`) / send back / cancel, edit-anything per §7. Pane shows line amount vs limit and `verify_po_sent` detection (**display only — never auto-verifies**). Repair-service TPOs: deferred (§12).

**Prepayments** — buyer requests against a line with a cut PO (`create_prepayment`; one live request per line; server-side payee snapshot; **includes the test-report-sent toggle** — it's on the current Teams form and already in the model). The prepayment method is a **dropdown — wire, PayPal, credit card, ACH** (the model has the first three; ACH is additive): the buyer picks it at request, and the same dropdown renders on the manager's approval card so the approver can adjust it before deciding (adjustment logged). **The approve button reads "OK to pay"** — the method lives in the field, not the button. **COD lines cannot request a prepayment** — nothing to pay in advance — enforced with a friendly guard. Downstream notices are method-aware ("OK to pay — {method}"), while the existing backend notices stay untouched. Manager approve/reject (`prepay_request_decide`: approve mints the single-use pay token + "OK TO WIRE" to accounting; reject → void + stand-down). AP confirms via `/p/confirm/{token}`. Amount + payee always visible on rows and cards; paid shows wire reference.

## 9. Hard boundaries

- No changes to `services/approvals/*` engine, models, migrations, `/p/confirm`, outbox — with exactly three sanctioned exception paths, all stop-and-confirm-with-Mike first: (a) additive columns for **order type** and/or **line payment method** if Step 0 finds no existing storage; (b) a notes mechanism if nothing existing fits; (c) a small polymorphic **attachments** table + file storage if no reusable upload/document model exists.
- Reuse existing edit endpoints; add thin new ones only where missing. Every edit endpoint: permission guard + status guard + stale-edit guard + ActivityLog write. No silent edits.
- Decisions call existing services untouched. "Send back" reuses reject→draft. No new states, no new gates.
- Four roles only. All existing tests stay green after every phase.

## 10. Build order

All four tabs usable first (display + approve/reject/verify/prepay, incl. the lite path) → the editing layer → the kanban. Small stable steps.

## 11. Defaults chosen (flip any) and explicitly deferred

1. Route: rebuild in place at `/v2/approvals`; old Buy Plans hub retires after parity.
2. Non-sourcing order types get the lite approval path (nothing stays in Teams).
3. Prepay: managers approve; Myrna/Katy confirm via pay link, no logins.
4. Kanban aging thresholds: 3d amber, 7d red.
5. QP-sales fields optional at submit; manager completes at approval.
6. Notes reuse ActivityLog manual notes if the pattern fits; sales keeps notes while pending.
7. Payment method is picked by the buyer at confirm-PO (wire / PayPal / credit card / ACH / COD); COD lines are excluded from prepayment and the risk lane.
8. **Deferred, in writing:** repair-service TPOs (handled outside for now); bulk approve/verify actions; buyer sibling-line context beyond a "line N of M · partial-ship yes/no" flag on the card; Myrna/Katy in-app approve rights.
