# Approvals Rework — Acceptance-Criteria Spec (build-ready)

> **Phase 1 deliverable** of the Approvals Rework program. Converts the locked vision
> (`2026-06-28-approvals-rework-vision.md`, Stages A–F + locked decisions) into a build-ready,
> **zero-TBD** spec with testable acceptance criteria, the data-model deltas, the gate→role routing,
> and the SLA/notification rules. Every later code phase (2–8) writes its TDD sub-plan from THIS
> document. Companion: phased plan `2026-06-28-approvals-rework-phased-plan.md`; UX research
> `../research/2026-06-28-approval-ux-research.md`. Memory: `project_approvals_rework_2026_06_28`.
>
> **Grounded in code** (maps taken from `main`, 2026-06-28). Symbols cited are current as of that
> read; each phase re-verifies its own line numbers before editing (CLAUDE.md linear-development rule).

---

## 1. Resolved decisions (this spec settles every open item)

| # | Decision | Resolution |
|---|----------|------------|
| D1 | Returned-for-correction: restart vs resume | **Resume at the returning gate.** A return re-enters the gate it came from; earlier-cleared gates are not re-run. (Simplified by D5 — see below.) |
| D2 | Delegation / out-of-office | **Manual reassign (already exists) + a simple per-user OOO delegate.** A pending-gate recipient who is OOO routes to their configured delegate. |
| D3 | Reminder / escalation cadence | **Faster (same-day).** Reminder + aging badge at **4 business-hours** pending; **escalate** to the manager tier at **24 wall-clock hours** pending. |
| D4 | Gate→role routing + dollar tiers | **As locked, dedicated rights, no dollar tiers.** New `can_approve_sales_orders` right for Gate 1; existing rights for Gate 2 / PO. Every plan always traverses a human gate (no auto-approve, no dollar skip). |
| D5 | Gate-2 (buy-plan) negative outcome | **None — Gate 2 is edit-and-approve, never reject.** The purchasing manager reshapes the plan to suit the company and approves; that approval releases it to buyers. Their edits are audit-logged and do **not** bounce the plan to Gate 1. |

