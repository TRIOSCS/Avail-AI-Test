# Prepayment Requests Tied to POs, with Accounting/AP Notifications — Design

**Date:** 2026-07-03
**Status:** Approved (design); pending spec review → implementation plan
**Area:** Approvals hub (`/v2/approvals`) — Prepayment + PO Approval tabs

## Problem & Goal

Trio currently runs prepayment approvals in the Microsoft Teams *Approvals* app
(reference: card `PP-TP0202781`). A buyer requests to pay **one specific PO** in advance;
a **manager** approves because cash is put at risk; the **accounting manager** and **AP
clerk** must *see* the request and *be notified of the approval* so AP can execute the
wire — but they are not Avail users and must be reached by **email + Teams**, sourced from
the Outlook **accounting** and **AP** groups so the recipient set auto-updates as staff
change (never hardwired to individuals).

In Avail today the prepayment *engine* exists (`Prepayment` model, `create_prepayment`
service, `POST /v2/prepayments`, PREPAYMENT `ApprovalRequest` routing to
`can_approve_prepayments` managers, the decide route, and the Prepayment tab) **but has no
UI entry point** (nothing calls `POST /v2/prepayments`) and the record links to the whole
`buy_plan`, not a specific PO. This design adds the request entry point, ties prepayments
to a specific PO, matches the Teams field set, and adds the accounting/AP notifications.

Also folds in the user's PO-tab ask: each PO row must show a **connection to its parent
sales-order/buy-plan**.

## Confirmed Decisions

1. **Entry point:** "Request prepayment" action **on a specific PO** (a `BuyPlanLine` with
   a cut PO — status `pending_verify` or `verified`), on both the plan detail
   (`_detail_lines.html`) and the PO Approval tab (`_tab_po_approval.html`).
2. **Linkage:** the `Prepayment` record links to the specific PO line **and** its parent
   plan (both).
3. **Approver model:** the **manager is the sole approver in Avail** — unchanged engine
   routing (any eligible `can_approve_prepayments` user whose `prepayment_approval_limit`
   covers the amount; the first to act decides). Accounting + AP are **notify-only, never
   approvers.**
4. **Notification source:** admin configures the two **Outlook group email addresses**
   (accounting, AP). Notify on **request** and on **approval** (not on reject). Delivery:
   (a) **email** the group addresses directly (the distribution list auto-updates
   natively); (b) **Teams DM** each *current* member, resolved live via Microsoft Graph so
   membership auto-updates. Best-effort, failure-isolated per channel.
5. **Amount:** pre-filled from the PO line total, **editable** ("Total incl. fees" can
   differ from the line total via wire fees / currency).
6. **PO Approval tab:** each PO row is clickable to its parent buy plan and shows the SO#.

## Architecture

### 1. Data model — `Prepayment` gains a PO link (migration 178)

`app/models/quality_plan.py::Prepayment` (currently links `buy_plan_id` only):
- **ADD** `buy_plan_line_id = Column(Integer, ForeignKey("buy_plan_lines.id", ondelete="SET NULL"), nullable=True)` + relationship + `Index("ix_prepayment_buy_plan_line", "buy_plan_line_id")`.
- Keep `buy_plan_id` required (carries the plan/SO connection).
- Nullable so existing rows survive; new requests always set it.

**Migration 178** (DDL, reversible, chains onto `177_qp_section_reviewed_cols`): add the
column + index; downgrade drops both. Round-trip upgrade→downgrade→upgrade on a THROWAWAY
Postgres 16 (never staging). Append the claim line to `MIGRATION_NUMBERS_IN_FLIGHT.txt`.
Single head verified.

### 2. Request entry point + form

- **Route:** reuse `POST /v2/prepayments` (`app/routers/prepayments.py`); add a GET partial
  `GET /v2/partials/prepayments/new?line_id={id}` that renders a modal pre-filled from the
  PO line: vendor (from `line.offer.vendor_card`), `total_incl_fees` (default =
  `unit_cost * quantity`, editable), `payment_method` (select: wire / cc / paypal),
  `test_report_sent` (Y/N), `buyer_remarks` (textarea). Fields mirror the Teams card.
- **Service:** `create_prepayment` (`app/services/prepayment_service.py`) gains
  `buy_plan_line_id: int`; validates the line belongs to `buy_plan_id` (400 otherwise) and
  that the line has a cut PO. **Duplicate guard:** refuse a second *pending* prepayment on
  the same line (one in-flight prepayment per PO) — return a 400 error toast.
- **Trigger UI:** a "Request prepayment" button on each eligible PO line via a shared
  Jinja macro, rendered in `_detail_lines.html` and `_tab_po_approval.html`. Gated to users
  who may act on the plan (reuse the existing ownership/`can_resource`-style predicate; a
  new `can_request_prepayment(user, line)` Jinja global if needed).
- On success: existing HX-Trigger toast + re-render; fire the request-time notification
  (§4).

### 3. Manager approval (existing engine, enriched view)

- Routing **unchanged**: `create_prepayment` still spawns the routed PREPAYMENT
  `ApprovalRequest`; `prepay_request_decide` (`app/routers/htmx/buy_plans.py:290`) still
  decides. No change to who approves.
- **Enrich the Prepayment tab** (`_tab_prepayment.html` + its `RowVM` in
  `services/approvals/queue.py`) so the pending row and resolved row show the full request
  like the Teams card: vendor, payment method, amount incl. fees, a "test report sent"
  badge, buyer remarks (truncated), and the **connected PO # / plan / SO#** (clickable to
  the plan detail). No separate detail page required; the enriched row carries it.

### 4. Accounting/AP notification (new module)

