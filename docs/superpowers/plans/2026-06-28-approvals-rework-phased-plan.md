# Approvals Rework — Phased Implementation Plan (rework-first)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each phase task-by-task. This is a **program roadmap** spanning multiple subsystems; each code phase (2–8) gets its **own** bite-sized TDD plan written just-in-time, in `docs/superpowers/plans/2026-06-28-approvals-rework-pN-*.md`, before its code is touched. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Approvals module to match the user's re-captured deal-lifecycle vision (Stages A–F), eliminate the lost-work that the original crash caused, and fix the wrong approval routing — shipping in independently-deployable increments.

**Architecture:** Keep the existing approval **engine + state machine** (`app/services/approvals/*`, `app/models/approvals.py`) and the 5-stage tab IA; rework only the lifecycle model, the Sales-Order concept, the approval routing, and the data-entry UX on top. Server-rendered HTMX + Alpine throughout (no SPA). Each phase is a self-contained, shippable slice with its own tests, migration (if any), and live-verify.

**Tech Stack:** FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic · HTMX 2.x · Alpine.js 3.x · Jinja2 · Tailwind 3.x · pytest (xdist) · Microsoft Graph (Teams/email).

**Source-of-truth spec:** `docs/superpowers/specs/2026-06-28-approvals-rework-vision.md` (the user's verbatim Stage A–F workflow + locked decisions + research-backed UX). **Read it before any phase.** Memory: `project_approvals_rework_2026_06_28`.

## Global Constraints

- **Stack is HTMX + Alpine + Jinja2 — NOT React.** Reuse existing UI-conformance primitives, `.page-fluid`/`.page-readable`, status pills, `.compact-table`, resizable-modal pattern, the `template_response()` partial contract. No new UI conventions.
- **No band-aids — root-cause only.** Workarounds get the PR closed.
- **No auto-approve** — every plan traverses a human gate (remove the threshold auto-approve).
- **ALL schema changes via Alembic** (never raw DDL in startup/services/routers). Migration workflow: model change → autogenerate → review → upgrade/downgrade/upgrade round-trip on a throwaway PG → commit with the model change. Claim migration numbers via `MIGRATION_NUMBERS_IN_FLIGHT.txt`; verify single head.
- **Always write tests with new code** (don't ask). TDD: failing test first.
- **SAVE at each phase boundary** — commit + push + update the `project_approvals_rework_2026_06_28` memory. The user's connection is unstable; never lose more than one increment.
- **Deploy = `./deploy.sh` from `main`** (one host; `recover_prod.sh`/`post-deploy.sh` are empty stubs). After deploy, **live-verify on real PG** (alembic head, gate routing, role-scoped render, no 500s) and confirm new Tailwind classes landed in the built CSS.
- **CI gate = rollup conclusion** of `test` + `security`, never `gh pr checks --watch` (false-greens on TLS). Don't run the full suite locally concurrent with the ~02:30 nightly cron (OOMs; has a pre-existing unrelated failing sightings test).
- **Each code phase = its own PR + its own detailed TDD sub-plan** authored when the phase starts (later phases depend on Phase 1's acceptance criteria — do not pre-fabricate their steps).

---

## Phase ordering (recommended)

| # | Phase | Type | Ships | Depends on |
|---|---|---|---|---|
| 0 | Warm-up: boot-reset defect + merge #537 | bugfix + hygiene | independently | — |
| 1 | Durable acceptance-criteria spec | spec (no code) | the spec doc | — |
| 2 | Kill lost-work (autosave / draft recovery) | feature | independently | 1 |
| 3 | Stage A — win + build | feature | independently | 1, 2 |
| 4 | Stages B+C — the two approval gates | feature + migration | independently | 1, 3 |
| 5 | Stage D — PO execution → inbound | feature + migration | independently | 1, 4 |
| 6 | Stage E — inbound → close / fall-down | feature | independently | 1, 5 |
| 7 | Stage F — prepayments (standalone) | feature | independently | 1 |
| 8 | Monitoring + "My Desk" workview | feature | independently | 3–6 |

Phases 1, 2, and 7 have no cross-dependencies beyond the spec and can be reordered if priorities shift. Phases 3→6 are the lifecycle spine and must land in order.

---

## Phase 0 — Warm-up: live boot-reset defect + merge #537

**Goal:** Operator-enabled connectors survive a reboot; clean baseline before the rework.

**Root cause (verified against live code 2026-06-28):** `app/startup.py:98`, inside `run_startup_migrations()`, runs:
```python
_exec(conn, "UPDATE api_sources SET is_active = false WHERE status = 'disabled' AND is_active = true")
```
`ApiSource.status` is the **auto-managed health state** (set by `app/services/health_monitor.py`; `disabled` = "no connector available"); `ApiSource.is_active` is the **operator toggle** (set by `PUT /api/sources/{id}/activate` in `app/routers/sources.py`). They are orthogonal — coupling them wipes operator intent on every boot.

**Design note (must resolve, not paper over):** `run_startup_migrations()` returns early when `TESTING` is set (`app/startup.py:85-87`), so a test that merely calls it won't exercise this statement. The fix + test below extract the connector-reconciliation concern into a named, directly-testable helper so the regression test is **not** vacuous.

**Files:**
- Modify: `app/startup.py` (remove line 98's coupling; introduce `_reconcile_connector_active(conn)` no-op-preserving helper, called from the `with engine.connect()` block)
- Test: `tests/test_startup.py` (new test; reuse `_make_sqlite_engine()` at `tests/test_startup.py:27` and the `ApiSource` seed shape from `tests/test_connectors_settings.py:46` `_seed_sources`)
- Verify-only: `app/startup.py:1213-1214` (`seed_api_sources()` sets `is_active = status == "live"` — confirm this only runs on **first insert** of a row, never overwrites an existing operator toggle; if it can re-run on existing rows, fold that into the fix)

- [ ] **Step 1 — Write the failing regression test.** Seed `ApiSource(status="disabled", is_active=True)`; call `_reconcile_connector_active(conn)`; assert the row's `is_active` is still `True`. (Also assert a `status="live", is_active=False` row is left untouched — reconciliation must not *enable* anything either.)
- [ ] **Step 2 — Run it; verify it fails** (`_reconcile_connector_active` undefined / current coupling flips it false).
- [ ] **Step 3 — Implement the fix.** Delete the line-98 coupling; add `_reconcile_connector_active(conn)` that does **not** touch `is_active` (root-cause: stop clobbering intent — a `disabled` source with no connector simply can't run, and the toggle is correctly retained for when health later recovers). Wire it into `run_startup_migrations()` in place of the removed line.
- [ ] **Step 4 — Grep-confirm no reader depends on the old behavior.** `grep -rn "is_active" app/routers/sources.py app/services/health_monitor.py` — confirm every reader only *filters* on active and none assumes "disabled ⇒ inactive."
- [ ] **Step 5 — Run the test; verify it passes.** `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_startup.py -v`
- [ ] **Step 6 — Commit** on branch `fix/connectors-boot-reset`, push, open PR, merge on green rollup, deploy via `deploy.sh`, live-verify (enable a creds-less connector in Settings → restart → confirm it stays enabled).
- [ ] **Step 7 — Merge PR #537** (nightly coverage, tests-only, CI green): un-draft if needed → merge.

**Acceptance:** new test green; on a real reboot an operator-enabled-but-health-disabled source keeps `is_active=true`; #537 merged; PR CI rollup SUCCESS.

**User-side, non-blocking (track, don't gate):** rotate the exposed gh PAT; prune noisy claude.ai connectors (keep MS 365 / Clay / Sentry).

---

## Phase 1 — Durable acceptance-criteria spec (no code)

**Goal:** Convert the locked vision into a build-ready spec so execution has zero ambiguity (CLAUDE.md: zero TBDs) and intent can never be lost again.

**Deliverable:** `docs/superpowers/specs/2026-06-28-approvals-rework-acceptance.md` (committed + pushed), resolving the open spec-time decisions the research surfaced:
- [ ] **Returned-for-correction semantics:** after a Gate-1/Gate-2 reject, does the plan **restart at Gate 1** or **resume at the returning gate**? (Recommend: resume at the returning gate; the prior gate's approval stands unless the edit changes its inputs.) Define what edit invalidates an upstream approval.
- [ ] **Audit trail + delegation:** exact immutable record per gate decision (who/when/reason/snapshot); out-of-office delegation handoff.
- [ ] **Reminder / escalation / digest timings** for procurement SLAs (per-stage nudge delay → escalate-to-manager; batch-non-critical window).
- [ ] **Gate→role routing table:** Gate 1 = sales/ops manager right; Gate 2 = purchasing-manager right; PO gate = `can_approve_pos`. Show the resolved approver chain to the requester at submit.
- [ ] **Per-stage acceptance criteria + edge cases** for Stages A–F, and the data model deltas (canonical `BuyPlan.sales_order_number`; new `inbound` state; second gate type).

**Acceptance:** spec committed; every Stage A–F step and every locked decision maps to explicit, testable acceptance criteria; no TBDs. Each later phase's sub-plan is written from this spec.

---

## Phase 2 — Kill lost-work (autosave / draft recovery) — first code

**Goal:** Persist input server-side as it's entered so a crash never wipes work (the #1 fix — the exact failure that started this).

**Scope (detailed plan: `…-p2-autosave.md`):** the New-SO builder and the submit/approve/reject/PO modals stop holding state only in Alpine. Per-input autosave (`hx-post` on blur **or** ~3s-debounced `keyup`) into a server-side DRAFT, returning a tiny "saved" fragment; consolidated "N changes saved" toast with undo; navigate-away guard modal (Alpine + `beforeunload`) on unsaved edits; soft-delete + restore window instead of hard DELETE. Builder auto-creates/updates a DRAFT buy plan via the existing origination seam.

**Key touch-points (verify at plan-time):** `app/templates/htmx/partials/approvals/_sales_order_new.html`; `app/templates/htmx/partials/buy_plans/detail.html` (modals); autosave partial routes alongside `create_sales_order_from_offers` / `_assemble_buy_plan` in `app/services/buyplan_builder.py`. Respect the Alpine double-quote/`tojson` and htmx `[filter]`-hugging rules in CLAUDE.md.

**Acceptance:** a draft survives a mid-entry reload (no data loss); autosave is debounced/consolidated (not one toast per keystroke); navigate-away with unsaved edits prompts; soft-deleted drafts are restorable. Tests cover autosave persistence, debounce, and restore. Independently shippable.

---

## Phase 3 — Stage A: win + build

**Goal:** Salesperson builds the buy plan from the requisition and closes the req to a win.

**Scope (detailed plan: `…-p3-stage-a.md`):** RFQ→WON trigger (building the plan closes the req to a win); **clone-the-req** action to keep searching after closing; builder = **offer checkboxes + qty inputs** + **Acctivate SO# entered once**; submit → routes to Gate 1. Reuse `create_sales_order_from_offers` / `_assemble_buy_plan`. Apply smart defaults (auto-assign to current user; show full requisition scope so auto-added lines aren't a surprise).

**Acceptance:** building a plan from a WON RFQ closes the req; clone produces an independent searchable req; checkbox+qty selection + single SO# entry submits to Gate 1; tests cover the WON trigger, clone, and submission. Independently shippable.

---

## Phase 4 — Stages B+C: the two approval gates (fixes wrong routing)

**Goal:** Two ordered gates, two roles — the core routing fix.

**Scope (detailed plan: `…-p4-gates.md`):** **Gate 1 = Sales-Order approval** (sales/ops manager; reviews SO in Acctivate; **reject carries correction notes** back to the salesperson — "returned for correction" is a first-class status). On approve → **Gate 2 = buy-plan approval** (purchasing manager, **can edit** the plan, then approve) → **notify buyers** with part/vendor/all details incl. Quality-Plan info. Collapse the **3-way Sales Order** into the single canonical `BuyPlan.sales_order_number`; fold the ops "verify-SO" into Gate 1. **Retire the legacy approve path** so only the engine drives side-effects. Remove threshold auto-approve. New second gate type alongside the existing buy-plan gate (Alembic migration for the gate type + any data backfill; round-trip + data-op test like migration 164).

**Hard ordering constraint:** the gate-routing change and any in-flight-request data migration must ship **atomically** (same PR) so no approval is orphaned mid-flight (lesson from SP-2's `qp_sales` rename).

**Acceptance:** a submitted plan goes Gate 1 → Gate 2 → buyers, each routed to the correct role; reject returns correction notes and is distinct from a hard reject; no plan auto-approves; one canonical SO#; legacy approve path gone; migration round-trips; tests cover routing, returned-for-correction, and the data migration. Independently shippable.

---

## Phase 5 — Stage D: PO execution → inbound

**Goal:** Buyer cuts POs, manager approves, PO goes to the vendor and the line becomes "inbound."

**Scope (detailed plan: `…-p5-stage-d.md`):** buyer enters **PO#** in Avail → **PO approval** request to the manager (`PURCHASE_ORDER` gate + `can_approve_pos`) → manager approves → **attach PO PDF** to the buy plan (reuse the attachments subsystem) → **send to vendor via Avail** (reuse the existing Graph email-send path) → status auto-becomes **"inbound"** (new buy-plan/line state — string column, no enum migration unless a DB enum is in play; confirm at plan-time). Reuse the existing `po_cancellation_service` seam where applicable.

**Acceptance:** PO# entry → approval → PDF attach → send-to-vendor flips the line to inbound; buyer can't skip the gate; PDF + email verified; tests cover the gate, attach, send, and state transition. Independently shippable.

---

## Phase 6 — Stage E: inbound → close / fall-down

**Goal:** Close out received parts; route rejected/cancelled parts back to re-source.

**Scope (detailed plan: `…-p6-stage-e.md`):** an inbound line holds until either **re-source** (reuse `po_cancellation_service` + `resource_line` in `app/services/buyplan_workflow.py`) **or** **confirmed arrived + passed testing** → buyer marks **received → complete → archive**. Add the **parts-rejected-in-receiving** trigger into the same re-source path (this folds in the old SP-4). Replaces today's auto-complete behavior with an explicit buyer action.

**Acceptance:** buyer can mark received/complete/archive; rejected-in-receiving drops the line into the re-source queue with a reason; vendor-cancel and parts-rejected share one re-source path; tests cover both close and fall-down. Independently shippable.

---

## Phase 7 — Stage F: prepayments (standalone)

**Goal:** A pure notification workflow, fully separate from the deal lifecycle.

**Scope (detailed plan: `…-p7-prepayments.md`):** buyer **submits** a prepayment → manager **approves the wire** → on approval, fan out **email + Teams instant message** to `Accounting@TRIOSCS.COM` **and** the **AP Group chat in Teams** (reuse the existing Microsoft Graph client + notification/outbox plumbing). Not connected to A–E; lives in the Vendor Prepayments tab.

**Acceptance:** submit → approve → email + Teams IM both fire to the correct recipients; nothing couples it to the buy-plan lifecycle; tests cover the approval gate and the Graph fan-out (mocked). Independently shippable. (No lifecycle deps — can ship any time after Phase 1.)

---

## Phase 8 — Monitoring + "My Desk" workview

**Goal:** Make every transaction's stage/owner/age visible, and give each person a one-glance worklist — so a deal can never silently get lost.

**Scope (detailed plan: `…-p8-monitoring.md`):** a per-person **"My Desk"** worklist (approvals waiting on me, POs to cut, plans returned to me) with status-count chips (incl. a distinct *returned* count) and an always-open details pane to act in place; a **pipeline monitor** (every transaction's stage, owner, age, blocked-on, with aging/stalled flags, swimlanes by stage/owner + an auto-surfaced urgent/high-value lane, and a stage stepper showing "who's it waiting on"). Built over the existing Supervise board + role-default landing. Apply the **clean, not noisy** goals (progressive disclosure, color = meaning, calm-by-default — urgency only when real). Worklist items can surface incrementally as Phases 3–6 land.

**Acceptance:** each role sees its My-Desk worklist; managers see the pipeline monitor with accurate aging/stalled flags; UI matches the existing design system (verified in built CSS); tests cover lens scoping + aging logic. Independently shippable.

---

## Deferred (after the rework — none urgent; do not block the rework)

From the recovered open-work program (`…-approvals-rework-vision.md`, Phases 3–7): enrichment finish-work (contact-discovery pass, email re-verify, `can_use_credits`) + **schema-drift #464** + the alembic downgrade-base xfail; **dormant-flag activation** (SAM.gov + Hunter free/safe now; cost/multi-user flags gated); small wiring (resell My-Day task, calendar delta sync, vendor soft-archive); the **parked-decisions batch** (QP C2c, roadmap connectors, OEM Phase B, knowledge-jobs, webhooks Phase 4/5, prospecting SP-2 Clay async, presence, resell on-win); and verify/doc cleanup (overdue Pre-Rollout TLS/DNS gate, 8x8 0-rows, stale-doc archive). Each is itself spec→plan→TDD when picked up.

---

## Self-review (against the vision spec)

- **Stage coverage:** A→P3, B+C→P4, D→P5, E→P6, F→P7 — all five lifecycle stages mapped. ✅
- **Locked decisions:** two gates/two roles (P4), no auto-approve (P4 + global constraint), canonical SO# (P4), keep engine + 5-tab IA (architecture), returned-for-correction first-class (P1 semantics + P4), lost-work fix (P2), clean-not-noisy + match-the-site (global + P8). ✅
- **Cross-cutting goals:** lost-work (P2), monitoring/workview (P8), simple data entry (P2/P3), match look-feel-logic (global constraint, every phase). ✅
- **No fabricated detail:** Phases 2–8 deliberately carry scope + acceptance criteria, not invented bite-sized steps — each gets its TDD sub-plan from the Phase-1 spec at execution time (per writing-plans' multi-subsystem decomposition). Phase 0 is fully detailed because it's verified and ready. ✅
