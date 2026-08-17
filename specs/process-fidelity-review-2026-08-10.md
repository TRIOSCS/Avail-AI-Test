# Process Fidelity Review — AVAIL vs. the real TRIO brokerage process

Date: 2026-08-10. Scope: how faithfully AVAIL models the business end-to-end.
All findings below are HIGH severity and CONFIRMED against code (file:line cited,
or "no path exists" where the gap is the finding). Nothing here has been fixed.

---

## 1. Executive read

AVAIL models the **middle** of every process well and the **endings** badly.
The sourcing happy path — requisition → quote → buy plan → manager approval →
PO confirm → verify → complete — is faithful for a standard "New" order. But
step off that path in any of the ways real brokerage deals routinely do, and
the deal either strands in a state it can never leave or silently loses the
record the business needs.

**Five through-lines explain almost every finding:**

1. **Close-outs are missing, not broken.** Testing Service and Comps orders
   can never complete; a halted-then-resumed plan can never complete; a resell
   list that doesn't sell 100% of its lines can never resolve; a quote marked
   Won leaves its requisition open forever. The flows were built to the last
   approval, not to the last state.
2. **Teardown logic exists but isn't wired to every trigger.** The prepayment
   sweep fires on cancel/halt/complete/re-source but not on PO send-back —
   the one transition that says "this PO was wrong." The WITHDRAWN line status
   is honored by resell award logic but no code ever sets it.
3. **Metrics are computed into a void.** Buyer leaderboard and vendor
   scorecards run every 12h into tables no screen reads; team win rate is
   computed and then dropped by the only template that receives it;
   `won_revenue` is written only by an endpoint no UI calls; the vendor
   win-rate the UI *does* show has no production writer at all.
4. **The one-screen workspace retired old pages without inheriting their
   duties.** The workspace quote-result path skips the requisition close the
   JSON twin performs; the SO picker (the only door to the lite order path)
   hides a requisition once it's won.
5. **Resell is modeled to "bid sent," then stops.** No execution handoff to
   ERP references, no manager visibility, no way to resolve partial outcomes —
   the command-center pattern the sourcing side has was not carried over.

**Fidelity scorecard:** sourcing "New" order chain — faithful. Order-type
completion coverage — 2 of 5 (New, Stock Sale). Resell — faithful through
bid-out, then a dead end. Reporting parity with Salesforce — near zero.

---

## 2. Findings by area

### A. Sourcing & approvals

**TRUE DEAD-ENDS (work strands — a real deal gets stuck):**

- **A1. Testing Service / Comps orders can never complete.**
  `check_completion` early-returns on zero-line plans
  (app/services/buyplan_workflow/buyplan_approval.py:569-570); the
  auto-complete job covers only `is_stock_sale` plans
  (app/jobs/inventory_jobs.py:90-91); the startup sweep excludes zero-line
  plans (app/startup.py:1596-1604); no manual "mark complete" route exists
  anywhere. `create_lite_sales_order`'s own docstring admits it
  (app/services/buyplan_builder.py:227-230).
  *Scenario:* a finished testing engagement sits ACTIVE in the workspace
  forever; the only exits are Cancel (records a delivered deal as CANCELLED)
  or Halt. Completed-deal counts for these order types read zero
  (buyer_leaderboard.py:70, avail_score_service.py:154).

- **A2. A halted-then-resumed plan can never complete.**
  `halt_plan` stamps `so_status=REJECTED` (buyplan_approval.py:474);
  `resume_plan` restores only `plan.status=ACTIVE` (buyplan_approval.py:882)
  and never restores `so_status`; `check_completion` requires
  `so_status==APPROVED` (buyplan_approval.py:575). The only APPROVED writer
  (line 173) is guarded to PENDING plans — unreachable after resume.
  *Bonus wedge:* HALTABLE_STATUSES includes PENDING (line 423), so
  halt-then-resume of a never-approved plan lands it ACTIVE with the manager
  approval never granted and buyer tasks never generated.
  *Scenario:* ops halts for a wrong ship-to, manager fixes and resumes, buyers
  verify every PO — plan clogs the active lens forever; only exits are
  cancelling a shipped deal or a full reset + re-approval.

