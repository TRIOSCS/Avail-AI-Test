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
   (accounting, AP) + one **Teams channel webhook**. Notify on **request** and on
   **approval** (not on reject). Delivery: (a) **email** the group addresses directly, sent
   from a logged-in admin's mailbox (the distribution list auto-updates natively);
   (b) a **Teams channel card** to the configured webhook. (Teams *DMs* to the non-Avail
   accounting/AP staff are impossible via Graph — replaced by the channel card.)
   Best-effort, failure-isolated per channel.
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
  `test_report_sent` (Y/N), `buyer_remarks` (textarea). Fields mirror the Teams card. The
  amount default reuses **`buyplan_workflow._line_amount(line)`** (already consumed by
  `po_queue.py:104`) and stays **`Decimal`** end-to-end (no float rounding on
  `total_incl_fees`, a `Numeric(12,2)`).
- **Service:** `create_prepayment` (`app/services/prepayment_service.py`) gains
  `buy_plan_line_id: int`; validates the line belongs to `buy_plan_id` (400 otherwise) and
  that the line has a cut PO (`po_number` set; status `pending_verify` or `verified`).
  **Duplicate guard (race-safe):** `Prepayment` has no status column and no FK to its
  `ApprovalRequest` (only the reverse polymorphic pair `subject_type='prepayment' /
  subject_id`), so "pending" is derived by querying `approval_requests` (gate
  `PREPAYMENT`, status `REQUESTED`) whose `subject_id` is a `Prepayment` on this line.
  To avoid a two-request race, take `SELECT … FOR UPDATE` on the `buy_plan_line` row at
  the top of the create transaction, then re-check for an open prepayment before inserting;
  a second concurrent request loses the lock and gets the 400 error toast.
- **Permission:** any user who may act on the plan may request — reuse the existing
  ownership gate already inside `create_prepayment` (`get_buyplan_for_user`, 404 on a
  restricted role not owning the parent requisition). The button visibility mirrors it via
  a thin `can_request_prepayment(user, line)` Jinja global (wraps the same ownership check).
- **Trigger UI:** a "Request prepayment" button on each eligible PO line via a shared
  Jinja macro. On the **plan detail** (`_detail_lines.html`) it shows on both
  `pending_verify` and `verified` lines. On the **PO Approval tab** only `pending_verify`
  lines are actionable rows (`build_po_queue_view` sources `_query_po_pending_verify`;
  `verified` lines are history-only `POHistoryRow`s with plain fields), so the button
  appears there only on pending rows — verified-line requests go through the plan detail.
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

Delivery = **email to the two Outlook group addresses + a Teams *channel* card** (per user
decision). Teams *DMs to non-Avail staff are impossible* — the app's `send_teams_dm`
(`teams_notifications.py:84`) needs a recipient `User` ORM object + that user's own
delegated token, and Graph forbids app-only 1:1 chat posting; accounting/AP are not Avail
users. So the Teams half is a channel card via the existing `post_teams_channel_card`
(webhook, no per-user permission). Email is sent from a **logged-in admin's mailbox** using
the delegated-admin-token pattern already in `buyplan_notifications.py:589-614`
(`notify_stock_sale_approved`) — the app has **no** app-token `sendMail` path (all mail is
delegated `/me/sendMail`), so app-only email is out of scope.

- **Config:** three runtime-editable keys in `system_config` (real pattern: `SystemConfig`
  @ `models/config.py:46`, `admin_service.get_config_value/set_config_value`,
  `PUT /api/admin/config/{key}` @ `admin/system.py:134`) — `accounting_group_email`,
  `ap_group_email`, `prepayment_teams_webhook`. **Registered** in `SYSTEM_SETTINGS_META` +
  rendered in `templates/htmx/partials/settings/system.html` with seeded empty defaults so
  they appear in the admin Settings section (admin-only). Any empty key ⇒ that channel is
  skipped (logged once, graceful).
