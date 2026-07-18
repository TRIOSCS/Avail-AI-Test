# Approvals Parity Checklist — /v2/buy-plans hub → Approvals Workspace

Verifiable, surface-by-surface map of every capability of the retired Buy Plans hub
(`/v2/buy-plans`: My Queue + Pipeline lenses) to its home in the Approvals Workspace
(`/v2/approvals`: Sales Orders · Buy Plans · Purchase Orders · Prepayments).
Spec: `specs/approvals-workspace.md` §11.1 ("old Buy Plans hub retires after parity").

**This PR is blocked on Mike's parity sign-off.** Prepare-only; do not merge before
he approves. Legend: ☑ built and verified in the workspace · ☐ needs check / gap.

## How to verify any row

Log in as the stated role, open `/v2/approvals`, pick the tab in the "New home"
column, and confirm the behavior described under "How to verify". Backing tests are
listed where they exist.

## My Queue lens (role-aware "what needs YOU now")

| Old surface | New home | How to verify | Status |
|---|---|---|---|
| Role-aware queue rows: `plan_approve` / `prepay_approve` (`buyplan_hub.my_queue`) | "Needs your approval" group at the top of every workspace list (`_workspace_list.html`), oldest first, oldest auto-selected into the pane | As an approver with a PENDING plan + a REQUESTED prepayment: SO/BP tabs group the plan first; Prepayments tab groups the prepayment first; the pane lands on the oldest decision (`test_approvals_hub_tabs.py`, `test_approvals_workspace_pane.py`) | ☑ |
| `po_verify` rows (pending-verify lines for PO approvers) | Purchase Orders tab — `pending_verify` lines flagged needs-approval where `can_verify_po_line` passes | As a PO approver: PO tab groups verifiable lines first; pane offers Approve / Send back / Cancel (`test_po_tab_workspace.py`) | ☑ |
| `cut_po` / `cut_po_overdue` rows (buyer's own AWAITING_PO lines, SLA split) | Purchase Orders tab — the viewer's assigned `awaiting_po` lines listed live, each with an age chip; pane = the confirm-PO form | As the assigned buyer: PO tab lists your awaiting lines with ages; confirm-PO works from the pane. NOTE: the explicit red "Overdue" SLA split (buyer-nudge clock) is not reproduced — age chips (amber 3d / red 7d) carry the aging signal instead | ☑ |
| `claim` rows (RESOURCING pool) | Purchase Orders tab — claimable re-sourcing pool rows; pane claim button (also on the SO-pane kanban resourcing lane) | As a PO-cutter with a pooled line: PO tab shows it; claim from the pane succeeds (`test_po_tab_workspace.py`, kanban: `test_kanban_render.py`) | ☑ |
| `flagged` rows (ISSUE lines, supervisor triage) | Purchase Orders tab — ISSUE lines listed live with the Issue badge; resolution actions on the pane | As a manager with a flagged line: PO tab lists it; pane shows the issue state | ☑ |
| `halted` rows (P1 risk) | SO/BP tabs — HALTED is a live status in the work list (rose badge); lifecycle controls (resume) on the manager pane | Halt a plan; SO/BP lists still show it, badge "Halted"; manager pane offers Resume (`test_approvals_workspace_pane.py`) | ☑ |
| `plan_draft` / `plan_returned` rows (own drafts, returned split) | SO/BP tabs — DRAFT plans in the live list; the pane's approval block shows the sent-back state + note-to-fixer thread; Mine toggle scopes to your own | As a rep with a fresh draft and a sent-back draft: both appear under Mine; the sent-back one carries the decision-tagged note (`test_change_summary.py`). NOTE: there is no distinct "Returned" pill on the row itself — the pane carries the story | ☑ |
| `no_approver` rows (config-stall signal) | BP tab row-level amber "No approver configured — stalled" warning (`plan_needs_approver_reason`) + the same warning on the pane | Remove all approver rights, submit a plan: its BP-tab row and pane show the stall warning (`test_approvals_hub_tabs.py`) | ☑ |
| Queue header: N items · $ in play · % avg margin | Consciously dropped (see Margin metrics below) | — | ☐ dropped |
| One-click inline actions on queue rows (approve / verify / claim without opening detail) | Workspace panes — every action is one row-click away (row auto-select + pane action at the bottom); decide re-renders the pane in place + refreshes the list | Approve a plan from the SO tab: pane refreshes, row leaves the needs-approval group (`awListRefresh`) | ☑ |

## Pipeline lens (4-stage deal board)

| Old surface | New home | How to verify | Status |
|---|---|---|---|
| Build / Approve / Purchase / Halted lanes (deal cards by plan status) | SO/BP work lists with per-status badges (Draft / Pending / Active / Inbound / Halted) + search + Mine/All; the per-deal "who has the ball" now reads from the pane's approval block + PO kanban | Create plans in each status; SO/BP list shows each with the right badge; the pane tells the stage story | ☑ |
| Deal card (customer, value, margin badge, SO#, PO#s, primary MPN, blocker text, PO-progress bar, 4-pip stepper) | List rows (title=customer, amount, SO# copy chip, order-type badge, status badge, age) + SO pane header (SO# copy chip, Rev $ · margin %, type badge) + per-line PO kanban | Open a plan from the SO tab: header carries SO#/value/margin; kanban shows per-line PO progress. NOTE: the blocker one-liner ("N POs to cut") and the pip stepper are not reproduced as such — the kanban lanes ARE the progress view | ☑ |
| Metric strip (N open deals · $ open value · % avg margin) | Consciously dropped (see Margin metrics below) | — | ☐ dropped |
| Done archive (completed deals, newest-first) + lazy "Load older" | SO/BP lists' **Closed** filter (COMPLETED + CANCELLED), searchable; no pagination (full closed list renders) | Complete a plan; toggle Closed on the SO tab: it appears with the Completed badge (`test_approvals_hub_tabs.py`). NOTE: lazy "load older" paging dropped — Closed renders the whole filtered list; search covers the long tail | ☑ |
| Mine / All scope toggle (role-resolved; sales locked to mine) | Workspace list Mine/All toggle on every tab | Toggle Mine as a rep: only own items remain | ☑ |
| Kanban-style per-line PO flow | SO pane kanban (`_pane_kanban.html`, `kanban_lanes.py`): Awaiting PO / Pending approval / Paid awaiting delivery / Approved / Received / Re-sourcing, aging chips, claim + mark-received | Active sourcing order: pane kanban shows lines in lanes; mark received works (`test_kanban_lanes.py`, `test_kanban_render.py`, `test_mark_received.py`) | ☑ |

## Origination, lifecycle, deep links

| Old surface | New home | How to verify | Status |
|---|---|---|---|
| "New Buy Plan" button (hub shell) / `?new=1` entry | "New sales order" button on the SO/BP workspace lists → the same `sales_order_new` picker (order type → requisition → builder), now hosted in `#main-content` with its own swap container | Click New sales order on the SO tab: picker loads; Cancel returns to the workspace; create swaps in the plan detail (`test_sales_order_origination.py`) | ☑ |
| Plan lifecycle controls (halt / resume / cancel / reset) | Manager SO pane lifecycle controls (workspace 2.5) + the staying plan-detail partial; same POST routes | As a manager on the pane: halt with reason, resume, cancel, reset all work (`test_approvals_workspace_pane.py`) | ☑ |
| Stall warnings (`no_approver_reason`) | BP-tab rows + SO pane + plan detail partial | See `no_approver` row above | ☑ |
| Deep link `/v2/buy-plans` (and `?lens=`) | 308 → `/v2/approvals?tab=buy-plans`; the `lens` query is dropped (no workspace equivalent of a lens key) | `curl -I /v2/buy-plans` → 308, Location `/v2/approvals?tab=buy-plans` (`test_htmx_views_nightly30.py`) | ☑ |
| Deep link `/v2/buy-plans/{id}` | 308 → `/v2/approvals?tab=buy-plans`. **GAP:** the workspace list has no `?select=` preselection — a plan deep link lands on the tab, not the plan; the pane default-selects the oldest needs-approval row instead. Templates that push `/v2/buy-plans/{id}` after loading the detail partial (requisition/customer buy-plan tabs, QP detail) still work in-session; only a reload/bookmark loses the specific plan | Reload a pushed `/v2/buy-plans/{id}` URL → workspace BP tab (`test_htmx_views_nightly30.py`) | ☐ gap (accepted: tab-level redirect only; list preselection is a follow-up) |
| Hub partial URLs (`/v2/partials/buy-plans`, `/{tab}`, `/pipeline-archive`) | 308 → workspace partials (`/v2/partials/approvals?tab=buy-plans`, `/v2/partials/approvals/buy-plans`, `/v2/partials/approvals/buy-plans/list?show_closed=true`); `?new=1` → 308 to the origination picker partial | `test_buy_plans_router_gaps.py` redirect assertions | ☑ |
| Approvals nav item + alert badge | Nav already points at `/v2/approvals` (internal id `buy-plans` keeps the alert-badge + active-highlight wiring; `/v2/buy-plans*` URLs still highlight it via `urlToNav` so redirected/pushed URLs stay correct) | Nav badge polls `/v2/partials/alerts/buy-plans/badge` (unchanged route) | ☑ |

## Margin metrics — consciously dropped (rationale)

`buyplan_hub.open_avg_margin` (the org-wide open-book average margin on the My Queue
header and Pipeline metric strip) has **no workspace equivalent and is dropped with
this retirement**. Rationale: the workspace is a decision surface, not a reporting
surface — per-deal margin still shows where a decision needs it (SO pane header
`Rev $X · N%`, and margin drives nothing else in the approve flow). An org-wide
margin/open-value strip belongs to a reporting view (Dashboard/MVP scope), not the
approvals queue. If Mike wants it back, it is one cheap aggregate + one line on the
workspace shell — flag it in the parity review.

## Deleted vs kept

- **Deleted templates** (grep-proven unreferenced): `buy_plans/hub.html`,
  `approvals/_surface_my_queue.html`, `approvals/_surface_pipeline.html`,
  `approvals/_pipeline_archive_rows.html`, `approvals/_pipeline_macros.html`
  (only the two deleted Pipeline surfaces imported it).
- **Deleted service code**: `buyplan_hub.py` hub read models (`my_queue`, `QueueRow`,
  `deals_board`, `completed_archive`, `open_avg_margin`, `supervise_overview`,
  `buyer_line_queue`, `team_line_queue`, `resourcing_pool_queue`,
  `_query_stuck_no_approver_plans` and their private helpers) + their tests.
- **Kept in `buyplan_hub.py`**: `_customer_name`, `_age_hours`, `_line_mpn`,
  `_query_po_pending_verify` (+ `_LINE_PLAN_LOADS`) — the workspace PO queue
  (`services/approvals/po_queue.py`) imports them.
- **Kept routes** (the workspace calls them): origination
  (`/v2/partials/buy-plans/sales-orders/new|create`), plan detail
  (`/v2/partials/buy-plans/{id:int}`), and ALL lifecycle/edit POSTs (submit, approve,
  halt, resume, cancel, reset, so-number, lines add/edit/remove/bulk, confirm-po,
  verify-po, claim, resource, receive, prepay decide).