- **A3. A line whose approved prepayment was voided can never get another.**
  `create_prepayment`'s duplicate guard keys on ApprovalRequest.status IN
  (REQUESTED, APPROVED) (app/services/prepayment_service.py:177-189), but the
  teardown sweep voids the Prepayment while leaving its request APPROVED
  forever (buyplan_approval.py:312, 325-336) — no code path ever moves it.
  The UI deliberately re-shows the Request button after a void
  (prepayment_service.py:46-49, _macros.html:96) and the click 400s forever.
  *Scenario:* halt voids an unwired prepayment; resume; vendor still demands
  the wire — AVAIL refuses, so the wire happens outside AVAIL with no
  approval record. Also blocks deposit + balance split payments.

**BROKEN CONTROL (money chain):**

- **A4. PO send-back leaves the wire authorization alive.**
  `verify_po`'s reject branch resets the line to AWAITING_PO and clears
  `po_number` but never voids the line's prepayment
  (app/services/buyplan_workflow/buyplan_po.py:233-244). The sweep
  (`_cancel_open_prepayment_requests_for_plan`, buyplan_approval.py:256) is
  called only from halt/complete/cancel/re-source — PO send-back is absent.
  A REQUESTED prepayment stays approvable (approvals/service.py:132-283, no
  subject-state re-check); an APPROVED one keeps its live pay token
  (prepayment_confirm.py:103-142) — accounting can confirm a wire against a
  PO that no longer exists. `flag_line_issue` (buyplan_lines.py:253) has the
  same hole. And after re-confirming a corrected PO, the guard in A3 blocks
  the fresh, correct-amount request.

### B. Quote → deal

**TRUE DEAD-END:**

