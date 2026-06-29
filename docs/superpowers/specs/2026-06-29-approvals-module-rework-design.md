# Approvals Module Rework — Design (build-ready)

> **Approved by the user 2026-06-29 (full scope).** Reworks the WHOLE Approvals module —
> not just the Supervise lens — for two goals: **(1)** a clean, simple, scannable UI across
> every surface, and **(2)** a simple workflow to **progress deals through the stages**.
> Builds on the shipped Supervise foundation (PR #588: the uniform `queue` in
> `buyplan_hub.supervise_overview` + calm `_supervise.html` + de-noised `_board.html`).
> Synthesizes three refinement passes: architecture, frontend-design, and a YAGNI/simplify
> critique. Supersedes the scope of `2026-06-29-supervise-lens-redesign-design.md` (that lens
> becomes one instance of the row language defined here).

## Goals & non-goals
- **Goal:** one consistent row language module-wide; a role-aware **My Queue** ("what needs
  YOU now") + a **Pipeline** (deals as cards in 4 stage columns); a flattened detail page; and
  a simplified one-approval workflow.
- **Non-goal:** no SPA, no new framework, no new design tokens, no new navigation *mechanism*
  (reuse the existing lens switcher). Reuse the existing view-models and approvals engine.

## Stages (canonical, 4)
**Build → Approve → Purchase → Done.** `INBOUND` is a sub-status of Purchase. Off-ramps:
**Halted / Cancelled** (resubmit from Halted). Status→stage map (rendering only):
`DRAFT→Build`, `PENDING→Approve`, `ACTIVE|INBOUND→Purchase`, `COMPLETED→Done`. Use these four
labels EVERYWHERE (tabs, stepper, pipeline columns, card pips) — retire the old
"Draft/Pending/Active" vocabulary.

## Design system (aesthetic — the one law)
**Color is the accent, shape is the job, everything structural is gray.** Spend the single
azure accent (`--accent`/`accent-500/600`/`accent-ring`) on exactly one thing: the live
"who-has-the-ball" node. Status hues (emerald/amber/rose) mean health only; the neutral
`brand` ramp + `--line*` hairlines are all structure.

### Primitive rule (resolves the three-pill conflict)
| Primitive | Shape | Job | Cardinality |
|---|---|---|---|
| `status_badge()` / `.badge` | rounded-full | whole-object **lifecycle status** | exactly one |
| `.chip` | rounded-rect | a **metadata tag** (SO#, MPN, vendor, "stock") | many |
| `.badge-success/warning/danger` | rounded-full | a **number's health** (margin % only) | one |

Row-level classifier → `.chip` wins; `.badge`/`status_badge` is reserved for whole-object
status. **Kill** every hardcoded `bg-blue-50` banner (→ `bg-brand-50 border-line-base`; an
*actionable* banner earns an `accent-500` left rule + `.btn-primary`) and every stray
`border-gray-200` (→ `.border-line-base`).

### The signature: "who-has-the-ball" at three scales
The one memorable, comprehension-carrying cue, reusing the Requisitions dot-grammar:
- **Queue row (micro):** a leading **risk-band dot + uppercase microlabel** — 3 bands, not 6
  kind-hues: **At-risk** (Halted/Flagged/Overdue) `rose-500`; **Decide** (Approve/Prepay)
  `accent-500`; **Routine** (Verify) `brand-400`.
- **Pipeline card (mini):** a **4-pip** strip `●●○○` — done=`brand-500` fill, ball=`accent-500`,
  upcoming=hollow `brand-200`. Replaces the blocker line *and* the progress bar.
- **Detail (full):** a 4-node **stepper** — done node gray-fill+check; current node azure fill
  + `accent-ring` halo + caption ("With you" / "With Sales" / "Needs approval" / "With Buyer");
  upcoming hollow; Halted node `rose-500` + dashed connector ahead. One accent, one meaning.

### One My-Queue row (grid; echoes `.opp-row`)
`grid: 16px 84px 1fr auto auto auto auto` → `dot · KIND · identity(1fr) · age · value · margin · action`.
- **Customer** `text-sm font-semibold text-gray-900` (the one bold thing).
- **Value** `font-semibold tabular-nums` right-aligned (second-loudest).
- **Kind** risk-dot + `text-[11px] font-semibold uppercase` (band color).
- **Margin** `.badge-success ≥30 / .badge-warning ≥15 / .badge-danger else` (exact `_board` thresholds).
- **Age** `tabular-nums text-gray-500`; **>72h → `text-amber-600 font-medium`** (only aging cue).
- **Secondary line** `.text-tertiary`, middot-joined present-only fields (`SO`/`MPN` in `.font-mono`,
  vendor, `{AM|Buyer} name`; flagged appends reason in `text-rose-600`).
- **Action** right rail, quiet: Approve→`[Approve][Review]`; Verify→`[Verify][Reject]`;
  Halted/Flagged/Overdue→whole row links, trailing `Open →`. **Cut the colored left rail**
  (the dot+sort already encode risk — no double-encoding). needs-my-action ring → `ring-accent-400`.
- **Copy from the user's side:** empty = "You're all caught up. Nothing needs you right now.";
  verbs stay constant through the flow (`Approve`→toast "Approved"; `Reject`→"Sent back to buyer.").
  Filter chips: `All · Halted · Flagged · Overdue · Approve · Verify` (collapse Verify-SO+PO into
  one "Verify" chip) with live counts; active `bg-accent-600 text-white`.

## Architecture
### My Queue — one builder
Add `my_queue(db, user) -> list[QueueRow]` to `app/services/buyplan_hub.py`. **Extract** the six
inline source queries in `supervise_overview` into private `_query_*` helpers and have BOTH
`supervise_overview` (shape unchanged) and `my_queue` call them — single source of truth, so a
workflow change just stops emitting a kind. `QueueRow` = frozen dataclass (kind, priority, label,
plan_id, line_id, customer_name, primary_mpn, tso=sales_order_number, value, age_hours, is_overdue,
action_url, action_label, detail_href, extra). Module consts `_QUEUE_PRIORITY`/`_QUEUE_LABEL`
(risk-first: halted 1, plan_returned 2, plan_approve/prepay_approve 3, po_verify 4, claim 5,
cut_po_overdue 6, cut_po 7, receive 8, plan_draft 9). Role gating selects which kinds are emitted
(reuse `can_approve_buy_plans`, `can_approve_purchase_orders`, ops membership, role); prepay rows
reuse the engine's `_actionable_request_ids(... gate=PREPAYMENT)`. Jinja never touches ORM —
only `QueueRow`. Reuse `_user_name`/`_customer_name`/`_issue_reason`.

### Two surfaces via lens values (not a new mechanism)
Add lenses `my_queue` + `pipeline` to `_APPROVALS_TABS` and `approvals_tab_partial` in
`app/routers/htmx/buy_plans.py`; new partials `approvals/_surface_my_queue.html` +
`approvals/_surface_pipeline.html`. `hub.html`: two-button strip; preserve the lazy
`hx-trigger="load"` + `hx-target="#bp-hub-body"` contract; inline action buttons set
`hx-push-url="false"`. `_default_lens`: buyers/sales/most → `my_queue`, supervisors → `pipeline`.
**Pipeline** = `deals_board()` called per column (`[DRAFT]`,`[PENDING]`,`[ACTIVE,INBOUND]`) +
`completed_archive()` for Done (collapsed); extract a `_deal_card()` macro + `_metric_strip()`
macro. Old 5 lenses stay until retired in the final phase.

### Workflow fold (full scope — the only migration/risk piece)
1. **One approval absorbs verify-SO:** in `_run_approve_side_effects` (right after
   `plan.status=ACTIVE`), set `so_status=APPROVED` + `so_verified_by_id=approver` +
   `so_verified_at=now`. `check_completion`'s `so_status==APPROVED` gate then passes; no separate
   verify-SO step. Retire the verify-SO approve/reject route + modal + `so_pending` query + queue
   kind. Keep `sales_order_number` + `so_status` columns.
2. **Preserve Halt standalone:** extract the `verify_so(action="halt")` body into `halt_plan(plan_id,
   user, db, *, reason)` (auth = supervisor/ops), new `POST .../halt`, surfaced from detail. This
   is the ONLY halt path — must survive.
3. **verify-PO → manager role:** replace the `VerificationGroupMember` check in `verify_po()` with
   `can_approve_purchase_orders`; add `require_buyplan_po_approver` dep; inline one-click in My Queue.
4. **Data migrations (must precede code on deploy — deploy.sh runs `alembic upgrade head` first):**
   (a) `UPDATE buy_plans SET so_status='approved', so_verified_at=now() WHERE status IN ('active','inbound')
   AND so_status='pending'` (else those plans never auto-complete — R2); (b) `UPDATE users SET
   can_approve_purchase_orders=TRUE WHERE id IN (SELECT user_id FROM verification_group_members WHERE
   is_active) AND can_approve_purchase_orders IS DISTINCT FROM TRUE` (grandfather current PO verifiers
   — R3). Data-only Alembic migration(s) via `op.execute(text(...))`, claim next slot on current head,
   single head verified.

### Detail flatten
Decompose `detail.html` (~1000 lines) into `_detail_stepper.html` (the 4-stage signature stepper),
`_detail_action_banner.html`, `_detail_lines.html`, `_detail_sidebar.html` (collapsed secondary:
notes/AI flags/history/case-report). Extract `line_status_pill` macro. Behavior-preserving except
the stepper (4 stages) + removing the now-dead verify-SO banner (lands with the workflow phase).

## Phase plan (each: TDD → subagent build → review → Tailwind gate → full suite → PR → I merge → deploy → live-verify on real PG → memory save)
- **A — Foundation (no UI):** extract 6 `_query_*` helpers; `QueueRow` + consts; `my_queue()`; unit tests.
- **B — My Queue surface:** `_surface_my_queue.html` + route + 2-button shell + the row language/3-band/signature dot; prepayments folded in.
- **C — Pipeline surface:** `_surface_pipeline.html` + route + 4 columns + 4-pip cards + `_deal_card`/`_metric_strip` macros. (B and C parallel after A.)
- **D — Workflow fold (highest risk):** SO fold + `halt_plan` + verify-PO→manager + the two data migrations + 4-stage stepper update. Land as ONE PR rebased on B+C.
- **E — Detail flatten:** decompose into partials + signature stepper.
- **F — Retire old 5 lenses + opportunistic primitive/token cleanup** (no big-bang restyle; tidy each template as touched). Then branch/worktree cleanup.

## Acceptance & verification
- Per surface: full suite green (`-n auto`); built CSS has all new classes; live-drive on real
  prod PG (seed admin + docker-net IP) — My Queue shows correct per-role rows + chips filter;
  Pipeline columns map to the 4 stages; **no capability lost vs the old 5 tabs**.
- Workflow: a submitted plan reaches ACTIVE via exactly one manager approval; `check_completion`
  needs no separate verify-SO; **Halt still works standalone**; manager verify-PO one-click works;
  post-deploy `count(active+so_status=pending)=0` and ops verifiers hold `can_approve_purchase_orders`.
- Migration round-trips on a throwaway PG; single alembic head.

## Risks (from the architecture pass)
R1 phase ordering (B/C vs D touch `hub.html`/`buy_plans.py` — land D rebased on B+C). R2 SO-fold
backfill must precede code (same PR as the migration). R3 ops PO-rights backfill same. R4 the
`_query_*` extraction must not change `supervise_overview`'s return shape (run suite before adding
`my_queue`). R6 `hx-push-url="false"` on all inline action buttons. R7 keep the stepper 4-stage
update inside phase D (not E).
