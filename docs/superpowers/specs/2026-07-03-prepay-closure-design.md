# Prepay Closure — Payment Lifecycle — Design

**Date:** 2026-07-03
**Status:** Design (pending spec review → implementation plan)
**Area:** Prepayment feature (extends the shipped prepayment-on-PO workflow)
**Builds on:** `2026-07-03-prepayment-po-workflow-design.md` + `-simulation-findings.md`

## Problem & Goal

The shipped prepayment feature models **request → approve → notify** and then stops. There
is no way to record that the wire actually went out, and (the QA review's biggest residual
money risk) an **approved-but-unwired** prepayment survives a dead plan. "Prepay closure"
adds the payment lifecycle that closes the loop: a prepayment moves `requested → approved →
paid`, or is `void`-ed if killed before payment. Accounting (who actually wires, and is
**not** an Avail user) confirms payment from the approval email itself; the "paid" event
flows back to everyone tracking the deal.

Approvals / separation-of-duties are **explicitly left unchanged** (user directive).

## Confirmed Decisions

1. **Accounting marks paid via a tokenized link in the "OK TO WIRE" email** (they are not
   Avail users). Plus a **manager/admin in-app mark-paid fallback** if the email is lost.
2. **Lifecycle states:** `requested → approved → paid`, plus `void`. No `reconciled` (a
   later QuickBooks/bank-matching program); no refund/claw-back tracking after `paid`.
3. **Paid notification fan-out:** the buyer (requester), the salesperson working the deal
   (plan owner/submitter), and **all Manager-role users** — in-app alerts.
4. **Void-on-teardown:** an `approved` (not `paid`) prepayment whose plan is
   cancelled/halted/completed/re-sourced flips to `void` + a "DO NOT WIRE / claw back"
   stand-down notice to accounting/AP. A `paid` prepayment is never auto-voided.

## Architecture

### 1. Data model — `Prepayment` gains a lifecycle (migration 179)

`app/models/quality_plan.py::Prepayment` + `PrepaymentStatus` StrEnum in `app/constants.py`
(`requested` / `approved` / `paid` / `void`):

- `status` — `String(20)`, default `requested`, indexed.
- `approved_by_id` (FK users SET NULL) + `approved_at` (UTCDateTime) — stamped on approve.
- `pay_token` — `String(64)`, unique, nullable — cryptographically-random
  (`secrets.token_urlsafe(32)`), minted when the prepayment is approved (the "OK TO WIRE"
  email carries the link). Cleared on void/paid so a spent/dead link can't act again.
