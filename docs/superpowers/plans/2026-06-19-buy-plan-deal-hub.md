# Buy Plan Deal Hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Reporting nav with a dedicated, role-aware Buy Plan deal hub (sales watch deals, buyers cut/confirm POs, managers supervise), re-tier notifications, and fold the old Reporting analytics into compact contextual chips.

**Architecture:** A new read-model service (`buyplan_hub.py`) feeds three lens partials rendered by the existing `/v2/buy-plans` routes; the rich `detail.html` + all `buyplan_workflow.py` action routes are reused unchanged. Notifications re-tier in `buyplan_notifications.py`. The cross-app alert source re-points from the dead `reporting` tab key to `buy-plans`. No schema change (v1 is migration-free).

**Tech Stack:** FastAPI + sync SQLAlchemy 2.0 + Jinja2 + HTMX 2.x + Alpine.js 3.x + Tailwind; Vite-built `htmx_app.js`/`styles.css`; pytest (TESTING=1, in-memory SQLite) + Playwright.

## Global Constraints
- Stack is HTMX + Alpine + Jinja — server-render + swap, never React. No `innerHTML` (use `htmx.ajax()`/Alpine). No literal `"` inside double-quoted Alpine attributes (use single-quoted or `{# #}`). Lazy/`hx-trigger="load"` containers need an explicit `hx-target`.
- Status values via `app/constants.py` StrEnums (BuyPlanStatus, BuyPlanLineStatus, SOVerificationStatus, UserRole), never raw strings. `db.get(Model, id)` not `query().get()`.
- Keep routers thin; business logic in `app/services/`. The state machine stays in `buyplan_workflow.py` — the hub only READS + re-presents; it never duplicates a transition.
- Every new file gets a header comment (what/calls/depends). Write tests with all new code. Loguru not print.
- v1 is **migration-free**. The buyer "waiting on vendor + ETA" note is a deferred fast-follow (separate migration), NOT in this plan.
- After any code change, update the relevant `docs/APP_MAP_*.md`.
- Run `pre-commit run --files <changed>` before committing; docformatter may need a second run.

---

### Task 1: `buyplan_hub.buyer_line_queue` — the buyer's per-line PO queue

**Files:**
- Create: `app/services/buyplan_hub.py`
- Test: `tests/test_buyplan_hub_queue.py`

**Interfaces:**
- Produces: `buyer_line_queue(db: Session, user: User) -> list[dict]` — one dict per actionable line assigned to `user`, ordered kicked-back-first then by urgency (oldest plan first). Each dict: `{line_id, plan_id, customer_name, mpn, description, vendor_name, vendor_contact_email, quantity, unit_cost, status, kicked_back: bool, po_rejection_note, plan_created_at}`. "Actionable" = `BuyPlanLine.buyer_id == user.id AND status == BuyPlanLineStatus.AWAITING_PO` on a plan whose `status == BuyPlanStatus.ACTIVE`. `kicked_back` = the line has a `po_rejection_note` (ops bounced a prior PO).

- [ ] **Step 1: Write the failing test** — `tests/test_buyplan_hub_queue.py`. Use conftest fixtures (`db_session`, `test_user`, `manager_user`, `test_quote`, `test_requisition`). Build minimal BuyPlan(status=ACTIVE)+BuyPlanLine rows (mirror `tests/test_alert_source_buyplan.py` helpers `_make_plan`/`_make_line`). Cover: (a) my AWAITING_PO line on an ACTIVE plan appears; (b) a line on a DRAFT/PENDING plan does NOT; (c) another buyer's line does NOT; (d) a `po_rejection_note`-bearing line sorts FIRST with `kicked_back=True`; (e) the dict carries mpn/vendor/qty/unit_cost/customer.

```python
def test_buyer_queue_only_my_active_awaiting_lines(db_session, test_user, manager_user, test_quote, test_requisition):
    from app.services.buyplan_hub import buyer_line_queue
    # ... create ACTIVE plan w/ my AWAITING_PO line + a DRAFT plan w/ my line + another buyer's line
    rows = buyer_line_queue(db_session, test_user)
    assert [r["line_id"] for r in rows] == [my_active_line.id]

def test_buyer_queue_kicked_back_first(db_session, test_user, test_quote, test_requisition):
    from app.services.buyplan_hub import buyer_line_queue
    # ... two ACTIVE lines, one with po_rejection_note set
    rows = buyer_line_queue(db_session, test_user)
    assert rows[0]["kicked_back"] is True and rows[0]["line_id"] == kicked.id
```

