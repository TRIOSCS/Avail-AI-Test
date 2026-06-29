# Approvals Tab (Frozen Scope) — Implementation Plan

> **Status (2026-06-29):** SP1–SP4 shipped & deployed. **REMAINING (stranded):** fold SO-verification into the single manager approval — `verify_so` is still a separate blocking gate (`app/services/buyplan_workflow.py`). This is the one open item left in the frozen scope.

> **For agentic workers:** Use superpowers:test-driven-development per task (failing test first).
> Scope is **frozen** — see `docs/superpowers/specs/2026-06-28-approvals-rework-acceptance.md`. Build only
> what is listed there. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The buy plan is the one vehicle: built (saved) → SO# added → ONE manager approval → buyers notified
→ buyers enter PO#s for tracking. Achieved mostly by **removing** code (auto-approve, the second approval) and
**renaming** ("Sales Order" → buy plan).

**Tech Stack:** FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · HTMX 2.x · Alpine.js 3.x · Jinja2 · pytest (xdist).

## Global Constraints
- Build ONLY what the frozen-scope spec lists. **Do not** add gates, fields, migrations, SLA, autosave,
  soft-delete, undo, or any SO/PO-creating code path.
- HTMX + Alpine + Jinja2 (no React). Status via `BuyPlanStatus` constants. `db.get(...)`. Loguru.
- TDD: failing test first. Targeted test runs only (`TESTING=1 PYTHONPATH=<worktree> pytest …`), never the full
  suite concurrent with the nightly cron. Keep `sales_order_number` / `po_number` fields.
- SAVE at each task (commit). Independently shippable; one PR to `main`.

---

## Task 1: Remove auto-approve — every plan gets the one manager approval

**Files:** Modify `app/services/buyplan_workflow.py` (`_should_auto_approve` + `submit_buy_plan` ~74 +
`resubmit_buy_plan` ~854); `app/config.py` (`buyplan_auto_approve_threshold` ~251). Test: `tests/test_buyplan_workflow*.py`.

- [ ] **Step 1 — Failing test.** Assert a low-cost plan that would have auto-approved now goes to `PENDING`
  with an open `BUY_PLAN` approval request (no `ACTIVE`, no `auto_approved=True`). Reuse the existing
  buyplan_workflow test fixtures (find the file via `grep -rl "submit_buy_plan" tests/`).
- [ ] **Step 2 — Run; verify it fails** (today the plan auto-approves to `ACTIVE`).
- [ ] **Step 3 — Implement.** Delete `_should_auto_approve`; in `submit_buy_plan` and `resubmit_buy_plan`,
  remove the auto-approve branch so they unconditionally set `PENDING` + call `_open_engine_request_for_plan`.
  Delete `buyplan_auto_approve_threshold` from `config.py` and any remaining references
  (`grep -rn "auto_approve" app/`). Leave the `BuyPlan.auto_approved` column in place (always `False`; no migration).
- [ ] **Step 4 — Run; verify pass** + the existing buyplan_workflow suite stays green.
- [ ] **Step 5 — Commit:** `feat(approvals): remove auto-approve — every plan gets the one manager approval`.

## Task 2: One approval only — fold the SO-verification step in

**Files:** Modify `app/services/buyplan_workflow.py` / `app/routers/htmx_views.py` (the `verify-so` route +
`so_status` writes) + `app/templates/htmx/partials/buy_plans/detail.html` (verify-SO modal). Test: as below.

- [ ] **Step 1 — Investigate first.** `grep -rn "so_status\|verify.so\|verify_so\|SOVerification" app/` — determine
  whether SO-verification is a **separate blocking approval** or just an annotation. If it is NOT a separate
  blocking gate, this task is naming-only (Task 3) — record that and skip. If it IS a separate gate, continue.
- [ ] **Step 2 — Failing test.** Assert a submitted plan requires **exactly one** approval to reach `ACTIVE`
  (the manager approval) — no separate SO-verification step gates it.
- [ ] **Step 3 — Run; verify it fails.**
- [ ] **Step 4 — Implement.** Retire the separate verify-SO gate path (route + modal + `so_status` blocking
  semantics) so the single `BUY_PLAN` manager approval covers the SO#. The manager sees the SO# on the plan and
  approves plan + SO# together. Keep the `sales_order_number` column.
- [ ] **Step 5 — Run; verify pass** + existing approvals tests green.
- [ ] **Step 6 — Commit:** `feat(approvals): single manager approval covers the buy plan + SO# (retire verify-SO gate)`.

## Task 3: Rename "Sales Order" → "buy plan" (labels only; keep fields/routes)

**Files:** Modify `app/templates/htmx/partials/approvals/_sales_order_new.html` (title "New Sales Order" →
"New Buy Plan"; button "Create Sales Order" → "Build Buy Plan") + the hub label for that surface
(`buy_plans/hub.html` / `approvals_tab_partial`). Keep `sales_order_number` / `po_number` columns, the
`sales-orders` route paths, and the `create_sales_order_from_offers` function name (internal; rename deferred —
out of frozen scope to churn). Test: render assertion.

- [ ] **Step 1 — Failing test.** GET the builder (`/v2/partials/approvals/sales-orders/new?requisition_id=…`)
  asserts the rendered title says "Buy Plan" and not "New Sales Order".
- [ ] **Step 2 — Run; verify it fails.**
- [ ] **Step 3 — Implement** the user-facing label changes only.
- [ ] **Step 4 — Run; verify pass.**
- [ ] **Step 5 — Commit:** `chore(approvals): label the builder "buy plan", not "Sales Order"`.

## Task 4: Verify persistence + buyer notification (confirm; minimal fix)

**Files:** read-mostly; small fix only if a gap is found.

- [ ] **Step 1 — Confirm** building a plan persists a `DRAFT` immediately (`create_sales_order_from_offers`
  commits) and the SO# entry persists. Add a test only if a gap exists.
- [ ] **Step 2 — Confirm** approval notifies the buyers (today: `_generate_buyer_tasks` creates each assigned
  buyer's "cut PO" task). If buyers are not actually surfaced/notified, add the minimal notification — no new
  framework, reuse the existing task/outbox path. Test the notification fires on approve.
- [ ] **Step 3 — Commit** if anything changed: `fix(approvals): ensure approval notifies buyers`.

---

## Wrap-up
- `pre-commit run --files <changed>` clean. Update `docs/APP_MAP_INTERACTIONS.md` if approval routing changed.
- PR to `main`; verify CI rollup (`test` + `security`) = SUCCESS. Deploy via `deploy.sh`; live-verify on real PG:
  submit a low-cost plan → it waits for manager approval (no auto-approve); one approval → `ACTIVE` + buyers
  notified. SAVE memory.

## Self-review (vs. frozen-scope spec)
- Remove auto-approve (T1) ✅ · one approval only (T2) ✅ · rename (T3) ✅ · persistence + notify (T4) ✅.
- Nothing from the "NOT in scope" list is built. No new gates/fields/migrations/SLA/autosave/soft-delete.