- **Config:** two runtime-editable settings — `accounting_group_email`, `ap_group_email` —
  stored in `system_config` (admin-editable in Settings, no redeploy), surfaced in the
  Approvals/Ops settings section (admin-only). Empty ⇒ that channel is skipped (graceful).
- **New module** `app/services/prepayment_notifications.py`, mirroring
  `buyplan_notifications.py`'s best-effort, failure-isolated, `run_notify_bg`
  fire-and-forget pattern (re-derives from `prepayment_id`):
  - `notify_prepayment_requested(prepayment_id)` and
    `notify_prepayment_approved(prepayment_id)`.
  - **Email:** send to `accounting_group_email` + `ap_group_email` via the existing Graph
    app-token mail path (`email_service` / `graph_app_auth`). Body: vendor, PO#/plan/SO#,
    amount incl. fees, payment method, test-report flag, buyer remarks, requester, and
    (for the approved variant) approver + timestamp. The DL delivers to current members —
    auto-updates natively.
  - **Teams DM:** resolve each group's *current* members via Graph
    (`GET /groups?$filter=mail eq '{addr}'` → id → `GET /groups/{id}/members`), then DM
    each via `teams_notifications.py`. Auto-updates because membership is re-resolved each
    call. **Prerequisite:** the app registration needs the Graph `GroupMember.Read.All`
    (application) permission with admin consent — a one-time ops step (documented like the
    datasheet `Sites.Selected` grant). Until granted, the Teams-DM half fails soft (logged)
    and email still fires.
- **Wiring:** `notify_prepayment_requested` fires from the create path; `..._approved`
  fires from the approve branch of `prepay_request_decide`. Both dispatched fire-and-forget
  so a Graph outage never blocks the request/approval.

### 5. PO Approval tab → SO/plan connection

- `services/approvals/po_queue.py::build_po_queue_view` — add `so_number` to each PO row
  (from `plan.sales_order_number`).
- `_tab_po_approval.html` — wrap the identity cell in a link to the parent buy plan
  (`/v2/partials/buy-plans/{plan_id}`, push `/v2/buy-plans/{plan_id}`) and render the SO#
  in the sub-line. The tab already tracks pending + recently-resolved.

## Data Flow

```
Buyer on a PO line → "Request prepayment" → modal (prefill from PO)
  → POST /v2/prepayments (line_id, vendor, amount, method, test_report, remarks)
  → create_prepayment: persist Prepayment(buy_plan_line_id, buy_plan_id, …)
        + spawn routed PREPAYMENT ApprovalRequest (→ eligible managers)
        + run_notify_bg(notify_prepayment_requested)  → email DLs + Teams-DM members
  → Prepayment tab (manager): enriched pending row (vendor/PO/SO/amount/test-report/remarks)
  → manager Approve → prepay_request_decide(approve)
        + run_notify_bg(notify_prepayment_approved)   → email DLs + Teams-DM members
        (AP executes the wire from the email/Teams notice)
```

## Error Handling

- Duplicate pending prepayment on a line → 400 error toast (no second request).
- Line not on the plan / no cut PO → 400.
- No eligible manager at the amount → existing `NoEligibleApproverError` surfaced (amber
  banner / honest error), request not silently dropped.
- Notification failures (email or Teams) are logged and isolated per channel and per
  recipient; they never block or roll back the request/approval (best-effort, matches the
  house pattern).
- Unset group address ⇒ that channel skipped, logged once, not an error.

## Testing

- **Model/migration:** `Prepayment.buy_plan_line_id` present; migration 178 round-trips;
  single head.
- **Service:** `create_prepayment` sets the line, validates line∈plan, rejects a second
  pending prepayment on the same line, still spawns the routed approval.
- **Entry point:** the request modal renders pre-filled from a PO; the "Request prepayment"
  button shows only on eligible lines for permitted users.
- **Notifications:** `notify_prepayment_requested`/`_approved` email the configured group
  addresses AND Teams-DM the resolved members (Graph mocked); fire on request + approval,
  NOT on reject; unset address skips that channel; a Graph failure is isolated and does not
  raise.
- **PO tab:** rows link to the parent plan and show SO#.
- **Enriched prepay tab:** row shows vendor/method/amount/test-report/PO/SO.
- Full suite green (run with `SENTRY_DSN=""` — its shutdown flush corrupts xdist teardown);
  `pre-commit run --all-files`.

## Deploy

Migration 178 + code ship in the **same batch** (`./deploy.sh --no-commit`; the entrypoint
runs `alembic upgrade head`). Go/no-go on staging data first. Then live-verify: request a
prepayment on a PO, confirm the enriched manager row, approve, confirm the connection
renders. The Graph `GroupMember.Read.All` consent is an ops prerequisite for the Teams-DM
channel; email works without it.

## Out of Scope (YAGNI)

- Requester-chosen approvers / multi-approver "no response" tracking (engine routes to any
  eligible manager; first decides).
- A standalone prepayment detail page (the enriched row carries the Teams-card fields).
- Notifying accounting/AP on reject (user chose request + approval only).
- Plan-level prepayment entry (PO-level only).

## Key Anchors (verified against current main)

- `app/models/quality_plan.py:143` — `Prepayment`
- `app/services/prepayment_service.py:28` — `create_prepayment`
- `app/routers/prepayments.py:39` — `POST /v2/prepayments`
- `app/routers/htmx/buy_plans.py:290` — `prepay_request_decide`
- `app/templates/htmx/partials/approvals/_tab_prepayment.html`, `_tab_po_approval.html`
- `app/services/approvals/queue.py` (RowVM), `app/services/approvals/po_queue.py`
- `app/services/buyplan_notifications.py` (notification pattern), `teams_notifications.py`,
  `email_service.py`, `graph_app_auth.py`
- `app/models/auth.py:73` — `can_approve_prepayments`, `prepayment_approval_limit`