- **New module** `app/services/prepayment_notifications.py`, mirroring
  `buyplan_notifications.py`'s best-effort, failure-isolated pattern — but with its **own
  `prepayment_id`-keyed background runner** (`run_notify_bg` hardcodes
  `bg_db.get(BuyPlan, plan_id)` @ `buyplan_notifications.py:37-53` and cannot re-derive a
  `Prepayment`; copy the pattern, don't reuse the function):
  - `notify_prepayment_requested(prepayment_id)` and
    `notify_prepayment_approved(prepayment_id)`.
  - **Email:** send to `accounting_group_email` + `ap_group_email` via the delegated-admin
    token (find an admin with a valid Graph token, as `notify_stock_sale_approved` does;
    if none, log + skip email). Body: vendor, PO#/plan/SO#, amount incl. fees, payment
    method, test-report flag, buyer remarks, requester, and (approved variant) approver +
    timestamp. The DL delivers to current members — auto-updates natively.
  - **Teams:** `post_teams_channel_card(prepayment_teams_webhook, card)` with the same
    details. No per-user permission, no membership resolution.
- **Wiring:** `notify_prepayment_requested` fires from the create path; `..._approved`
  fires from the approve branch of `prepay_request_decide`. Both dispatched fire-and-forget
  via the new runner so a Graph/webhook outage never blocks the request/approval. Fires on
  request + approval only (NOT reject).

### 5. PO Approval tab → SO/plan connection

- **No view-model change** — `POPendingRow` already carries the full ORM `plan`
  (`po_queue.py:47,100`), so the template reads `row.plan.sales_order_number` and links
  `row.plan.id` directly.
- `_tab_po_approval.html` — wrap the identity cell in a link to the parent buy plan
  (`/v2/partials/buy-plans/{plan_id}`, push `/v2/buy-plans/{plan_id}`) and render the SO#
  in the sub-line. The tab already tracks pending + recently-resolved.

### 6. Cancel/re-source cancels a dangling prepayment approval

A pending PREPAYMENT `ApprovalRequest` whose PO line is later cancelled or re-sourced would
otherwise keep routing/showing "approve this prepay" for a PO that no longer exists — a real
money risk (`ondelete SET NULL` rarely fires because lines are status-changed, not deleted).
So `resource_line` (`buyplan_workflow.py`), on the cancel/re-source path, must **cancel any
open PREPAYMENT `ApprovalRequest`** tied to a `Prepayment` on that line (look up via
`buy_plan_line_id` → the polymorphic prepayment subject), stamping a resolution note. Tested.

## Data Flow

```
Buyer on a PO line → "Request prepayment" → modal (prefill from PO)
  → POST /v2/prepayments (line_id, vendor, amount, method, test_report, remarks)
  → create_prepayment: persist Prepayment(buy_plan_line_id, buy_plan_id, …)
        + spawn routed PREPAYMENT ApprovalRequest (→ eligible managers)
        + notify_prepayment_requested  → email DLs (admin mailbox) + Teams channel card
  → Prepayment tab (manager): enriched pending row (vendor/PO/SO/amount/test-report/remarks)
  → manager Approve → prepay_request_decide(approve)
        + notify_prepayment_approved   → email DLs (admin mailbox) + Teams channel card
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
  addresses (delegated-admin token, mocked) AND post the Teams channel card
  (`post_teams_channel_card`, mocked); fire on request + approval, NOT on reject; an unset
  key skips that channel; a Graph/webhook failure is isolated and does not raise; the new
  `prepayment_id`-keyed runner re-derives the `Prepayment` correctly.
- **Dangling approval:** cancelling/re-sourcing a line with a pending prepayment cancels its
  PREPAYMENT `ApprovalRequest`.
- **PO tab:** rows link to the parent plan and show SO# (no view-model change).
- **Enriched prepay tab:** row shows vendor/method/amount/test-report/PO/SO.
- Full suite green (run with `SENTRY_DSN=""` — its shutdown flush corrupts xdist teardown);
  `pre-commit run --all-files`.

## Deploy

Migration 178 + code ship in the **same batch** (`./deploy.sh --no-commit`; the entrypoint
runs `alembic upgrade head`). Go/no-go on staging data first. Then live-verify: request a
prepayment on a PO, confirm the enriched manager row, approve, confirm the connection
renders. No new Graph consent is required (email = admin delegated token, Teams = channel
webhook); see the ops-prerequisite note below for the config keys the admin must set.

## Deploy prerequisite (ops, not code)

None blocking. Email uses an admin's existing delegated token (no new consent); the Teams
channel card uses a webhook the admin pastes into Settings. A dedicated app-token sender
mailbox (`Mail.Send` application grant) was explicitly deferred (user chose the admin-mailbox
sender). Admin must set the three config keys in Settings for notifications to fire; unset
keys skip that channel silently.

## Out of Scope (YAGNI)

- Requester-chosen approvers / multi-approver "no response" tracking (engine routes to any
  eligible manager; first decides).
- Teams **DMs** to accounting/AP (impossible — non-Avail users; replaced by a channel card).
- App-token / dedicated-mailbox email sender (`Mail.Send` grant) — using the admin-mailbox
  delegated pattern instead.
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
