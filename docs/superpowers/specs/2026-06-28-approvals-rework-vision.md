# Approvals Module — REWORK Vision & Phased Plan (RECOVERED)

> **Recovered 2026-06-28** from an interrupted session (`c64b8707`) after an internet/terminal
> crash. This is the user's re-laid-out vision for the Approvals workflow ("we were way off base"),
> captured verbatim below, plus the deep-research-backed UX guidance and the phased build plan that
> was approved in plan mode just before the crash. **This file is the durable source of truth** — per
> the plan's own Phase 2 / Step 0, the authoritative workflow must live in a committed spec so intent
> can never be lost to a crash again. Companion research: `../research/2026-06-28-approval-ux-research.md`.
> Memory pointer: `project_approvals_rework_vision_2026_06_28`.

---

# Phased Plan — Complete All Open Work (AvailAI)

## Context

A deep multi-modal sweep (git/PR, specs/checklists, code markers, feature flags, project
memory, deploy delta) plus the fresh handoff (`project_session_handoff_2026_06_28`) produced a
full inventory of unfinished/offline work. The repo is healthy — main CI is green, only 2 open
PRs, 1 open issue — so the backlog is not in branches. It is: one live user-facing defect, the
active Approvals program (SP-3/SP-4), a set of post-provider-removal no-op stubs, a dozen
merged-but-dormant features behind unset flags/keys, a few security/hygiene user actions, and a
cluster of parked items that need a decision before anyone builds them.

This plan sequences all of it. User decisions captured for this plan:
- **Parked items → decide-then-build**: plan the actionable work in full; present parked items
  as a decision batch with a recommendation each; build only the green-lit ones.
- **Dormant flags → free/safe now, gate the rest**: enable zero-cost/low-risk flags now; hold
  cost-incurring and multi-user-gated ones for explicit sign-off.
- **Ordering → quick wins → defect → program**, then finish-work, dormant activation, parked
  decisions, verify.

### Operating constraints (apply throughout)
- **Deploy = publish a GitHub Release.** `.github/workflows/deploy.yml` triggers on
  `release: published`; `docker-entrypoint.sh` runs `alembic upgrade head` on boot and refuses to
  start if it fails. There is **one host** (no separate prod box; `recover_prod.sh`/`post-deploy.sh`
  are empty stubs). Do **not** run alembic manually on top of a deploy.
- **CI verification**: confirm the status-check *rollup conclusion*, never `gh pr checks --watch`
  (false-greens on TLS). Branch protection requires `test` + `security`.
- **Don't run the full suite locally**: the `scripts/nightly_tests.sh` cron (~02:30, `--cov`)
  OOMs xdist under memory pressure and has a *pre-existing* failing test
  (`test_sightings_router_coverage3.py::...test_batch_refresh_mixed_failed_and_skipped`) — not a
  branch signal. Rely on PR CI for the full-suite gate; run targeted tests locally.
- **Process**: brainstorm → spec → plan → TDD → review → deploy; subagent-driven; **SAVE at each
  SP boundary** (commit + push + update memory) — the user's router is unstable.
- DB inspection routes through the read-only `postgres` MCP (`availai_ro`), not `docker exec psql`
  (classifier-blocked).

---

## Phase 0 — Quick wins: security & hygiene (mostly user actions + small merges)

Goal: clean baseline before building.

1. **Rotate the GitHub PAT (USER — security, do first).** Token `gho_…oBrf` is gh's live CLI
   token. Steps: revoke at github.com/settings/applications → Authorized OAuth Apps → GitHub CLI →
   Revoke; `gh auth login` to re-auth; remove from `availai/.env`:
   `sed -i '/^GITHUB_TOKEN=/d;/^GITHUB_REPO=/d' availai/.env`. (`.env` left untouched by tooling —
   secrets-guard hook.)
2. **Activate the new guard hooks (USER/session).** Run `/hooks` reload or restart so the two
   project `PreToolUse` guards (`.claude/hooks/git-add-guard.sh`, `.claude/hooks/requirements-guard.sh`)
   take effect. Plugin/MCP changes (ralph-loop off, `github` MCP removed, `postgres`→read-only)
   apply next session start.