- Paid: `paid_at` (UTCDateTime), `paid_by_id` (FK users SET NULL, nullable — accounting has
  no User row), `paid_by_label` (`String(120)`, the initials/name accounting types on the
  confirm page, or the Avail user's name for the in-app path), `paid_via`
  (`String(20)`: `accounting_email` | `in_app`), `wire_reference` (`String(120)`),
  `paid_amount` (`Numeric(12,2)`, defaults to `total_incl_fees`, editable at confirm time).
- Void: `voided_at`, `voided_by_id` (FK users SET NULL, nullable), `void_reason` (`String(255)`).

**Migration 179** (chains onto 178): add the columns + index on `status` + unique index on
`pay_token`. **Backfill** `status` from each prepayment's linked PREPAYMENT `ApprovalRequest`
(approved→`approved`, rejected→`void`, requested→`requested`) and `approved_by_id/at` from
the request's resolver where present. Round-trip on a THROWAWAY Postgres; single head.

### 2. Lifecycle transitions (status is the source of truth, synced at each point)

- **create** (`create_prepayment`) → `requested` (default).
- **approve** (`prepay_request_decide` approve branch) → `approved`, stamp
  `approved_by_id/approved_at`, **mint `pay_token`**. The `notify_prepayment_approved`
  ("OK TO WIRE") email now includes the confirm-paid link (§3).
- **reject** (`prepay_request_decide` reject branch) → `void` (`void_reason="rejected by
  approver"`), fire `notify_prepayment_voided` (§4 stand-down).
- **teardown** (`_cancel_open_prepayment_requests_for_plan`, §4) → `void`.
- **mark paid** (§3, email token OR in-app) → `paid`, set the paid fields, clear `pay_token`,
  fire `notify_prepayment_paid` (§5 fan-out).

### 3. Mark-paid — the tokenized email link (+ in-app fallback)

- **Public route** `GET /p/confirm/{token}` and `POST /p/confirm/{token}` in a new
  `app/routers/prepayment_confirm.py` (short public prefix; **no auth**). The token is the
  authorization. **CSRF-exempt** (add the path to `CSRF_EXEMPT_URLS` in `main.py`, like the
  webhooks) and **rate-limited** (`rate_limit.py`).
  - GET → a minimal public confirmation page (own base, no app nav): prepayment summary
    (vendor, amount, currency, PO#/plan/SO), a "Confirm wire sent" button, and optional
    `wire_reference` + `your initials` fields. Idempotent: if the prepayment is already
    `paid` → "Already marked paid on {date} by {label}"; if `void` → "This prepayment was
    voided ({reason}) — do not wire." (No token / unknown token → generic 404 page.)
  - POST → look up by token; only acts when `status == approved`; set `status=paid`,
    `paid_at=now`, `paid_via='accounting_email'`, `paid_by_label` from the form (fallback
    "Accounting"), `wire_reference`, `paid_amount` (default `total_incl_fees`); clear
    `pay_token`; fire `notify_prepayment_paid`. Then render the "recorded — thank you" page.
- **In-app fallback:** a "Mark paid" button on `approved` prepayment rows (Prepayment tab +
  plan detail), gated to a plan owner/buyer or manager/admin, opening a modal capturing
  `wire_reference` + `paid_amount` (prefilled) + date; POST to
  `/v2/partials/prepayments/{id}/mark-paid` → same transition with `paid_via='in_app'`,
  `paid_by_id=user.id`, `paid_by_label=user.name`.
- **Undo (safety):** a manager/admin "Correct — mark unpaid" on a `paid` row reverts to
  `approved` (clears paid fields, re-mints `pay_token`), logging an ActivityLog. A mis-click
  on a money state needs a correction path; the fan-out already makes a bad "paid" visible.

### 4. Void-on-teardown (closes the QA review's biggest residual risk)

`_cancel_open_prepayment_requests_for_plan` (`app/services/buyplan_workflow.py`) today only
cancels `REQUESTED` requests. Extend it: also select the plan's `approved` prepayments
(status), flip each to `void` (`voided_at/by`, `void_reason=<the teardown reason>`), clear
`pay_token`, and fire `notify_prepayment_voided(prepayment_id, reason)` — the "DO NOT WIRE /
claw back" stand-down to accounting/AP. Called from cancel/halt/complete (plan-scope) and
`resource_line` (line-scope, per the QA fix). **`paid` prepayments are never touched.**

### 5. Notifications (reuse `app/services/prepayment_notifications.py`)

- `notify_prepayment_voided(prepayment_id, reason)` — accounting/AP stand-down ("DO NOT WIRE
  — this prepayment was voided: {reason}"), email DLs + Teams channel card, same best-effort
  pattern.
- `notify_prepayment_paid(prepayment_id)` — **in-app alerts** (durable ActivityLog +
  cross-app alert badges, the mechanism `buyplan_notifications` uses) to: the buyer
  (`created_by_id`), the salesperson (`buy_plan.submitted_by_id`, fallback the requisition
  creator), and **all `role == manager` users**. Message: "Prepayment paid — {vendor} {amount}
  wired for PO {po#} (plan #{id})." Deduped recipients.

### 6. UI badges

Extend `prepayment_state_for_lines` (and the tab RowVM) to surface the new states. The
PO-line / tab badge gains **Paid** (emerald, with the wire reference + paid date/by on the
row) and **Void** (neutral/gray, with the reason). The request button stays a pill once a
prepayment exists (unchanged).

## Data Flow

```
approve → status=approved, mint pay_token → "OK TO WIRE" email to accounting/AP w/ confirm link
  accounting wires → clicks link → GET /p/confirm/{token} (public) → confirm → POST
    → status=paid, paid fields set, pay_token cleared
    → notify_prepayment_paid → in-app alerts: buyer + salesperson + all managers
  (fallback: manager/admin "Mark paid" in Avail → same transition, paid_via=in_app)
teardown of an approved prepayment (plan cancel/halt/complete/resource)
    → status=void → notify_prepayment_voided (DO NOT WIRE) → accounting/AP
```

## Error Handling

- Token route: unknown/spent token → 404 page; already paid/void → status page (idempotent,
  never double-fires the paid notice); acts only on `status==approved`. Rate-limited;
  CSRF-exempt (token is auth). No PII beyond what the approval email already contained.
- Mark-paid on a non-`approved` prepayment (in-app) → 400 error toast.
- Notification failures are best-effort/isolated (existing pattern), never block the
  transition or the DB commit.
- Undo-paid only from `paid`, manager/admin only.

## Testing

- Model/migration 179 round-trips; backfill maps existing statuses; single head.
- approve stamps approved_by/at + mints pay_token; the approval email body contains the
  confirm URL.
- Token confirm: happy path marks paid + fires the paid fan-out; idempotent second click
  no-ops; a voided prepayment's token shows the do-not-wire page and cannot be paid; unknown
  token 404; the route is CSRF-exempt + rate-limited.
- In-app mark-paid: permission-gated; captures fields; `paid_via=in_app`. Undo reverts to
  approved (manager only) and re-mints the token.
- Teardown voids an `approved` prepayment + fires `notify_prepayment_voided`; a `paid` one is
  untouched; reject → void + stand-down.
- `notify_prepayment_paid` targets buyer + salesperson + all managers (deduped); Graph mocked.
- Badges render Paid/Void.
- Full suite green (`SENTRY_DSN=""`); `pre-commit --all-files`.

## Deploy

Migration 179 + code same batch (`./deploy.sh --no-commit`). Go/no-go on staging (179 adds
nullable columns + a backfill over few rows — safe). The confirm route is public — verify it
renders + is rate-limited. Live-verify: approve → grab the token → confirm-paid → assert the
fan-out alerts + Paid badge; teardown an approved prepayment → assert void + stand-down.

## Out of Scope (YAGNI)

- `reconciled` state / QuickBooks / bank-statement matching (own program). NOTE: Trio's
  QuickBooks is the **Desktop edition, hosted in Azure** — a reconciliation integration
  would go through the QuickBooks **Web Connector / QBXML SDK** (or a hosted-desktop file
  bridge), NOT the QuickBooks Online REST API (so the session's Intuit *QBO* connector does
  not apply). Factor this into the reconciliation program's design.
- Refund / claw-back tracking after a `paid` wire.
- Emailing (vs in-app alerting) the buyer/salesperson/managers on paid — they're Avail users;
  in-app alerts suffice (email can be a later toggle).
- Changing the approver / separation-of-duties model (explicitly left as-is).

## Key Anchors

- `app/models/quality_plan.py::Prepayment`, `app/constants.py` (new `PrepaymentStatus`)
- `app/services/prepayment_service.py::create_prepayment`
- `app/routers/htmx/buy_plans.py::prepay_request_decide` (approve/reject branches)
- `app/services/buyplan_workflow.py::_cancel_open_prepayment_requests_for_plan` + call sites
- `app/services/prepayment_notifications.py` (add `_voided`/`_paid`)
- `app/main.py` (`CSRF_EXEMPT_URLS`), `app/rate_limit.py`
- `app/services/prepayment_service.py::prepayment_state_for_lines` + `_tab_prepayment.html`
- `app/models/auth.py` (`role == manager` for the fan-out; `submitted_by_id` on BuyPlan)
