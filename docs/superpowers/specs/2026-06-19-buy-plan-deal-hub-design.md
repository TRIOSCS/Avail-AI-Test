# Buy Plan Deal Hub — Design Spec (2026-06-19)

## Problem & goal

A concurrent change (PR #385) introduced a top-level **Reporting** nav (pipeline/forecast +
cadence coverage + team performance dashboards) and **demoted Buy Plans** from a primary
nav item to a card inside Reporting + a CRM sub-tab. Buy-plan management is a **primary core
function** — deal/opportunity execution — and must be a first-class, dedicated, role-aware
hub. This redesign:

1. **Removes the Reporting nav** and **restores Buy Plans as a primary nav item**.
2. **Decomposes** Reporting's analytics into small, contextual numbers folded next to the
   activity they describe (no standalone dashboards, never dominating a page).
3. Builds the Buy Plans page into a **role-lens deal hub** serving three roles at their real
   altitudes: **sales watch their deals**, **buyers cut & confirm POs fast**, **managers
   supervise**. Ops verification (the SO/PO gate) gets a home in the manager lens.
4. Re-tiers buy-plan **notifications** into an *urgent* channel (new assignments + kickbacks)
   and a *routine* channel (progress FYIs).

This reuses the intact buy-plan data model + workflow (`buyplan_workflow.py`,
`buyplan_notifications.py`, the 651-line detail view) — the work is the **view layer**,
notification tiering, nav, and the contextual-metric fold.

## Locked design decisions (from brainstorming)

- **Hybrid role-lens hub**, one page, opens in the user's role lens, switchable:
  `[ My Deals · My Orders · Supervise ]`.
- **My Deals (sales)** = a **compact stage board** of *their* deals (columns Draft → Pending
  → Active/PO → Done); each card shows value · margin · the one current blocker · projected
  ship/close · a PO-progress meter (e.g. 3/5 cut). Cards needing *sales* action
  (rejected → resubmit, missing SO#) pin to the top. Click → the existing detail view.
- **My Orders (buyer)** = a **per-line PO queue** across all the buyer's deals (NOT a board —
  buyers act on lines). **One PO per item**: each row has everything to cut the PO (MPN, qty,
  vendor + contact, unit cost, deal/customer) and an **inline `PO# + ship-date + Confirm`**.
  Confirming flips the line to *awaiting verification* and drops it off the queue. Rows are
  **urgency-sorted** (need-by / age; past-SLA flagged). A buyer may **group/sort by vendor**
  for organization. Inline **Flag issue** (sold-out / price-changed) routes to the manager.
  *(Fast-follow: a "waiting on vendor" + ETA note — deferred since it needs a migration.)*
- **Supervise (managers)** = the **all-deals stage board** + a **slim metric strip** (open
  value, avg margin, approval-queue count, bottleneck counts: halted · overdue-PO · flagged)
  + a **"needs attention" triage** worklist with **inline approve/reject** and **issue
  resolution** (swap offer / re-assign / cancel line). **Ops verification** (SO + PO) surfaces
  here for verification-group members. Plus team-workload (per-buyer open lines, per-rep
  deal value) and a **"ready to fulfill"** signal on deals whose POs are all verified.
- **Notifications — two tiers:**
  - **URGENT** (email + Teams + in-app highlight + nav badge; item floats red to the top of
    the right lens): a **new buyer assignment** (plan activated → your line awaits its PO), an
    **SO kicked back** → salesperson, a **PO kicked back** → buyer, a **new approval needed** →
    manager, a **new verification needed** → ops. Unifying rule: *a new action just landed on
    you.*
  - **ROUTINE** (in-app only, no email/Teams): progress FYIs — your PO was verified, plan
    approved, deal completed. Shown in-app + on the deal card; no email/Teams blast.