3. **Prune claude.ai connectors (USER).** Remove Viator, Intuit QuickBooks, Gmail, Google
   Calendar, Google Drive, Apollo.io. Keep Microsoft 365, Clay, Sentry (~70 irrelevant tools/session).
4. **Merge PR #539** (`chore/readonly-role-setup-script`, docs-only RO-role SQL) once CI rollup =
   SUCCESS.
5. **Land PR #537** (nightly coverage, CI green, tree clean): un-draft → merge. Low priority.
6. **Stale-branch cleanup.** ~51 deletable remote branches (~45 merged `feat/*`/`fix/*` +
   6 unmerged stale: 5 nightly-coverage + `fix/schema-drift-gate`, a dup of merged #463). Run
   `scripts/branch-cleanup.sh --apply --remote` (archives unmerged as tags; hooks block direct
   `-D`/`--delete`). Optional coverage-diff before discarding the 2 net-new files on
   `test/nightly-coverage-85pct`.

---

## Phase 1 — Live defect: connectors boot-reset fix (P1)

Goal: operator-enabled connectors survive a reboot.

- **Root cause**: `app/startup.py:98` runs
  `UPDATE api_sources SET is_active=false WHERE status='disabled' AND is_active=true` on every
  boot. `ApiSource.status` (`app/models/config.py:13-44`) is the **auto-managed health state**
  (source of truth, set by `app/services/health_monitor.py` — `disabled` = "no connector
  available"); `is_active` is the **operator toggle** (set by `PUT /api/sources/{id}/activate`,
  `app/routers/sources.py:592-609`). They are orthogonal; coupling them wipes operator intent.
- **Fix**: remove the offending startup line (preserving `is_active` is harmless — a `disabled`
  source with no connector simply can't run, and intent is correctly retained for when a
  connector/credential later becomes available and health recovers). Confirm via grep that nothing
  reads `is_active` expecting it to be false-for-disabled before deleting.
- **Regression test** in `tests/test_startup.py` (pattern: `_make_sqlite_engine()` + seed via the
  `_seed_sources()` helper in `tests/test_connectors_settings.py`): seed an `ApiSource` with
  `status='disabled', is_active=true`, run the startup migration step, assert `is_active` stays
  `true`.
- Root-cause, not band-aid: do not "re-enable on next health success" — just stop clobbering intent.

---

## Phase 2 — REWORK the Approvals module (re-brainstorm from intent, then rebuild)

⚠️ **CHANGED 2026-06-28 (user):** "the whole buy-plan approval process is set up wrong … the
system kept crashing … it lost a lot of info of what I wanted … we need to totally rework
everything on the approval tab." So Phase 2 is **not** an incremental SP-3/SP-4 continuation —
it is a rework driven by re-captured intent. SP-3 (PO receiving) and SP-4 (fall-down) fold
**into** the new design, not on top of the old one.

**Scope LOCKED with user (2026-06-28):**
- ✅ The captured deal lifecycle is correct → **implement it cleanly** (see below).
- ✅ **Keep the approval engine + state machine** (`app/services/approvals/*`,
  `app/models/approvals.py`) — rework only the lifecycle model, the SO concept, the tab content,
  and the data-entry UX on top.
- ✅ The 5-stage tab IA is fine (user did NOT flag layout) — keep it.
- 🎯 **Three priority fixes** (user's words): (1) losing input on crash, (2) Sales Order split
  3 ways, (3) wrong approval routing — all three now resolved into the design below.
- Routing, SO-canonicalization, and auto-approve are **decided** (see "All scope decisions
  LOCKED"). The only pre-code step is writing the durable spec + acceptance criteria.

### ⭐ AUTHORITATIVE WORKFLOW (user's own words, 2026-06-28 — the recaptured intent; implement this)

> This is the source of truth for the rework. The very first build action (Phase 2 step 0) is to
> copy this verbatim into a committed spec + memory so it survives any future crash.

**A. Win + build (salesperson).**
1. An RFQ is closed to **WON** status.
2. The salesperson **builds the buy plan from the requisition** and sets status to WON — **building
   the plan closes the req to a win.**
3. **Clone feature:** provide a way to clone (the req) in case they want to keep searching after
   closing the original to a win.
4. In the builder they **check boxes on the offers they want to use + set the qtys**.
5. They **enter the Acctivate sales (SO) number** and hit **submit** → the buy plan goes to the
   **manager for approval**.

**B. Sales-Order approval (manager).**
6. The manager **reviews the Sales Order in Acctivate** and **approves or rejects**.
   - **Reject** → the salesperson is **notified of the specific corrections** to make.
   - **Approve** → goes to the purchasing manager.

**C. Buy-plan approval (purchasing manager).**
7. The **purchasing manager can easily edit the buy plan**, then hits **approved**.
8. That **notifies the buyers**: what part to buy, from what vendor, and all important details
   (incl. the **Quality Plan info**, etc.).

**D. PO execution (buyer → manager).**
9. The **buyer cuts the POs in Acctivate**, enters the **PO number into Avail**, and sends the
   **PO approval request to the manager**.
10. The manager **looks in Acctivate and issues/approves the PO**, returns to Avail, **attaches a
    PDF of the PO to the buy plan**, and **sends it to the vendor via Avail** → status
    **automatically becomes "inbound."**

**E. Inbound → close.**
11. It stays **inbound** until either a **re-source** is needed OR it's **confirmed arrived and
    passed testing** (receiving).

**F. Prepayments (standalone — NOT connected to the above).**
12. A pure **notification workflow**: a **buyer submits** a prepayment; a **manager approves the
    wire**. On **approval**, send an **email + instant message to `Accounting@TRIOSCS.COM` and the
    AP Group chat in Teams**.

**Key deltas from the as-built tab:** (a) explicit RFQ→WON trigger + req clone; (b) offer
checkboxes + qtys in the builder; (c) **two pre-buyer approvals** — SO approval, then a
purchasing-manager buy-plan approval with edit — (this is the "wrong routing" to fix); (d) reject
carries **correction notes** back to sales; (e) PO step adds **PDF attach + send-to-vendor →
auto-inbound**; (f) prepayments are fully standalone with **Teams + email** fan-out. Auto-approve
(today's `_should_auto_approve` threshold) is **removed** — every plan goes to the manager (LOCKED).

### Cross-cutting design goals (user, 2026-06-28 — apply to EVERY step below)
- **Simple, clean, effective data entry.** Minimal fields, sensible defaults, inline validation,
  no redundant re-entry (enter the SO# once), forgiving forms with autosave. Every screen earns its
  inputs; nothing asks twice.
- **Clean, not noisy (user, 2026-06-28).** Restraint over density. Show only what matters for the
  current step; use **progressive disclosure** (detail on demand, collapsible sections) instead of
  cramming everything on screen. Color/pills carry **meaning, not decoration**; one clear primary
  action per view; minimal chrome, generous whitespace, no badge/alert clutter. Calm by default —
  surface urgency (aging/stalled) only when it's real.
- **Really good organization.** Clear information hierarchy across the 5 stage tabs; each
  transaction shows its stage, owner, key dates/amounts, and "what's next / who's it waiting on" at
  a glance. Consistent primitives/typography (follow the existing UI conformance system).
- **Transaction monitoring + workview.** A first-class way to *monitor every transaction* as it
  moves Won → SO approval → buy-plan approval → PO → inbound → received/complete, plus a per-person
  **workview** ("what's on my plate now"). Surfaces aging, bottlenecks, and anything stalled so a
  deal can never silently get lost (reinforces the data-loss fix). Built on the existing Supervise
  board + role-default landing, made genuinely good. (Exact lens = the question being asked now.)
- **Match the rest of the site's look, feel, AND logic (user, 2026-06-28).** The rework must be
  indistinguishable from the existing app — reuse the established design system and interaction
  conventions, do NOT invent new patterns:
  - *Visual*: the UI-conformance primitives (accent/buttons/inputs/focus rings), typography, the
    `.page-fluid`/`.page-readable` width policy, status pills, `.compact-table`, and the
    responsive/resizable-modal pattern — the same primitives the Sales Hub / requisitions2 use.
  - *Interaction logic*: server-rendered **HTMX + Alpine** (no SPA); the `template_response()`
    partial contract; HTMX conventions (hx-target/trigger, `hx-push-url` inheritance, no-cache
    middleware for partials); Alpine single-expression handlers + tojson-quoting rules.
  - *Verification*: confirm any new Tailwind classes actually land in the built CSS (safelist if
    needed) post-deploy. Follow `docs/HTMX_ALPINE_TOOLKIT.md` + the APP_MAP docs; update APP_MAP
    after changes. The deep-research UX details get **adapted into these existing primitives**, not
    bolted on as foreign components.

### Research-backed UX details to adopt (deep-research, 2026-06-28 — verified, primary sources)
Adapt each into our **existing** HTMX+Alpine primitives; keep it clean/low-noise. Source product in
parens.

**Lost-work prevention (the #1 fix — GitLab Pajamas, Airtable):**
- **Per-input autosave, never whole-form**: `hx-post` each field on **blur OR ~3s after last
  keypress** (debounced `keyup`), returning a tiny confirmation fragment. Consolidated toast
  "**N changes saved**" with an **undo**, not a toast per keystroke.
- **Guard modal on navigate-away** with unsaved edits (Alpine + `beforeunload`): "Your changes are
  not saved" → *Save changes* (primary) / *Discard and leave* (secondary).
- **Soft-delete with a restore window**, not hard DELETE (a status flag + restore endpoint).

**Data-entry ergonomics (HTMX, Linear, ClickUp, Pipefy):**
- **Inline click-to-edit** for buy-plan cells (HTMX's native click-to-edit) — no full-page reload.
- **Near-optimistic UI**: render submitted values immediately, reconcile via `hx-swap` (full SPA
  optimism isn't worth it server-rendered — a fast round-trip with a subtle pending state is fine).
- **Smart defaults**: auto-assign new items to the current user; pre-fill sensible values; enter the
  SO# **once**. **Field conditionals** — reveal fields only when relevant (keeps forms clean).
- **Clone/duplicate** action (already a locked requirement for the req-clone).

**Approval structure & routing (Zudello, Kissflow, Teams Approvals):**
- Ordered **gates that clear before the next opens** (matches our SO → buy-plan → PO chain);
  "require responses in assigned order" semantics.
- **Dollar-threshold → approver-tier routing** (which manager tier), reusing the deferred §15
  per-role limits — but per the LOCKED decision **every plan still needs a human approve (no
  auto-approve / no auto-complete-under-$X)**. At submit, **show the requester the resolved approver
  chain** ("this will go to …") before sending.
- **Reminders + escalation**: a scheduled job nudges a pending gate after a delay and can escalate
  to the approver's manager, flipping an **aging badge** on the item so nothing stalls silently.
- **Approve/reject with reasons**; **"returned for correction"** is a **first-class status distinct
  from rejected**, carrying the specific corrections back to the requester (your Stage-B reject).

**Status & stage visualization (Trello, Jira, steppers):**
- **Aging cue** keyed off last-activity: a subtle progressive fade and/or an explicit pill
  ("pending 3d") — surface staleness only when real (clean/low-noise).
- **Swimlanes by stage or owner**, plus an auto-surfaced **urgent/high-value lane** (driven by
  amount or aging) and a catch-all — for the monitor/Supervise view.
- A **stage stepper** on the detail showing where the deal is and **who it's waiting on** next.

**Notifications (Microsoft Planner, Asana):**
- Notify **only relevant/@mentioned** people; **fan out in-app + email + Teams**; every notification
  **deep-links straight to the item**.
- **Batch non-critical** within ~an hour into one digest; send **critical events individually**;
  **suppress the redundant email** if the user handles it in-app within a short grace window.
- **Layered unread cues**: sidebar dot, per-item count bubble, and a browser-tab title dot — subtle,
  not loud.

**Per-person workview + monitoring (ClickUp Me Mode, Asana Inbox):**
- A one-click **"My Desk"** worklist of *my open items* with **status-count chips** (incl. a
  distinct *returned* count), spanning everything assigned to me.
- An **always-open details pane** so an approver can approve/reject/return **in place** without
  navigating away (HTMX-load the right pane).

**Spec-time detail-decisions surfaced by the research (settle in Step 0's spec, not now):**
re-submit after correction — restart at Gate 1 or resume at the returning gate?; concrete
audit-trail + delegation/out-of-office handoff; exact reminder/escalation/digest timings for
procurement SLAs (the 30s/60min/per-stage defaults are product-specific).

### Current as-built reality (what we are reworking)
5-stage hub [Sales Orders, Buy Plans, Purchase Orders, Vendor Prepayments, Supervise]. The buy-plan
approval flow exists and the underlying **approval engine + state machine are sound**
(`app/services/approvals/service.py` create→route→decide; `BuyPlan.status`
DRAFT→PENDING→ACTIVE→COMPLETED/HALTED/CANCELLED). What is broken/fragmented:
1. **Data-loss = the "lost info."** Every form (New SO builder vendor/price picks; submit modal
   SO#/CPO#/notes; approve/reject/verify modals) holds state **client-side in Alpine until one
   final POST** — no drafts, no autosave, no crash recovery. Files:
   `app/templates/htmx/partials/approvals/_sales_order_new.html`,
   `app/templates/htmx/partials/buy_plans/detail.html` (modals ~592-714).
2. **"Sales Order" lives in 3 half-overlapping places**: `BuyPlan.sales_order_number`, the ops
   `BuyPlan.so_status` "verify SO" step, and the QP `qp_sales` gate — with no enforced dependency.
3. **Two approval code paths** (legacy detail POST `/v2/partials/buy-plans/{id}/approve` vs engine
   REST `/v2/approvals/requests/{id}/decision`) that must never drift.
4. **Implicit auto-approve** (`_should_auto_approve` threshold) with no user-facing explanation.
5. **Stale-recipient / no-eligible-approver** edge cases route to orphan-safe no-ops silently.

### Rework approach (build order — increments map to the workflow stages above)
**Step 0 — Durable spec FIRST.** Copy the AUTHORITATIVE WORKFLOW above into a committed spec
(`docs/superpowers/specs/2026-06-28-approvals-rework-*.md`) + a memory note, **before any code**, so
intent can't be lost again. Settle the one open routing decision (below) with the user.

**Step 1 — Kill the data-loss (top corrective).** Persist work server-side as it's entered so a
crash never wipes input: the builder auto-creates/updates a DRAFT buy plan (debounced autosave),
and the submit/approve/reject/PO modals stop holding state only in Alpine. Touches
`app/templates/htmx/partials/approvals/_sales_order_new.html`,
`app/templates/htmx/partials/buy_plans/detail.html` (modals ~592-714), + autosave partial routes
alongside `create_sales_order_from_offers` (`app/services/buyplan_builder.py`).

**Step 2 — Stage A (win + build).** RFQ→WON closes the req to a win when the buy plan is built;
add the **clone** action (clone the req to keep searching). Builder = **offer checkboxes + qty
inputs** + **Acctivate SO# entry** → submit. Reuse `create_sales_order_from_offers` /
`_assemble_buy_plan` (`app/services/buyplan_builder.py`).

**Step 3 — Stages B+C (the two approvals — fixes "wrong routing"; LOCKED: two gates, two roles).**
**Gate 1 = Sales Order approval** by a sales/ops manager (reviews the SO in Acctivate; reject →
salesperson notified **with correction notes**). **Gate 2 = buy-plan approval** by the **purchasing
manager** (can **edit** the plan, then approve). No auto-approve — every plan traverses both gates.
This needs a second gate type alongside `BUY_PLAN` (e.g. a sales-order gate routed to a sales/ops
approver right, then the existing `BUY_PLAN` gate routed to the purchasing-manager right). Collapse
the **3-way "Sales Order"** into one canonical `BuyPlan.sales_order_number`; fold the old ops
`so_status` "verify-SO" into Gate 1.
**Retire the legacy approve path** (`/v2/partials/buy-plans/{id}/approve`) so ONLY the engine
(`/v2/approvals/requests/{id}/decision`) drives side effects. Approval → **notify buyers** with
part/vendor/details incl. **Quality Plan info**.

**Step 4 — Stage D (PO execution).** Buyer enters **PO#** in Avail → **PO approval** to manager →
manager approves → **attach PO PDF** to the buy plan → **send to vendor via Avail** → auto-**inbound**.
Uses `PURCHASE_ORDER` gate + `can_approve_pos` (`app/models/auth.py:80`). PDF attach reuses the
attachments subsystem; send-to-vendor reuses the existing email-send path; "inbound" is a new
buy-plan/line state.

**Step 5 — Stage E (inbound → close / fall-down).** Inbound holds until **re-source** OR
**received + passed testing**. Re-source reuses `po_cancellation_service` + `resource_line`
(`app/services/buyplan_workflow.py:457`); add the parts-rejected-in-receiving trigger into the same
re-source path; add the buyer "confirm received/passed testing → complete → archive" action.

**Step 6 — Stage F (prepayments, standalone).** Keep prepayments fully separate: buyer submits →
manager approves wire → on approve, fan out **email + Teams IM** to `Accounting@TRIOSCS.COM` + the
**AP Group chat** (via Microsoft Graph; reuse existing Graph client + notification/outbox plumbing).

**Step 7 — Transaction monitoring + workview.** Build the monitoring layer over the now-clean
lifecycle: a per-person **workview** ("what's on my plate" — approvals waiting on me, POs to cut,
plans rejected back to me) and a **pipeline monitor** (every transaction's stage, owner, age, and
what it's blocked on, with stalled/aging flags). Extends the Supervise board + role-default
landing. Per-stage worklist items surface incrementally as Steps 2–6 land, so monitoring grows with
the lifecycle. (Primary lens per the user's answer.)

**Throughout:** TDD; **SAVE at each increment** (commit+push+memory) — router unstable; each
increment independently shippable; deploy via Release; live-verify on real PG. Cross-cutting design
goals (simple/clean data entry, organization, monitoring/workview) apply to every step.

### All scope decisions LOCKED (2026-06-28)
- Lifecycle = the AUTHORITATIVE WORKFLOW above (Stages A–F). ✅
- Keep the approval engine + state machine; rework model/SO/routing/UX on top. ✅
- Keep the 5-stage tab IA. ✅
- **Two gates, two roles**: Sales-Order approval (sales/ops manager) → buy-plan approval
  (purchasing manager, can edit). ✅
- **No auto-approve** — every plan goes to the manager(s). ✅
- One canonical Sales Order = `BuyPlan.sales_order_number`; ops verify-SO folds into Gate 1. ✅
- PO approver = purchasing manager (assumed; trivially adjustable in routing config).
- The detailed acceptance criteria + edge cases get written into the committed spec at Step 0
  before any code (brainstorm only to flesh out spec detail — no scope is open).

---

## Phase 3 — Data-integrity & enrichment finish-work

Goal: close the no-op stubs and the schema/migration debt. Root-cause fixes.

1. **Customer-enrichment secondary pass** (`app/services/customer_enrichment_service.py:87-135`,
   called from `app/routers/crm/companies.py:464`). The main "Enrich" button already works
   (cascade via `enrichment_router.gather_company`). This stub is the *contact-discovery* pass.
   **Reconnect** it to the live providers: `clay_mcp.find_contacts()`
   (`app/connectors/clay_mcp.py`) / `explorium.search_contacts()` (`app/connectors/explorium.py`),
   gated by `clay_enrichment_enabled` / `explorium_enrichment_enabled` (already ON in staging .env).
   Add a test asserting contacts are added when a provider returns results.
2. **Email re-verification** (`app/services/customer_enrichment_batch.py:103-109`, quarterly cron
   `app/jobs/email_jobs.py:373`): point at a configured verification provider, **or** remove the
   dead cron + stub. *(Decide with the batch-credits item below.)*
3. **`can_use_credits` stub** (`customer_enrichment_batch.py:21-23`, returns True): reinstate
   per-provider credit accounting **or** delete the stub + call site. Recommend delete unless
   per-provider gating is still wanted (credit_manager was removed deliberately).
4. **Schema-drift #464** (`scripts/check_schema_matches_models.py`). REMOVE_COLUMNS/REMOVE_FKS are
   already reconciled (migration 154). Remaining buckets to close with **real, data-safe**
   migrations, removing each from its `_GRANDFATHERED_*` set as it lands:
   `_GRANDFATHERED_ADD_INDEXES` (1), `_GRANDFATHERED_REMOVE_INDEXES` (48 raw-DDL trgm/gin/FTS —
   declare in models or formalize as op.create_index), `_GRANDFATHERED_ADD_CONSTRAINTS` (17 unique
   constraints declared-but-never-created), `_GRANDFATHERED_MODIFY_TYPE` (8 UTCDateTime
   reflections), `_GRANDFATHERED_MODIFY_COMMENT` (1). **NEVER touch** the DANGER orphan tables in
   `_GRANDFATHERED_REMOVE_TABLES` (`buy_plans`, `enrichment_credit_usage`, `notification_engagement`,
   `_sp1_desc_backup`, `self_heal_log`). Claim migration numbers via `MIGRATION_NUMBERS_IN_FLIGHT.txt`.
5. **Alembic downgrade-base xfail** (`tests/test_alembic.py:173`): migration `d2bea118f720`
   `_recreate_fk` hard-codes an FK list incl. `error_reports`, dropped earlier by
   `a3f9c1d82e47`. Add a `has_table()` guard (or de-hardcode the list) so
   `alembic downgrade base` replays; then **remove the `xfail(strict=True)` pin** (it flips to
   XPASS/CI-fail when fixed).

---

## Phase 4 — Dormant-feature activation (free/safe now; gate the rest)

Goal: turn on merged features that only need config. Each = `.env` change → redeploy (Release) →
live-verify. **Enable now:**
- **SAM.gov enrichment** — `SAM_GOV_ENRICHMENT_ENABLED=true` (free, no key; gate
  `app/services/enrichment_router.py:201`).
- **Hunter.io enrichment** — `HUNTER_ENRICHMENT_ENABLED=true` (key already present; gate
  `enrichment_router.py:124`).

**Hold for explicit sign-off (do not flip without a yes):**
- `SPEC_RESOLVER_ENABLED` (per-call Opus cost; `app/search_service.py:524`).
- `AI_SCREEN_WEB_SEARCH_ENABLED` (Claude web_search spend; `app/services/prospect_screening.py:307`).
- `OWNERSHIP_SWEEP_ENABLED` (35-day reassignment; gate to multi-user go-live;
  `app/jobs/email_jobs.py:42`).
- **eBay** (`EBAY_CLIENT_ID/SECRET` — needs a free eBay dev app), **Sourcengine**
  (`SOURCENGINE_API_KEY` at go-live), **ACS** call/SMS (`ACS_CONNECTION_STRING` — provision
  resource), **AI features audience** (`AI_FEATURES_ENABLED=all` at multi-user go-live).
- **Datasheet company library** (`DATASHEET_LIBRARY_DRIVE_ID` + Graph `Sites.Selected` grant —
  needs IT). Tracked by PRE_ROLLOUT Gate 10.
- **Vendor-API parametric backfill** — USER runs `backfill_vendor_specs --apply` (dry-run first;
  quota-paced; bulk write).

---

## Phase 5 — Smaller wiring / finish items

- **Resell My-Day seam** (`app/templates/htmx/partials/resell/_not_yet_strip.html:16`,
  `app/routers/resell.py:1034`): in `resell_not_yet_strip()`, create a per-buyer follow-up via
  `task_service.create_company_task()` (`app/services/task_service.py:268`, migration 138 shipped);
  remove the TODO. Small, clear.
- **Calendar delta sync** (`app/jobs/email_jobs.py:82`): implement `/me/calendarView/delta`
  incremental sync reusing existing `SyncState`, so meeting edits/cancellations are captured.
- **Vendor soft-archive**: add `VendorCard.is_active` via Alembic (backfill true + NOT NULL +
  server_default), then a soft-archive control + scoped vendor lists.
- **Presence service** (`app/services/presence_service.py`): needs a graph client threaded to a
  new contact-card partial route before wiring `get_presence()` into vendor/customer templates —
  *or* delete if off-roadmap. **Decide in Phase 6.**
- **Resell on-win hook** (`recompute_buyer_score_on_win`, `app/services/buyer_affinity_service.py:440`):
  ⚠️ scope is larger than "wire a call" — the ExcessOffer→`WON` award/bid-back path itself is
  **not implemented** (no setter found). Investigate first; this is part of a larger resell award
  feature, not a quick wiring. **Decide in Phase 6.**

---

## Phase 6 — Parked decisions (decide → build only the green-lit)

Present as a batch; each has a recommendation. Build follows for the ones approved.
- **QP C2c** (vendor-facing redacted QP share link; spec in main via #536) — *Recommend build
  after* Mike signs off the redaction whitelist on the native QP view. Renumber its old
  "migration 162" (now taken).
- **Roadmap connectors** (findchips/future/heilind/lcsc/rochester/verical, `_PLANNED`,
  `app/services/connector_service.py:58`) — *Recommend* building **LCSC** first (public API ready);
  drop the rest's placeholder cards unless wanted.
- **OEM web-resolution Phase B** — Phase A resolved 0/16. *Recommend* the vetted-reseller two-quote
  tier (3 options exist); else formally drop.
- **Knowledge-jobs AI insights** (`app/jobs/knowledge_jobs.py:38`, disabled cron, no UI,
  high Anthropic cost) — *Recommend delete* the dead generation pipeline unless reviving behind a flag.
- **Email/phone webhooks Phase 4/5** (tag `archive/crm-phase4-calendar-webhooks`) — infra-blocked
  (public HTTPS callback + secret, 8x8 webhook, MVP_MODE carve-out). *Recommend* keep polling until
  infra is ready; restore the tag onto a branch when it is.
- **Prospecting SP-2 Clay async** — *Recommend retire* the async-webhook design (the Clay MCP sync
  client is now live and covers it) unless a webhook is still required.
- **Presence service** & **Resell on-win/award path** (from Phase 5) — decide build vs delete.
- **Tech-debt roadmap** (`docs/superpowers/plans/2026-05-27-deferred-high-tier-roadmap.md`):
  SQLAlchemy-2.0 migration (~1,561 `db.query` callsites) and `htmx_views.py` decomposition
  (~18,302 LOC) are regressing; SEC-4 Graph `validationToken` echo is accepted-risk. *Recommend*
  explicitly: park SEC-4; schedule (or formally drop) the 2.0 migration + god-file split before it
  grows further.

---

## Phase 7 — Verify & doc cleanup

- **Pre-Rollout Gate-3 TLS/DNS renewal** (`docs/PRE_ROLLOUT_CHECKLIST.md`) — **overdue** (was
  "before May 2026"; today 2026-06-28). Verify cert >30 days + DNS, independent of the
  (still-deferred) SFDC import.
- **8x8 call auto-tracking shows 0 rows** — investigate the CDR poll path / connector status /
  matcher on live infra.
- **Materials float-grouping** — live-verify facet counts on real PG (#355).
- **Archive stale gate docs** — `docs/PROJECT_V2_TEST_SCOPE.md` and `docs/FRONTEND_REDESIGN_PLAN.md`
  DoD lists are for shipped work; spot-check then archive to avoid future confusion.

---

## Verification (per phase)

- **Phase 0**: `gh auth status` shows re-auth; `grep -c GITHUB_TOKEN availai/.env` = 0; PRs show
  merged; `git branch -r | wc -l` drops after cleanup.
- **Phase 1**: new `tests/test_startup.py` test green; manual — enable a connector with no creds in
  Settings → Connectors, restart, confirm it stays enabled.
- **Phase 2**: per-SP PR CI green (rollup); targeted approvals tests; after deploy, live-verify on
  real PG (alembic head, gate routing, role-scoped tab render, no 500s). SAVE memory at each SP.
- **Phase 3**: targeted enrichment tests; `python scripts/check_schema_matches_models.py` shows the
  closed buckets empty and still catches NEW drift; `alembic downgrade base && alembic upgrade head`
  round-trips on a throwaway PG and the xfail is removed (CI green, no XPASS).
- **Phase 4**: after each flag flip + Release, hit the feature live and confirm it acts (e.g.
  SAM.gov firmographics appear on a fresh company enrich).
- **Phase 5/6/7**: targeted tests + live-verify per item; decisions recorded in memory.

## Out of scope / explicitly not doing
- No separate prod-host work (one host).
- No editing `requirements*.txt` directly (edit `.in`, recompile — hook-enforced).
- No full local suite runs concurrent with the nightly cron.
- Parked items not green-lit in Phase 6 are not built.