- **B1. Workspace "Mark Won/Lost" ends the quote, not the deal.**
  `quote_result_htmx` (app/routers/htmx/quotes.py:564-585) never touches the
  requisition, never sets `quote.won_revenue`, and the buttons
  (templates/htmx/partials/quotes/detail.html:130-147) submit no reason, so
  `result_reason` is stored as ''. The JSON twin (routers/crm/quotes.py:492)
  closes the req but via a raw status write that bypasses
  `requisition_state.transition` (reason required at requisition_state.py:18),
  advances only the anchor req of a combined quote — and has zero UI callers.
  Buy-plan flow closes nothing (zero requisition-status writes in
  buyplan_workflow/*).
  *Scenario:* rep works the one-screen flow, wins, ships — the req sits in
  QUOTED forever; owner win rate (crm_service.py:755-772) never records the
  win; revenue scoring on `won_revenue` (avail_score_service.py:611-620)
  reads $0; every loss carries an empty reason. The win/loss-reason
  discipline TRIO had in Salesforce is silently lost.

**BROKEN / MISSING:**

- **B2. Two opposite quote-revision conventions.** Builder/JSON paths retire
  the OLD quote (rename to -R{n}, status REVISED, canonical number moves to
  the new revision — quote_builder_service.py:520-528, crm/quotes.py:53-71).
  The workspace path does the inverse: the NEW quote gets the -R suffix and
  the old one keeps the canonical number AND its live status
  (htmx/quotes.py:589-648, esp. 601; Revise button has no status gate,
  detail.html:196). Both rows can independently be marked won (double
  won_revenue; revenue_90d in crm_service.py:776-792 double-counts under
  BOTH conventions since it never filters quote status). The customer-facing
  number can point at a stale version; "-R" means opposite things per path.

- **B3. No order-type branch on a won quote.** The only forward action is
  "Build Buy Plan" (detail.html:150-158), which runs the offer-scoring
  sourcing builder — for a Stock Sale / Testing / Comps deal this yields a
  zero-line plan stamped order_type='new' (builder never sets order_type;
  column default at models/buy_plan.py:139) or 400s with no requirements.
  The correct lite path (`create_lite_sales_order`, buyplan_builder.py:208-261)
  is reachable only from the SO picker, hardcodes `quote_id=None` (:242) and
  copies no quoted pricing/terms; the picker hides a won requisition
  (htmx/buy_plans.py:220). Quote→SO money linkage is severed for three of
  the five order types.

### C. Resell

**TRUE DEAD-ENDS:**

- **C1. A partially-sold list can never resolve.** The list flips to AWARDED
  only when EVERY line is AWARDED or WITHDRAWN
  (app/services/excess_service.py:1358-1372) — and `WITHDRAWN` has **no
  writer anywhere in app/** (only reads at :1313, :1340, :1352). Line
  edit/delete are draft-only (:1710-1722). A 20-line list where 12 sell and
  8 are junk — the normal excess deal — strands in bid_out forever.
- **C2. A rejected customer bid strands the list too.** `record_bid_response`
  flips only the CustomerBid (bid_back_service.py:444-470, :464). Both close
  routes 409 outside OPEN/COLLECTING (_CLOSEABLE_LIST_STATUSES,
  excess_service.py:1900, enforced :1924-1925); the nightly sweep skips
  bid_out (:1971, :2025) and actually pushes lists INTO it (:2048). Exhaustive
  status-writer sweep confirms no exit from BID_OUT except a 100% award. The
  bids_out KPI (routers/resell.py:309) inflates monotonically; resell
  win/loss is never recorded at list level.
- Root cause shared by C1/C2: the missing withdraw-line writer plus the
  OPEN/COLLECTING-only close guard. Also mid-window, dead lines keep
  advertising through the Sighting mirror (excess_mirror.py:98) and keep
  collecting offers on stock that no longer exists.

**MISSING CAPABILITIES:**

- **C3. No execution handoff after acceptance.** `record_bid_response` is a
  pure status flip; no ERP document reference fields exist anywhere in resell
  (grep po_number/sales_order/customer_po across models/excess.py,
  routers/resell.py, excess_service.py, bid_back_service.py: nothing); no
  link between the accepted CustomerBid and awarded offers in either
  direction (accept with zero awards works; awarding continues after a
  rejection). Post-accept UI dead-ends at _build_bid.html:208-212. Six
  months later nobody can trace which ERP documents executed which resell
  deal — the command-center/buy-instruction pattern sourcing has was never
  mirrored.
- **C4. Management is blind to resell by construction.** Lens is strictly
  'mine'|'open' (routers/resell.py:429, 493-502);
  `can_see_customer = el.owner_id == user.id` (:362) with no role carve-out;
  managers 403 on Build-Bid and all bid-back routes (_require_owner, :338);
  the stat strip is scoped to the viewer's own lists (:273-277);
  reporting_service.py contains zero resell content. No awarded-dollar,
  acceptance-rate, cycle-time, or cross-trader metric exists anywhere. The
  identity-hiding gate (built to shield sellers from competing offerers)
  also locks out the owner from an entire revenue line.

### D. Reporting / Salesforce parity — see §3.

---

## 3. Owner-reporting gaps (distinct list)

1. **Buyer throughput & vendor performance land in tables nothing reads.**
   compute jobs run every 12h (app/jobs/offers_jobs.py:57-58, 209-237) into
   BuyerLeaderboardSnapshot / VendorMetricsSnapshot (models/performance.py:59,
   :21) — zero readers in any router or template; the surface both docstrings
   name (routers/performance.py) does not exist. Worse:
   `VendorCard.overall_win_rate` (models/vendors.py:96) is displayed and
   sortable (htmx/vendors.py:148,203; _shared_tabs.py:568) but has **no
   production writer** — the vendor win-rate on screen is dead data that can
   mislead vendor selection.
2. **Win rate / won-lost by period has no answer.** `pipeline_summary`
   computes all-time win rate (forecast_service.py:147-194) but its sole
   consumer renders only open_count/open_value/weighted_value
   (parts/workspace.html:17-21); no period parameter; Requisition has no
   closed/won timestamp (models/sourcing.py). Quote.result_at data exists and
   a monthly per-user win rate is already computed — into write-only
   AvailScoreSnapshot (avail_score_service.py:584-607). "What did we win vs
   lose this month" — Salesforce answered it; AVAIL cannot.
3. **No margin / gross-profit rollup.** Margin exists per quote
   (models/quotes.py:41), per plan (models/buy_plan.py:94-96), per approvals
   row (approvals/queue.py:206) and in per-deal case-report text
   (buyplan_reports.py:93-209) — but no aggregate GP by month, rep, or
   customer anywhere; the only cross-deal GP is the proactive-offers-only
   scorecard (proactive_service.py:997-1110). A brokerage runs on GP;
   revenue/cost are already persisted at approval time, so this is pure
   aggregation.
4. **The resell book has no numbers at all** (C4): no awarded dollars, no
   bid acceptance rate, no bid_out aging, no per-trader throughput — and the
   per-viewer stat strip means even participation counts are private.

---

## 4. Top-6 fix/build order (by business impact)

1. **Close the money hole.** In `verify_po`'s reject branch and
   `flag_line_issue`, call `_cancel_open_prepayment_requests_for_plan`
   scoped to the line (exactly as resource_line does, buyplan_lines.py:173);
   re-key `create_prepayment`'s guard on Prepayment lifecycle status, not
   ApprovalRequest rows. Kills the live-wire-against-dead-PO risk (A4) and
   the forever-blocked re-request (A3) in one small PR.
2. **Make a quote win close the deal — and route by order type.** One shared
   quote-result service for both endpoints: transition EVERY contributing
   requisition via `requisition_state.transition` with a required lost-reason
   modal, set `won_revenue`, delete the raw write at crm/quotes.py:492 (B1).
   On won, branch sourcing vs lite: Stock Sale/Testing/Comps call
   `create_lite_sales_order` carrying `quote_id` and the quoted terms (B3).
   Restores pipeline truth, win/loss discipline, and the revenue metric.
3. **One revision convention.** Route `revise_quote_htmx` through the
   builder/JSON convention (old quote → REVISED + -R{n}; canonical number on
   the new revision) (B2). Stops double-won revenue and the untrustworthy
   customer-facing number.
4. **Give every deal an ending.** (a) Manager "Mark complete" for zero-line
   lite plans (route → `_complete_plan`, which already refuses safely when a
   PO gate is open) or extend the auto-complete job past `is_stock_sale`
   (A1). (b) Snapshot/restore `so_status` across halt/resume, return a
   PENDING-halted plan to PENDING on resume, then run `check_completion`
   (A2 + the approval-bypass wedge).
5. **Resell resolution wave.** Owner-only `withdraw_line` (the missing
   WITHDRAWN writer: retire mirror Sighting, recompute rollup, 409 if
   awarded); allow close-without-bid from BID_OUT when no undecided won
   offers; on `record_bid_response(rejected)` prompt-or-auto close (C1/C2).
   Then the accept handoff: ERP-neutral reference fields (po_number on
   CustomerBid, sales_order_number on ExcessOffer) + post-accept next-step
   pane (C3) — the buy-instruction pattern, mirrored.
6. **Minimum owner reporting.** GP rollup over buy plans by month /
   submitted_by / customer (pure aggregation of persisted columns) + surface
   the already-computed period win rate + a manager/admin 'all' lens and
   rollup for resell (C4). Wire `overall_win_rate` to a real writer or drop
   it from the UI; wire or delete the void 12h snapshot jobs (§3.1-3.4).

Items intentionally after the top-6: nothing — all fifteen confirmed
findings are covered by the six items above (A1-A4, B1-B3, C1-C4, D1-D4).