- **Nav badge:** the cross-app alert system's buy-plan source (currently mis-pointed at the
  dead "reporting" tab key) re-points to the **buy-plans** nav; role-scoped count = buyer's
  POs-to-cut / manager's approvals / sales's resubmits, with the in-tab spotlight on the rows
  needing you.
- **Reporting analytics decomposition (compact, contextual, non-dominating):**
  - **Pipeline/forecast** (open requisitions weighted) → a slim strip on **Sales Hub**
    (open / weighted / won-this-period), and the by-owner figure on the user's own view.
  - **Cadence coverage** (account health by tier/rep) → a compact figure on the **CRM**
    account list (coverage %), near the existing cadence dots.
  - **Team performance** → stays in the existing **CRM** performance tab (already there).
  - Keep `forecast_service` + `reporting_service` (the computations); only change *where/how*
    they render (small chips, not a `/v2/reporting` dashboard). Remove the Reporting nav +
    page + `reporting/` dashboard templates.

## Architecture

### Routes (extend `app/routers/htmx_views.py`, buy-plan section)
- `GET /v2/buy-plans` (full page) + `GET /v2/partials/buy-plans` (the hub partial) — now
  renders the **role-lens hub**. Query param `lens` ∈ {deals, orders, supervise}; default
  resolved from the user's role (sales→deals, buyer→orders, manager/admin→supervise; ops-group
  member→supervise). Existing filters (status, search) live within the deals/supervise lenses.
- `GET /v2/partials/buy-plans/orders` — the buyer line-queue partial (lines where
  `buyer_id == me` and status ∈ {awaiting_po, (kicked-back) awaiting_po}, urgency-sorted),
  reusing the existing per-line confirm/issue POST routes.
- `GET /v2/partials/buy-plans/board` — the stage-board partial (deals grouped by stage),
  scoped to mine (deals lens) or all (supervise lens).
- Existing detail + all action routes (submit/approve/verify-so/confirm-po/verify-po/issue/
  cancel/reset) are **unchanged** and remain the click-through + action surface.

### Templates (`app/templates/htmx/partials/buy_plans/`)
- `hub.html` — the lens shell (lens switcher + slim metric strip) that includes one of:
  `_board.html` (deals/supervise), `_orders_queue.html` (buyer), `_supervise_extras.html`
  (triage + approvals + ops-verify, included in supervise).
- Reuse `detail.html` (the rich plan view) verbatim as the card click-through.
- A shared `_metric_strip.html` macro (compact, one line) for the per-lens numbers.
- Decompose `reporting/dashboard.html|pipeline.html|coverage.html` → delete the dashboard;
  fold their figures via small macros into Sales Hub + CRM list templates.

### Services
- `app/services/buyplan_hub.py` (new) — the read models for the three lenses:
  `deals_board(db, user, *, scope)`, `buyer_line_queue(db, user)`, `supervise_overview(db)`
  (metric strip + triage buckets). Thin queries over existing models; no business logic
  duplication (transitions stay in `buyplan_workflow.py`).
- `app/services/buyplan_notifications.py` — re-tier: introduce an explicit `urgent` vs
  `routine` dispatch; route new-assignment + all kickback events through urgent (email +
  Teams + in-app), demote progress FYIs to in-app only. (The channels already exist; this
  formalizes the tier per event.)
- `app/services/alerts/sources/buyplan.py` — change the registered tab key back to
  `buy-plans` (from `reporting`); update nav badge wiring + the `markers_for_tab` call in the
  buy-plans route to `buy-plans`.
- Keep `forecast_service` + `reporting_service`; add compact surface points (Sales Hub / CRM).

### Nav (`app/templates/htmx/partials/shared/mobile_nav.html`)
- Replace the `reporting` nav item with `buy-plans` (label "Buy Plans", icon = the shopping
  cart used before). Update the `urlToNav` map: `/v2/buy-plans → 'buy-plans'`; remove the
  `/v2/reporting` + the `/v2/buy-plans → 'reporting'` entries.