**Consequence of D5 (deliberate simplification):** because Gate 2 never returns and only the authorized
purchasing manager edits a post-Gate-1 plan, there is **nothing upstream to invalidate**. The
"edit-invalidates-upstream snapshot" machinery considered earlier is **dropped**. The only return paths
are **Gate 1 → salesperson** (correction notes) and a minor **PO gate → buyer** (e.g. wrong PO#); each
return simply re-enters its own gate on resubmit.

---

## 2. Architecture & global constraints

- **Keep the approval engine + state machine** (`app/services/approvals/*`, `app/models/approvals.py`).
  Rework the lifecycle model, the Sales-Order concept, the routing, and the data-entry UX **on top** of it.
- **Server-rendered HTMX + Alpine + Jinja2 — never React/SPA.** Reuse the UI-conformance primitives
  (`.badge`/`status_badge`, `.data-table`/`.compact-table`, `.page-fluid`/`.page-readable`, `.btn`/`.input`,
  the Alpine modal + `x-collapse` patterns) and the `template_response()` partial contract. No new UI conventions.
- **No auto-approve** — remove `_should_auto_approve` (`buyplan_workflow.py:906`) + `buyplan_auto_approve_threshold`
  (`config.py:251`); both call sites (`submit_buy_plan`, `resubmit_buy_plan`) always open Gate 1.
- **Engine-only side effects** — retire the legacy approve path `/v2/partials/buy-plans/{id}/approve`
  (`htmx_views.py:11572`); all decisions flow through `svc_decide` (`approvals/service.py:132`).
- **All schema changes via Alembic** (claim numbers in `MIGRATION_NUMBERS_IN_FLIGHT.txt`; upgrade→downgrade→upgrade
  round-trip on a throwaway PG; single head). startup.py stays runtime-only (no DDL).
- **TDD; SAVE at each increment** (commit+push+memory); deploy via `deploy.sh`; live-verify on real PG.

---

## 3. Data-model deltas (the concrete schema changes)

All enums live in `app/constants.py`; approval models in `app/models/approvals.py`; buy-plan models in
`app/models/buy_plan.py`; user/rights in `app/models/auth.py`.

### 3.1 Enums
- **`ApprovalGateType`** — add `SALES_ORDER = "sales_order"` (Gate 1). Keep `BUY_PLAN` (Gate 2),
  `PURCHASE_ORDER`, `PREPAYMENT`. **`QP_SALES` is left untouched** (separate Quality-Plan flow — NOT repurposed).
- **`ApprovalRequestStatus`** — add `RETURNED = "returned"` (returned-for-correction; distinct from `REJECTED`).
- **`ApprovalRecipientStatus`** — add `RETURNED = "returned"` (the recipient who returned it).
- **`BuyPlanStatus`** — add `RETURNED = "returned"` (visible "returned with notes", distinct from `DRAFT`).
- **`BuyPlanLineStatus`** — add `INBOUND = "inbound"` (PO sent to vendor, awaiting arrival). Add `INBOUND` to
  `RESOURCEABLE_LINE_STATUSES` (so a re-source can fall an inbound line back to `RESOURCING`).

### 3.2 New decision action
- `svc_decide(db, request_id, user, action, comment=None)` gains a third `action` value **`"return"`**
  (alongside `"approve"` / `"reject"`): sets request→`RETURNED`, recipient→`RETURNED`, records an
  `ApprovalEvent(event_type="returned", payload={"comment": ...})`, and dispatches the subject's return
  side-effect. `comment` (correction notes) is **required** for `"return"`.

### 3.3 Columns
- **`User`** (`app/models/auth.py`): add
  - `can_approve_sales_orders` `Boolean` default `False` (Gate-1 right).
  - `out_of_office_until` `UTCDateTime` nullable (OOO window end).
  - `delegate_to_id` `Integer FK(users.id, ondelete=SET NULL)` nullable (OOO delegate).
- **`BuyPlan`** (`app/models/buy_plan.py`): no new SO columns — `sales_order_number` becomes the **single
  canonical** Sales Order; the ops `so_status`/`so_verified_*`/`so_rejection_note` fields are **folded into
  Gate 1** (see §11.B) and stop being written by the retired verify-SO path. (`so_status` column is retained
  for back-compat read but is no longer the source of truth; the engine `SALES_ORDER` request is.)

### 3.4 New table
- **`BuyPlanAttachment`** (`buy_plan_attachments`) — mirrors `RequisitionAttachment`
  (`app/models/sourcing.py:303`): `buy_plan_id` FK(`buy_plans_v3`, CASCADE), `file_name`, `library_item_id`,
  `library_drive_id`, `library_web_url`, `content_type`, `size_bytes`, `uploaded_by_id`, `created_at`.
  Used for the Stage-D PO PDF; upload route + storage reuse `attachment_service.store_and_attach`.

### 3.5 Removals / retirements
- Delete `_should_auto_approve` + `buyplan_auto_approve_threshold` and the `auto_approved` shortcut in
  `submit_buy_plan` / `resubmit_buy_plan` (the `BuyPlan.auto_approved` **column** may remain, always `False`,
  to avoid a data migration — confirm at phase time).
- Retire `/v2/partials/buy-plans/{id}/approve` (and its legacy `approve_buy_plan` fallback) once no pre-engine
  plans remain; the buy-plan detail UI posts to the engine decision endpoint.

---

## 4. Roles & gate→role routing

Roles (`UserRole`): `BUYER, SALES, TRADER, MANAGER, ADMIN, AGENT`. Rights are **per-user boolean toggles**
on `User`, not role-derived. The routing eligibility query lives in `approvals/routing.py:route_request`.

| Gate | `ApprovalGateType` | Opens when | Required right | Typical role | Negative outcome |
|------|--------------------|-----------|----------------|--------------|------------------|
| **1 — Sales-Order approval** | `SALES_ORDER` | salesperson submits a buy plan with an SO# | `can_approve_sales_orders` *(new)* | sales/ops manager | **Return w/ corrections** → salesperson *(primary)*; hard **Reject** → kill *(exception)* |
| **2 — Buy-plan approval** | `BUY_PLAN` | Gate 1 approves | `can_approve_buy_plans` | purchasing manager | *(none — edit & approve)* |
| **PO approval** | `PURCHASE_ORDER` | buyer enters a PO# | `can_approve_pos` | purchasing manager | **Return** → buyer |
| **Prepayment** *(standalone)* | `PREPAYMENT` | buyer submits a prepayment | `can_approve_prepayments` (+`prepayment_approval_limit`) | manager | **Reject** → buyer |

**Resolved approver chain at submit (acceptance):** when a salesperson submits, the UI shows the resolved
chain ("Goes to: SO approval → buy-plan approval") naming the eligible approver(s) for each downstream gate,
computed from the same eligibility query `route_request` uses. If **any** gate has zero eligible approvers,
submission is **blocked** with a clear message (no orphan-safe silent no-op) — an admin must grant the right.

---

## 5. Lifecycle state machine

### 5.1 BuyPlan status (`BuyPlanStatus`)
```
DRAFT ──submit(SO#)──▶ PENDING ──Gate1 approve──▶ PENDING ──Gate2 approve──▶ ACTIVE ──all lines closed──▶ COMPLETED
  ▲                       │  └─Gate1 return─▶ RETURNED ─(salesperson edits + resubmit)─▶ PENDING (re-enters Gate 1)
  │                       │
  └───────────────────────┴─(RETURNED → DRAFT only if salesperson abandons; cancel → CANCELLED; halt → HALTED)
```
- A plan is **PENDING** while any gate request is open; the **current gate = the open `ApprovalRequest.gate_type`**.
- Gate 1 approve closes the `SALES_ORDER` request and opens a `BUY_PLAN` request (still `PENDING`).
- Gate 2 approve closes the `BUY_PLAN` request and sets plan **`ACTIVE`** (buyers notified) — this replaces today's
  auto-approve transition; **no plan reaches `ACTIVE` without traversing Gate 1 then Gate 2.**
- `HALTED` / `CANCELLED` reachable from PENDING/ACTIVE via explicit manager/owner action (existing behavior).

### 5.2 Line status (`BuyPlanLineStatus`)
```
AWAITING_PO ──buyer enters PO# + PO approved + PDF attached + sent-to-vendor──▶ INBOUND
INBOUND ──received + passed testing──▶ VERIFIED ─(plan rolls up)─▶ (plan COMPLETED when all lines terminal)
INBOUND ──re-source / parts rejected in receiving──▶ RESOURCING ─(buyer re-claims)─▶ AWAITING_PO
```
- `INBOUND` is a **line** state; the plan/deal surfaces "inbound" when it has any `INBOUND` line and no earlier-stage lines.
- Re-source from `INBOUND` (or the existing `PENDING_VERIFY`/`VERIFIED`) reuses `resource_line`
  (`buyplan_workflow.py:464`) + `po_cancellation_service` → line `RESOURCING`.

---

## 6. Return-for-correction semantics

- **Trigger:** Gate 1 (or PO gate) `return` action with required correction notes.
- **Effect:** request→`RETURNED`; plan→`RETURNED` (Gate 1) / line stays `AWAITING_PO` (PO gate); append
  `ApprovalEvent(event_type="returned")` carrying the notes; notify the submitter **immediately** (critical,
  individual) with a deep-link to the item and the notes shown inline.
- **Resume:** on resubmit, a **fresh request for the same gate** is opened (Gate 1 → `SALES_ORDER`; PO → `PURCHASE_ORDER`).
  Earlier-cleared gates are **not** re-run. (Per D5 there is no multi-gate "which gate to resume" puzzle.)
- **First-class status:** `RETURNED` is visually and in queries **distinct from `REJECTED`** — a returned plan
  shows "Returned for correction" with the notes, lands on the salesperson's My-Desk "Returned" chip, and is
  one click from re-edit. A hard `reject` (deal should not proceed) remains available at Gate 1 but is the
  exception; the primary negative action is **return**.

---

## 7. SLA, reminders, escalation, notifications

### 7.1 SLA clock (new scheduled job `app/jobs/approval_sla.py`, registered like the outbox drain)
- **Business hours** = Mon–Fri, `APPROVAL_SLA_BUSINESS_START`–`APPROVAL_SLA_BUSINESS_END` (defaults `08:00`–`17:00`)
  in `APPROVAL_SLA_TIMEZONE` (new setting; default `"UTC"` until configured). Weekends excluded.
- **Aging badge + reminder:** when an `ApprovalRequest` has been `REQUESTED`/open for **≥ 4 business hours**,
  set an aging flag (drives the badge) and enqueue **one** reminder to each `PENDING` recipient.
- **Escalation:** when open for **≥ 24 wall-clock hours**, enqueue a notification to the **manager tier**
  (active users with `UserRole.MANAGER`; fallback `UserRole.ADMIN`; if the approver is already a manager,
  escalate to `ADMIN`) and flag the request escalated. Escalation target is config-overridable
  (`APPROVAL_ESCALATION_USER_IDS`) — there is **no per-user manager hierarchy** in the schema, so this is
  role/config based, stated explicitly to avoid a TBD.
- **Idempotency:** reminder and escalation each fire **at most once** per request (tracked via an
  `ApprovalEvent` marker: `event_type in {"reminded","escalated"}`); the job is safe to run on every tick.
- **Expiry:** `ApprovalRequest.expires_at`, if set, transitions `REQUESTED → EXPIRED` and notifies the owner.
  (Default unset → never expires; no behavior change unless an expiry is configured.)

### 7.2 Notification model (reuse `ApprovalOutbox`, channels `in_app` + `email`; add `teams`)
- **Critical** (return, reject, escalation, prepayment wire-approved): sent **individually + immediately**.
- **Non-critical** (routine "approved/advanced" FYIs): **batched hourly** into one digest per recipient.
- **Email suppression:** if the recipient acts on an item **in-app within a short grace window** (default 10 min),
  the queued redundant **email** for that item is suppressed (the in-app + the action already informed them).
- **Deep-links:** every notification links straight to the item (`/v2/buy-plans?...` / detail partial).
- **My-Desk digest:** one daily 08:00 (business-hours TZ) digest of "what's on my desk" per recipient.

### 7.3 Channels available (from the map — reuse, don't reinvent)
- Email: `email_service.send_batch_rfq` pattern → Graph `/me/sendMail` (`utils/graph_client.py`).
- Teams 1:1 IM: `teams_notifications.send_teams_dm(user, message, db)` (needs the recipient's token).
- Teams channel/group: webhook `teams_notifications.post_teams_channel(message)` (no per-user token — use for the
  **AP Group chat** in Stage F).

---

## 8. Audit & delegation

- **Audit (reuse `ApprovalEvent`, append-only):** every gate decision (`submitted`, `approved`, `rejected`,
  `returned`, `reminded`, `escalated`, `reassigned`, `cancelled`) records **who / when / action / notes**.
  Extend the event `payload` to snapshot the decided subject: `amount`, and for Gate 2 the **final edited
  line set** (offer ids, qtys, vendors, prices, buyer assignments) so the record is self-contained even if the
  plan later changes. Manager Gate-2 edits are themselves logged (before→after) as `ApprovalEvent`s.
- **Delegation:** manual `events.reassign(db, request_id, from_user, to_user, actor)` stays as-is. Add an **OOO**
  check in `route_request`: when an otherwise-eligible approver has `out_of_office_until > now` and a
  `delegate_to_id`, the recipient row is created for the **delegate** instead (recorded in the event payload as
  `delegated_from`). If the delegate is also OOO or unset, fall back to the original approver (never drop the gate).

---

## 9. Lost-work / autosave acceptance criteria (Phase 2 — the #1 fix)

- **Builder selections persist server-side** as entered: the New-SO builder
  (`_sales_order_new.html`) and the buy-plan detail modals (`buy_plans/detail.html` submit/approve/PO modals)
  autosave each field on **blur OR ~3 s debounced keyup** into a server-side **DRAFT** (the builder
  auto-creates/updates a `DRAFT` `BuyPlan` via `create_sales_order_from_offers` / `_assemble_buy_plan`).
- A **mid-entry reload loses nothing** — reopening the builder/modal restores the saved draft. (Test:
  populate fields, autosave fires, simulate reload, assert values restored.)
- Autosave is **debounced/consolidated** — one "**N changes saved**" toast with **undo**, never one toast
  per keystroke.
- **Navigate-away guard:** leaving with unsaved edits prompts (Alpine + `beforeunload`): *Save changes* / *Discard*.
- **Soft-delete + restore** replaces hard `DELETE` for drafts (a status flag + restore endpoint; restorable
  within the window).

---

## 10. Monitoring / "My Desk" acceptance criteria (Phase 8)

- **Per-person My-Desk** (built over the Supervise board / role-default landing): a worklist of *my* open
  items — gates where I'm a `PENDING` recipient, POs to cut (my `AWAITING_PO` lines), and plans **returned to me** —
  with **status-count chips** including a **distinct "Returned" count**, and an **always-open details pane** to
  approve / return / edit **in place** (HTMX-load the right pane; no navigate-away).
- **Pipeline monitor** (managers): every transaction's **stage, owner, age, blocked-on**, with **aging/stalled
  flags** (driven by the §7.1 SLA clock), **swimlanes by stage or owner** + an auto-surfaced urgent/high-value
  lane, and a **stage stepper** on the detail showing "who it's waiting on" next.
- **Clean, not noisy:** calm by default; color/pills carry meaning; urgency (aging/stalled) shown only when real.

---

## 11. Per-stage acceptance criteria (Stages A–F)

Each bullet is an explicit, testable assertion. Phases map: A→P3, B+C→P4, D→P5, E→P6, F→P7, monitor→P8, autosave→P2.

### A. Win + build (salesperson) — *Phase 3*
- A1. Building the buy plan from a requisition transitions the req to **WON**: the build path calls
  `requisition_state.transition(req, RequisitionStatus.WON, actor, db, reason=...)` (WON requires a non-empty
  outcome reason). **Today it does not** (`htmx_views.py:14011` builds without transitioning) — this wires it.
- A2. A **Clone** action (`requisition_service.clone_requisition`) produces an independent, searchable
  requisition (`cloned_from_id` set; offers cloned as REFERENCE) so the salesperson can keep searching after closing.
- A3. The builder lets the salesperson **check offers + set qtys** and enter the **Acctivate SO# once**; submit
  opens **Gate 1** (`SALES_ORDER` request) and sets plan `PENDING`. The SO# is captured a single time and never re-asked.
- A4. Submitting with **no eligible Gate-1 approver** is blocked with a clear message (per §4).

### B. Sales-Order approval (sales/ops manager, Gate 1) — *Phase 4*
- B1. A Gate-1 approver (holds `can_approve_sales_orders`, is a `PENDING` recipient) can **Approve** → opens Gate 2,
  or **Return w/ corrections** (required notes) → plan `RETURNED`, salesperson notified immediately with notes.
- B2. The old ops "verify-SO" step (`so_status`, the verify-SO modal/route) is **folded into Gate 1** and the
  standalone verify-SO path is retired; `BuyPlan.sales_order_number` is the single canonical SO.
- B3. A returned plan, once corrected and resubmitted, **re-enters Gate 1** (fresh `SALES_ORDER` request); Gate 2
  is not reachable until Gate 1 approves.

### C. Buy-plan approval (purchasing manager, Gate 2) — *Phase 4*
- C1. A Gate-2 approver (`can_approve_buy_plans`) can **edit the plan inline** (offers, vendors, qtys, prices,
  Quality-Plan info, **and buyer assignment per line**) via HTMX click-to-edit; edits autosave and are
  **audit-logged** (before→after).
- C2. **Approve = release.** There is **no reject/return at Gate 2.** Approving finalizes the (possibly edited)
  plan, sets it `ACTIVE`, and **notifies the assigned buyers** with what to buy, from which vendor, and all
  details **including Quality-Plan info**. Buyers are not notified before approval.
- C3. The manager's Gate-2 edits **do not** re-open Gate 1 (authorized finalize); the audited final state is the
  snapshot of record.
- C4. **No plan auto-approves** — `_should_auto_approve` is gone; every plan reaches `ACTIVE` only via Gate 1 then Gate 2.

### D. PO execution (buyer → purchasing manager) — *Phase 5*
- D1. A buyer enters a **PO#** on a line in Avail and submits a **PO-approval** request (`PURCHASE_ORDER` gate,
  `can_approve_pos`). The buyer cannot skip the gate.
- D2. The PO approver can **Approve** (→ proceed) or **Return** to the buyer (e.g. wrong PO#) with notes.
- D3. On approve, the manager **attaches the PO PDF** (`BuyPlanAttachment` via `attachment_service.store_and_attach`)
  and **sends it to the vendor via Avail** (reusing the Graph email-send path); the line auto-transitions to **`INBOUND`**.
- D4. The PDF attach + vendor send are verifiable (attachment row exists; an email/contact record is created), and
  the send-to-vendor is what flips the line to `INBOUND`.

### E. Inbound → close / fall-down (buyer) — *Phase 6*
- E1. An `INBOUND` line holds until either **re-source** or **received + passed testing**.
- E2. The buyer marks **received → complete**; when all of a plan's lines are terminal (verified/received), the plan
  becomes `COMPLETED` (explicit buyer action — replaces today's auto-complete).
- E3. **Re-source** (vendor cancels) and **parts-rejected-in-receiving** share one path: `resource_line` +
  `po_cancellation_service` → line `RESOURCING` with a reason; the line returns to the re-source/claim queue.
- E4. A rejected-in-receiving line records its reason and drops to `RESOURCING` (folds in the old SP-4 fall-down).

### F. Prepayments (standalone) — *Phase 7*
- F1. A buyer **submits** a prepayment; a manager (`can_approve_prepayments`, within `prepayment_approval_limit`)
  **approves the wire**. Not coupled to the A–E lifecycle (lives in the Vendor Prepayments tab).
- F2. On approve, fan out two channels: an **email** to `Accounting@TRIOSCS.COM` (Graph send path) **and** a
  **Teams message to the AP Group chat** (webhook `post_teams_channel` — no per-user token needed). Both fire on
  approval; recipients/targets are verifiable and both channels are asserted (mocked) in tests. *(A per-user
  Teams DM to a specific accounting user is deferred — `send_teams_dm` needs that recipient's token; the AP-group
  post + the email satisfy the "instant message to Accounting + AP Group chat" requirement.)*
- F3. The prepayment approval gate is enforced (a buyer cannot self-approve a wire).

---

## 12. Out of scope / non-goals (this rework)

- No dollar-threshold approver tiering (D4); no auto-approve / auto-complete (D5/C4).
- No per-user manager **hierarchy** schema — escalation is role/config based (§7.1).
- No calendar-aware OOO — only `out_of_office_until` + `delegate_to_id` (D2).
- No `QP_SALES` repurposing; the Quality-Plan flow is untouched.
- Deferred (post-rework, per the phased plan): enrichment finish-work, schema-drift #464, the alembic
  downgrade-base xfail, dormant-flag activation, the parked-decisions batch, verify/doc cleanup.

---

## 13. Phase mapping (which phase implements which criteria)

| Phase | Implements | Migration? |
|------|------------|-----------|
| **P2** Autosave / lost-work | §9 | soft-delete flag (likely) |
| **P3** Stage A | §11.A | — |
| **P4** Stages B+C (two gates) | §3.1–3.3 (`SALES_ORDER`+`RETURNED`+`can_approve_sales_orders`), §4, §6, §11.B, §11.C, remove auto-approve, retire legacy path | gate-type + status enum + user-right; atomic w/ in-flight data move |
| **P5** Stage D | §3.4 (`BuyPlanAttachment`), `INBOUND` line state, §11.D | attachments table + line-status enum |
| **P6** Stage E | §11.E (re-source/fall-down/close) | — |
| **P7** Stage F | §11.F (Teams+email fan-out) | — |
| **P8** Monitoring / My-Desk | §10, §7 SLA job, OOO routing (§8) | OOO user columns (may land in P4) |

Each code phase writes its own bite-sized TDD sub-plan from this spec
(`docs/superpowers/plans/2026-06-28-approvals-rework-pN-*.md`) and re-verifies line numbers before editing.