- [ ] **Step 2: Run → fail.** `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_buyplan_hub_queue.py -v --override-ini="addopts="` — Expected: ImportError (buyplan_hub missing).
- [ ] **Step 3: Implement** `buyer_line_queue` in `app/services/buyplan_hub.py` — `db.query(BuyPlanLine).join(BuyPlan).filter(buyer_id==user.id, BuyPlanLine.status==AWAITING_PO, BuyPlan.status==ACTIVE)`, eager-load requirement/offer/buy_plan.quote for the display fields; sort `(po_rejection_note is None, plan.created_at)`. Build the dicts (read MPN from requirement.primary_mpn, vendor from line.offer/vendor, customer from plan.quote→site→company — match how `buy_plans_list_partial` derives customer_name). Header comment.
- [ ] **Step 4: Run → pass.** Same command. Expected: PASS.
- [ ] **Step 5: Commit.** `git add app/services/buyplan_hub.py tests/test_buyplan_hub_queue.py && git commit -m "feat(buy-plans): buyer_line_queue read model"`

---

### Task 2: `buyplan_hub.deals_board` — stage-grouped deals for sales/manager

**Files:**
- Modify: `app/services/buyplan_hub.py`
- Test: `tests/test_buyplan_hub_board.py`

**Interfaces:**
- Produces: `deals_board(db, user, *, scope: str) -> dict[str, list[dict]]` — keys are the four display columns `("draft","pending","active","done")`; values are deal dicts ordered newest-first. `scope="mine"` filters `submitted_by_id == user.id` (or `requisition.created_by`); `scope="all"` = no filter. Each deal dict: `{plan_id, customer_name, value, margin_pct, stage_label, blocker, po_progress: (cut:int,total:int), needs_my_action: bool, is_stock_sale}`. Column mapping: DRAFT→draft, PENDING→pending, ACTIVE/HALTED→active, COMPLETED→done, CANCELLED omitted. `blocker` = the single current hold-up string ("awaiting approval" | "SO needs verification" | "N POs to cut" | "N POs verifying" | "rejected — resubmit" | "ready to fulfill"). `needs_my_action` true when this user (by role) must act (sales: status==DRAFT & rejected/missing-SO; etc.).

- [ ] **Step 1: Write failing test** — cover: column bucketing by status; `scope=mine` vs `all`; `po_progress` counts lines verified/total; `blocker` text for an ACTIVE plan with 2 awaiting-PO lines == "2 POs to cut"; `blocker`=="ready to fulfill" when all lines verified + so_status approved; CANCELLED omitted.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** `deals_board` — one query for plans (filter by scope), eager-load lines; compute per-plan blocker + po_progress from line statuses + so_status; bucket into the 4 columns. Reuse the customer_name + value/margin derivation from Task 1 / `buy_plans_list_partial`.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit.** `git commit -m "feat(buy-plans): deals_board read model"`

---

### Task 3: `buyplan_hub.supervise_overview` — manager metric strip + triage

**Files:** Modify `app/services/buyplan_hub.py`; Test `tests/test_buyplan_hub_supervise.py`

**Interfaces:**
- Produces: `supervise_overview(db) -> dict` = `{strip: {open_value, avg_margin, approval_count, halted_count, overdue_po_count, flagged_count}, triage: {approvals: [plan_dict], halted: [...], overdue_pos: [line_dict], flagged: [line_dict]}}`. `approval_count` = plans status==PENDING & approved_by_id is None. `overdue_po_count` = AWAITING_PO lines on ACTIVE plans past the nudge SLA (reuse the nudge threshold constant from `buyplan_notifications`/the nudge job). `flagged` = lines status==ISSUE.

- [ ] **Step 1: Write failing test** — seed a PENDING plan (counts in approvals), a HALTED plan, an ISSUE line, an old AWAITING_PO line (overdue); assert the strip counts + triage lists.
- [ ] **Step 2: Run → fail.** **Step 3: Implement** (aggregate queries; reuse the SLA constant). **Step 4: Run → pass.** **Step 5: Commit** `feat(buy-plans): supervise_overview read model`.

