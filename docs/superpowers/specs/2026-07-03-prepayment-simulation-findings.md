# Prepayment Workflow — End-to-End Simulation Findings (2026-07-03)

Six-lens simulation (buyer / manager / accounting-AP / financial-controls / edge-cases /
UX) of the prepayment-on-PO workflow, 62 raw findings, adversarially synthesized.

## Headline

The T1–T10 plan delivers a **correct happy path** (request on a PO → route to a manager →
approve → email/Teams accounting/AP, with Teams-card field parity) but as a **money
control it is incomplete: it models only the request→approve leg and stops.** The single
biggest gap is the absence of any **post-approval state** (no status/paid/void/reconciled,
no approved_by, no FK back from the ApprovalRequest) — the common root of three real cash
risks: double-payment, prepayments stranded when their PO dies, and a manager unable to
tell whether AP was actually told to wire.

Decision: land the cheap money-safety guards before ship; defer the full lifecycle model
and the vendor banking-rails subsystem to clearly-scoped follow-ups.

## MUST-FIX before ship (folded into the overnight build unless noted)

1. **Double-pay guard** — the duplicate guard filters `status==REQUESTED` only; a second
   prepay on an already-**APPROVED** PO routes and approves again → duplicate wire. Add
   APPROVED to the blocked set. (T2)
2. **Teardown sweep** — PREPAYMENT approvals are never cancelled when a plan is
   cancelled/halted/completed (`_cancel_open_engine_requests_for_plan` filters BUY_PLAN
   only) and `resource_line` only handled one line. Add a shared
   `_cancel_open_prepayment_requests_for_plan` called from cancel/halt, iterate all
   re-sourced lines, and guard `check_completion`. (T9, extended)
3. **Never a blank payee** — snapshot `vendor_name` (a NOT-NULL string on Offer) onto the
   Prepayment (fold a `vendor_name` column into migration 178 while it's open); prefill and
   fall back to it so the approver/AP always see who's being paid. (T1 migration + T3 + T7)
4. **Amount > 0 guard** — reject `total_incl_fees <= 0` (zero/negative silently satisfies
   any approver limit and routes to the lowest tier). (T2)
5. **Plan-status guard** — reject a request when the plan is cancelled/halted/completed (a
   dead plan keeps VERIFIED lines); mirror in `can_request_prepayment`. (T2 + T3)
6. **Error path must not 500** — the JSON `post_prepayment` catches only
   `NoEligibleApproverError`; it will 500 on the new `ValueError` guards. Catch both; the
   HTMX route maps ValueError→400 toast, NoEligibleApprover→amber toast. (T3)
7. **Separation of duties** — ⚠️ **NEEDS YOUR DECISION (deferred).** A
   `can_approve_prepayments` manager can request on their own PO and approve their own wire
   (routing never excludes the requester; decide only checks a pending recipient). For
   **staging testing this is fine** (you test as Michael, sole approver). For **production
   it's a control hole.** Options in the morning: (a) enforce requester≠approver + require a
   second/backup approver as an ops prerequisite; (b) allow self-approval above no limit;
   (c) allow but log/flag self-approvals. Not enforced overnight so your morning test works.
8. **Notification-failure honesty** — the accounting/AP notice is the ONLY channel to the
   non-Avail wire executor and is fire-and-forget; if no admin has a live Graph token it
   logs-and-skips. Surface the send outcome to the approver ("AP notified ✓ / FAILED —
   follow up") and raise a persistent alert when all channels fail. (T5/T6 return + T7)
9. **Currency honesty** — no currency captured (always USD default) and the tab hardcodes
   `$` with `{:,.0f}` (drops cents). v1: render 2 decimals honoring `Prepayment.currency`,
   state the currency on the AP notice, default USD. (T3/T5/T7)
10. **My Queue parity** — the My-Queue one-click `prepay_approve` shows less than the tab
   (no test-report/PO/method/remarks) and suppresses drill-through. Give it the same
   decision-critical fields + a loud warning when `test_report_sent=False` + a Review
   affordance. No lower-fidelity one-click cash approval. (folded into the T7 group)

## FOLD INTO REMAINING TASKS (bounded UX/safety, folded overnight)

- **T3 modal:** show PO#/MPN/plan#·SO# read-only at top (confirm the exact PO before
  authorizing cash); client-side confirm when the amount deviates >~5% from the PO total;
  thread origin/target like `resource_form` so submitting from the PO tab doesn't eject the
  manager out of the hub; `can_request_prepayment` returns False (render a "Prepay
  requested/approved" pill, not the live button) when an open request already exists.
- **T7 tab:** show PO line total + the delta next to the requested "incl. fees" amount;
  render `test_report_sent=False` as a loud amber/danger warning ABOVE Approve; add
  buyer_remarks (truncated + tooltip) and requester_name; make the resolved row
  self-documenting (approved-by + time + amount + PO#); eager-load to keep the no-N+1
  contract; wire the dead "Review →" to the subject_href.
- **T8:** amber "Prepayment pending" / emerald "Prepaid" badge on the PO Approval row and
  the `_detail_lines` status cell so a PO approver isn't blind to a wire on the same line.
- **T5 notices:** visibly distinct "PENDING APPROVAL — DO NOT PAY" vs "APPROVED — OK TO
  WIRE" templates; use the vendor **legal_name** (fallback display_name) as the beneficiary.
- **Wording:** pin the checkbox to "Has the test report been sent to Management? (Y/N)" and
  fix the contradicting model docstring.

## STRONG FOLLOW-UPS (next PRs — NOT overnight; your call in the morning)

1. **Prepayment payment lifecycle** (the root fix): add `status`
   (requested/approved/paid/void) + `approved_by_id/approved_at` + paid fields
   (`paid_at/paid_amount/paid_currency/wire_reference/paid_by`) + a "mark paid / reconciled"
   action (Accounting in Avail, or a tokenized "confirm wire sent" email link since AP is
   non-Avail) + a PAID badge. This is what makes it a real money control. The T2 guard is
   the interim.
2. **Vendor banking record** so AP can truly "execute the wire from the notice" (the spec's
   premise): a `VendorBankAccount` (beneficiary legal name+address, bank, account/IBAN,
   SWIFT/BIC, routing, country/currency, last-verified, change-audited — a BEC/fraud
   surface) rendered as the full beneficiary block on both notices. Today AP still sources
   wire rails out-of-band, so the spec's "AP executes from the notice" is only partially
   delivered until this lands.
3. Void-on-teardown of an APPROVED prepayment (flip to VOIDED + "do NOT wire / claw back"
   notice) — depends on the lifecycle state.
4. Durable notification outbox + retry for the accounting/AP channel (reuse ApprovalOutbox).
5. Requester loop: alert the requester on approve/reject + a "Withdraw request" action.
6. Stand-down notice to AP on reject/withdraw/void; reconsider whether AP gets the
   request-time notice at all vs accounting-only.
7. Vendor proforma/invoice # on the request → wire memo/reference on the notice.
8. "Wire needed by" date → prominent on notices, sorts the manager tab (bank cutoffs).
9. Enrich the prepayment approval ActivityLog with buy_plan/vendor/amount linkage.
10. Require an approver acknowledgement/override reason when approving with
    `test_report_sent=False` (turn the risk flag into a soft-gate).

## OUT OF SCOPE (confirmed)

Requester-chosen approvers; Teams DMs to non-Avail staff; app-token mail sender; standalone
detail page; QuickBooks/bank auto-reconciliation; FX/currency-normalized approval limits.