- The alert badge `{% elif id in (... 'buy-plans' ...) %}` (replace `'reporting'`).

### Data model
- **No new tables and NO migration for v1.** The hub, the notification re-tiering, and the
  reporting-fold all reuse existing columns. (`main` is churning migrations hard right now —
  three forced re-chains last merge — so a schema-free feature merges cleanly.)
- **Deferred fast-follow** (the only migration-requiring refinement): the buyer **"waiting on
  vendor" + ETA** note (`buy_plan_lines.vendor_wait_note` Text + `vendor_eta` UTCDateTime).
  Ships as a separate 2-column migration after this lands, to keep v1 schema-free.

## Role → action → state (unchanged workflow, re-surfaced)
The hub does not change the state machine. It re-presents it per role:
- **Sales:** submit (SO#), resubmit after kickback. Sees own deals (board).
- **Buyer:** confirm PO (per line), flag issue. Sees own lines (queue).
- **Manager:** approve/reject (inline), resolve flagged issues, cancel/reset. Sees all (board + triage).
- **Ops (verification group):** verify/reject SO, verify/reject PO. Surfaced in Supervise.

## Live updates
The hub partials refresh in place (htmx `every Ns` poll on the active lens, like the
existing badge pattern) so a buyer's PO confirm reflects on sales/manager views without a
manual reload. No new realtime infra.

## Testing
- **Unit** (`buyplan_hub` read models): buyer queue scoping (only my awaiting-PO lines,
  urgency order, kicked-back-first), deals board scoping (sales=own, manager=all) + per-deal
  blocker/PO-progress computation, supervise overview counts (approval queue, bottlenecks).
- **Notification tiering:** assignment + kickback events dispatch urgent (email+Teams+in-app);
  progress events dispatch routine (in-app only) — assert channels per event.
- **Routes:** lens resolution by role; the orders/board/supervise partials render; role gating
  on the action routes is unchanged (regression).
- **Nav badge:** the buy-plan source registered under `buy-plans`; badge endpoint + seen +
  spotlight markers keyed to the buy-plans nav (regression of the alert work).
- **Reporting removal:** `/v2/reporting` gone (404/redirect); the folded metric strips render
  on Sales Hub + CRM; `forecast_service`/`reporting_service` still unit-pass.
- **Playwright:** buyer confirms a PO inline → line leaves queue + deal card advances; a
  kicked-back line shows red-on-top; lens switch works. (Browser-verify per the htmx render rule.)

## Phase plan
| Phase | Delivers |
|---|---|
| **0** | This spec; the `buyplan_hub` read-model service + unit tests. (No migration — v1 is schema-free.) |
| **1** | Restore the Buy Plans nav + remove the Reporting nav; re-point the alert badge/source to `buy-plans`; `/v2/buy-plans` renders the lens shell + metric strip. |
| **2** | **My Orders** buyer queue (per-line inline confirm/issue, urgency sort, kicked-back-red-top) + **My Deals** sales board (blocker/ETA/PO-progress, action-pins). |
| **3** | **Supervise** manager lens (metric strip, needs-attention triage with inline approve/reject + issue resolution, ops SO/PO verification). |
| **4** | Notification re-tiering (urgent: assignments + kickbacks via email+Teams+in-app; routine: in-app FYIs). |
| **5** | Reporting analytics decomposition — delete the dashboard, fold compact metric strips into Sales Hub + CRM; remove `/v2/reporting`. |
| **6** | Verify: full suite, `/simplify`, PR-review, browser/live-verify, APP_MAP docs. |

## Non-goals (v1)
- No change to the buy-plan state machine or the build-from-quote logic.
- No kanban free-drag (stage moves stay gated through the workflow actions; the board only visualizes).
- No new realtime infra (htmx polling, as today).
- No bulk/multi-line single-PO (decided: one PO per item).
- The Reporting *computations* are kept; only their presentation moves (no new analytics).