---

### Task 4: Restore the Buy Plans nav, remove Reporting nav

**Files:** Modify `app/templates/htmx/partials/shared/mobile_nav.html` (nav_items ~line 33, urlToNav map line 14, badge elif line 66); Test `tests/test_buyplan_nav.py`

- [ ] **Step 1: Write failing test** — render the nav (or assert on the template via the existing nav/test harness): the nav contains a `buy-plans` item (`/v2/buy-plans` → `/v2/partials/buy-plans`, label "Buy Plans") and NO `reporting` item; the badge `elif` includes `'buy-plans'` not `'reporting'`. (If no nav unit test exists, assert via `template_response` of a page including the nav, or a static grep test in `tests/test_static_analysis.py` style.)
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — in `nav_items`, replace the `('reporting', 'Reporting', '/v2/reporting', '/v2/partials/reporting', <icon>)` tuple with `('buy-plans', 'Buy Plans', '/v2/buy-plans', '/v2/partials/buy-plans', <cart-icon-path>)` (cart icon: `M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 100 4 2 2 0 000-4z`). In `urlToNav` map: change `'/v2/buy-plans':'reporting'` → `'/v2/buy-plans':'buy-plans'`; remove `'/v2/reporting':'reporting'`. In the badge block change `{% elif id in ('requisitions', 'reporting', 'crm') %}` → `('requisitions', 'buy-plans', 'crm')`.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit.** `feat(buy-plans): restore Buy Plans nav, remove Reporting nav`

---

### Task 5: Re-point the buy-plan alert source from `reporting` → `buy-plans`

**Files:** Modify `app/services/alerts/sources/__init__.py:14`; Modify `app/routers/htmx_views.py:7302` (the `markers_for_tab(db, user, "reporting")` in `buy_plans_list_partial`); Test `tests/test_alert_source_buyplan.py` (extend)

**Interfaces:** Consumes the existing `BuyplanActionSource` (unchanged). The registry tab key + the route's `markers_for_tab` key must agree = `"buy-plans"`.

- [ ] **Step 1: Write/extend failing test** — assert `tab_for_kind(AlertKind.BUYPLAN_ACTION) == "buy-plans"` and `sources_for_tab("buy-plans")` contains the buyplan source (after importing `app.services.alerts.sources`).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — `register("buy-plans", BuyplanActionSource())` (update the comment); change `markers_for_tab(db, user, "reporting")` → `"buy-plans"` in the buy-plans route.
- [ ] **Step 4: Run → pass** (this file + `tests/test_alerts_endpoints.py` + `tests/test_alerts_primitive.py` regression).
- [ ] **Step 5: Commit.** `fix(alerts): re-point buy-plan badge to the restored Buy Plans nav`

---

### Task 6: `/v2/buy-plans` renders the role-lens hub shell

**Files:** Modify `app/routers/htmx_views.py` (the `/v2/buy-plans` full-page route + `buy_plans_list_partial` at ~7238 → render the hub); Create `app/templates/htmx/partials/buy_plans/hub.html` + `app/templates/htmx/partials/buy_plans/_metric_strip.html`; Test `tests/test_buyplan_hub_routes.py`

**Interfaces:** Consumes Tasks 1-3. The partial accepts `?lens=` (deals|orders|supervise); default resolved by `_default_lens(user)` — sales→deals, buyer→orders, manager/admin→supervise, ops-group-member→supervise. Renders the lens switcher + `_metric_strip.html` + includes the active lens partial (stubbed includes in this task; filled in Tasks 7-9).

- [ ] **Step 1: Write failing test** — `client` as sales → GET `/v2/partials/buy-plans` returns 200 + contains the three lens labels + defaults to "My Deals"; `?lens=orders` activates the orders include; an admin defaults to "Supervise". (Use the role fixtures.)
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — add `_default_lens(user)` helper; the route computes `lens`, calls the relevant `buyplan_hub` read model, renders `hub.html` (lens switcher = three `hx-get` buttons swapping `#bp-hub-body`; metric strip via the macro). Keep the existing list/detail routes working (the board lens reuses them for click-through). Follow the existing buy_plans template styling.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit.** `feat(buy-plans): role-lens hub shell + lens routing`

---

### Task 7: My Orders — buyer per-line PO queue (inline confirm/issue)

**Files:** Create `app/templates/htmx/partials/buy_plans/_orders_queue.html`; Modify `htmx_views.py` (an orders partial route `GET /v2/partials/buy-plans/orders`); Test `tests/test_buyplan_orders_route.py` + `e2e/` Playwright spec
**Interfaces:** Consumes `buyplan_hub.buyer_line_queue`. Each row reuses the EXISTING per-line POST routes: confirm-PO `POST /v2/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po`, issue `.../issue` (verify exact paths in `htmx_views.py` ~7027/7095). One PO per item — a `PO# + ship-date + Confirm` form per row; `Flag issue` inline. Kicked-back rows render red + first (the queue is already sorted). After confirm, the row's htmx swap removes it from the queue.

- [ ] **Step 1: Write failing route test** — seed a buyer with an AWAITING_PO line on an ACTIVE plan; GET the orders partial → 200, contains the MPN, vendor, an input named `po_number`, and the confirm action URL; a kicked-back line shows the rejection note + a red marker class.
- [ ] **Step 2: Run → fail.** **Step 3: Implement** the route + `_orders_queue.html` (rows from `buyer_line_queue`; inline forms posting to the existing confirm/issue routes; empty state "You're all caught up"). Match `buy_plans/list.html` styling + the existing per-line confirm form in `detail.html` (lines ~240-371) so the action contract is identical. **Step 4: Run → pass.**
- [ ] **Step 5: Playwright** (`e2e/` spec, per the htmx render-verify rule): authenticated buyer → My Orders → enter a PO# inline → Confirm → the row leaves the queue + no console errors. **Step 6: Commit** `feat(buy-plans): buyer My Orders PO queue`.

---

### Task 8: My Deals — sales stage board

**Files:** Create `app/templates/htmx/partials/buy_plans/_board.html`; Modify `htmx_views.py` (board partial `GET /v2/partials/buy-plans/board`); Test `tests/test_buyplan_board_route.py`
**Interfaces:** Consumes `buyplan_hub.deals_board(scope="mine" for sales, "all" for supervise)`. Cards show customer · value · margin (color-coded like list.html) · blocker · PO-progress meter; `needs_my_action` cards pin to the top of their column with a highlight. Card click → existing `/v2/partials/buy-plans/{id}` detail.

- [ ] **Step 1: Write failing route test** — sales user with two deals in different stages → GET board → 200, both cards present in the right columns, value/margin shown, a rejected deal shows "resubmit" + a pin marker.
- [ ] **Step 2: Run → fail.** **Step 3: Implement** route + `_board.html` (4 columns; cards; click-through hx-get to detail). **Step 4: Run → pass.** **Step 5: Commit** `feat(buy-plans): sales My Deals stage board`.

---

### Task 9: Supervise — manager strip + triage + ops verify

**Files:** Create `app/templates/htmx/partials/buy_plans/_supervise_extras.html`; Modify `htmx_views.py` (supervise lens uses the board scope=all + this extras partial); Test `tests/test_buyplan_supervise_route.py`
**Interfaces:** Consumes `buyplan_hub.supervise_overview`. Renders the metric strip + a "needs attention" triage (approvals with inline Approve/Reject posting to the existing `.../approve` route; halted; overdue POs; flagged-issue lines with resolve actions reusing existing routes); ops-group members also see SO/PO verify actions (reuse `.../verify-so`, `.../lines/{id}/verify-po`). Gate the manager actions behind the existing role check (manager/admin); ops behind VerificationGroupMember.

- [ ] **Step 1: Write failing route test** — manager → GET supervise lens → 200, strip counts present, a PENDING plan appears in the approvals triage with an inline approve action URL; a non-manager does NOT see approve actions.
- [ ] **Step 2: Run → fail.** **Step 3: Implement.** **Step 4: Run → pass.** **Step 5: Commit** `feat(buy-plans): manager Supervise lens (triage + ops verify)`.

---

### Task 10: Notification re-tiering (urgent vs routine)

**Files:** Modify `app/services/buyplan_notifications.py` (the `notify_*` dispatchers); Test `tests/test_buyplan_notification_tiers.py`
**Interfaces:** Introduce a single dispatch helper `_dispatch(event, *, urgent: bool, recipients, email_fn, teams_fn, inapp_fn)`; URGENT events (new assignment via `notify_approved`/auto-approve buyer tasks, `notify_rejected` (SO/plan kickback to sales), the PO-reject path to the buyer, `notify_submitted` (approval-needed to managers), SO/PO verification-needed) send email+Teams+in-app; ROUTINE (`notify_so_verified`, `notify_po_confirmed` to ops as FYI, `notify_completed`) send in-app only (no email/Teams).

- [ ] **Step 1: Write failing test** — monkeypatch `_send_email`/`_teams_channel`/`_teams_dm` to record calls; trigger each event; assert urgent events called all three channels and routine events called in-app only (zero email/Teams). (Mock at the source per the test conventions.)
- [ ] **Step 2: Run → fail.** **Step 3: Implement** the tiering (the channel functions already exist — route per event). Keep existing payload builders. **Step 4: Run → pass** (+ regression `tests/test_buyplan_notifications.py`, `test_buyplan_v3_notifications.py`). **Step 5: Commit** `feat(buy-plans): two-tier notifications (urgent assignments+kickbacks)`.

---

### Task 11: Remove Reporting page; fold analytics into contextual chips

**Files:** Modify `app/routers/htmx_views.py:284` (remove `/v2/reporting`) + `app/routers/crm/views.py:120` (remove `reporting_dashboard`); Delete `app/templates/htmx/partials/reporting/{dashboard,pipeline,coverage}.html`; Modify the Sales Hub workspace template (add a slim pipeline strip via a new `_pipeline_chip.html` macro fed by `forecast_service.pipeline_summary`) + the CRM `_account_list.html` (add a coverage % chip fed by `reporting_service.coverage_report`); keep `forecast_service`/`reporting_service`. Test `tests/test_reporting_removed.py`

- [ ] **Step 1: Write failing test** — GET `/v2/reporting` → 404/redirect (gone); the Sales Hub partial contains the pipeline chip (open/weighted value); the CRM account-list contains a coverage figure. `forecast_service`/`reporting_service` unit tests still pass.
- [ ] **Step 2: Run → fail.** **Step 3: Implement** — delete the route+templates; add the two compact chips (one line each, non-dominating). **Step 4: Run → pass** (+ regression on any reporting tests — update/remove them). **Step 5: Commit** `feat(reporting): fold analytics into contextual chips; remove Reporting page`.

---

### Task 12: Verify + consolidate

**Files:** APP_MAP docs; no new code.
- [ ] **Step 1:** Full suite `TESTING=1 PYTHONPATH=$(pwd) pytest tests/ -q` — green (note any pre-existing main failures vs base).
- [ ] **Step 2:** `npm run lint && npm run build` (the hub adds no new JS, but confirm clean); `pre-commit run --all-files` on changed files.
- [ ] **Step 3:** `/code-review` (adversarial) over the diff; fix real findings; `/simplify`.
- [ ] **Step 4:** Browser/live-verify (isolated instance, per the comm-ledger recipe): buyer confirms a PO inline → leaves queue + deal advances; lens switch; no console errors.
- [ ] **Step 5:** Update `docs/APP_MAP_ARCHITECTURE.md` (buyplan_hub + the hub routes), `APP_MAP_INTERACTIONS.md` (the role-lens flow + notification tiers + reporting-fold). Commit.

## Self-Review
- **Spec coverage:** nav restore (T4) ✓, alert re-point (T5) ✓, hub shell+lenses (T6) ✓, buyer queue (T1,T7) ✓, sales board (T2,T8) ✓, supervise+ops (T3,T9) ✓, notification tiers (T10) ✓, reporting fold (T11) ✓, verify+APP_MAP (T12) ✓. Migration-free ✓ (waiting-on-vendor deferred, not in plan). Live-update polling: fold into T7/T8 partials (hx-trigger every 60s on the active lens) — noted in spec; add to those templates.
- **Placeholders:** UI tasks specify files+contracts+tests and defer pixel HTML to existing-pattern-following (codebase convention) — acceptable per writing-plans "follow established patterns"; the testable logic (T1-3, T10) carries real test specs.
- **Type consistency:** read-model dict keys are defined once per task interface and consumed by the matching template task; tab key `"buy-plans"` consistent across T4/T5/T6.
