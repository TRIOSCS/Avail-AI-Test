# Approvals Module Shell (SP-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-frame the "Buy Plans" Deal Hub as an **Approvals** module with lifecycle stage tabs, re-homing existing surfaces with no new flows or behavior changes.

**Architecture:** Pure UI/routing restructure. The full-page `v2_page` loader gains a `/v2/approvals` route (old `/v2/buy-plans` 302s to it); the hub shell's lens switcher becomes 5 stage tabs; each tab body composes existing partial bodies (board / orders / resource / supervise) plus a shared per-gate "Pending approvals" section reusing `services/approvals/queue.build_queue_view`. No model/DB/migration changes.

**Tech Stack:** FastAPI, HTMX 2, Alpine 3, Jinja2, Tailwind; pytest (`-n auto`, in-memory SQLite, `TESTING=1`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-27-approvals-module-sp1-shell-design.md`.
- Tabs (lens keys): `sales_orders`, `buy_plans`, `purchase_orders`, `prepayments`, `supervise`.
- Tab labels: Sales Orders / Buy Plans / Purchase Orders / Vendor Prepayments / Supervise.
- Keep `?lens=` as the query-param name (already threaded+validated by `v2_page`).
- Internal identifiers (`buy_plans/` template dir, `buyplan_hub`, the `"buy-plans"` nav/alert key, `_base_ctx(... "buy-plans")`) stay unchanged — only the user-facing label + URL change.
- Approvals stay org-wide among approvers; per-gate section gated by `can_approve_<gate>`.
- Run pytest INSIDE the worktree (cwd resolves templates); `TESTING=1 PYTHONPATH=<worktree>`.
- Full-page route tests need the `nonadmin_client` fixture (session cookie), not the `require_user`-override `client`.
- No dead "New" buttons — submission flows are SP-2/SP-3.
- After code change: update `docs/APP_MAP_ARCHITECTURE.md` + `docs/APP_MAP_INTERACTIONS.md`.

---

### Task 1: `/v2/approvals` route + 302 back-compat + lens threading

**Files:**
- Modify: `app/routers/htmx_views.py` — `v2_page` (≈364–474): add `/v2/approvals` + `/v2/approvals/{bp_id:int}` decorators; add `"approvals"` to `_VIEW_SEGMENTS` (before `requisitions`); in the `current_view == "buy-plans"` branch, also match `"approvals"` and thread `?lens=` validated against the new keys → `/v2/partials/approvals`. Add a `GET /v2/buy-plans` → `302 /v2/approvals` (preserving `?lens=`) — implement as a dedicated route registered BEFORE `v2_page`'s `/v2/buy-plans` decorator is removed, OR keep `v2_page` serving both and 302 only the bare path. Chosen: register `/v2/approvals*` on `v2_page`, and replace the `/v2/buy-plans*` decorators with a small `buy_plans_legacy_redirect` route returning `RedirectResponse(f"/v2/approvals{qs}", 302)`.
- Test: `tests/test_approvals_module_shell.py` (new).

**Interfaces:**
- Produces: `GET /v2/approvals` (full page, threads `?lens=`); `GET /v2/buy-plans` → 302 `/v2/approvals`.
- Consumes: existing `get_user`, `_VIEW_ACCESS`, `user_has_access`, `_MODULE_ENTRY_URLS`.

- [ ] **Step 1 — failing tests** (`tests/test_approvals_module_shell.py`):
```python
def test_buy_plans_redirects_to_approvals(nonadmin_client):
    r = nonadmin_client.get("/v2/buy-plans", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/v2/approvals"

def test_buy_plans_redirect_preserves_lens(nonadmin_client):
    r = nonadmin_client.get("/v2/buy-plans?lens=buy_plans", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/v2/approvals?lens=buy_plans"

def test_approvals_page_threads_lens(nonadmin_client):
    r = nonadmin_client.get("/v2/approvals?lens=buy_plans")
    assert r.status_code == 200
    assert "/v2/partials/approvals?lens=buy_plans" in r.text
```
- [ ] **Step 2 — run, expect FAIL** (`/v2/approvals` 200 but no threaded partial / `/v2/buy-plans` not 302).
- [ ] **Step 3 — implement** the decorator + `_VIEW_SEGMENTS` add + new lens-key validation tuple `("sales_orders","buy_plans","purchase_orders","prepayments","supervise")` + the legacy redirect route.
- [ ] **Step 4 — run, expect PASS** + no regression in existing buy-plan page tests (update any asserting `/v2/buy-plans` renders directly).
- [ ] **Step 5 — commit** `feat(approvals): /v2/approvals route + 302 back-compat from /v2/buy-plans`.

### Task 2: Hub shell → stage tabs + `_default_lens` + partial alias

**Files:**
- Modify: `app/routers/htmx_views.py` — `buy_plans_list_partial` (≈11024): add `GET /v2/partials/approvals` decorator (keep `/v2/partials/buy-plans` for in-flight htmx); widen the `active_lens` validation set to the 5 stage keys; rewrite `_default_lens` (buyer→`purchase_orders`, supervisor→`supervise`, else→`buy_plans`).
- Modify: `app/templates/htmx/partials/buy_plans/hub.html` — switch the lens list to the 5 stage tabs hitting `/v2/partials/approvals?lens=<key>`; keep the Alpine-reactive active pill + the `#bp-hub-body` explicit `hx-target` landmine guard; gate `supervise` on `can_supervise`.

**Interfaces:**
- Consumes: `_can_supervise`, `_can_resource`, `_can_approve_any`, `_can_see_all_deals`.
- Produces: `GET /v2/partials/approvals?lens=<stage>` shell; `_default_lens(user, db) -> stage key`.

- [ ] **Step 1 — failing tests**:
```python
def test_shell_renders_stage_tabs(nonadmin_client):
    r = nonadmin_client.get("/v2/partials/approvals")
    assert r.status_code == 200
    for key in ("sales_orders","buy_plans","purchase_orders","prepayments"):
        assert f"?lens={key}" in r.text
    assert 'hx-target="#bp-hub-body"' in r.text

def test_default_landing_buyer_purchase_orders(client):  # client user is a buyer
    r = client.get("/v2/partials/approvals")
    assert "/v2/partials/approvals/purchase-orders" in r.text or "lens=purchase_orders" in r.text
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** route alias + lens set + `_default_lens` + hub.html tabs.
- [ ] **Step 4 — run, expect PASS**; update `test_buyplan_hub_routes.py` shell tests to the new tab keys/URLs.
- [ ] **Step 5 — commit** `feat(approvals): stage-tab hub shell + role default landing`.

### Task 3: Per-tab bodies + shared pinned approvals section

**Files:**
- Create: `app/templates/htmx/partials/approvals/_pending_section.html` — renders a per-gate `QueueView` (pending rows w/ inline approve/reject for eligible recipients, reusing `approvals/_macros.html`) under a "Pending approvals (N)" heading; rendered only when passed `show=True`.
- Create thin tab bodies (or per-tab wrappers that `{% include %}` existing bodies):
  - `approvals/_tab_buy_plans.html` → pinned section (gate `buy_plan`) + `{% include buy_plans/_board.html %}`.
  - `approvals/_tab_purchase_orders.html` → pinned section (gate `purchase_order`) + orders body + resource body.
  - `approvals/_tab_sales_orders.html` → pinned section (gate `sales_order`) + neutral empty state.
  - `approvals/_tab_prepayments.html` → pinned section (gate `prepayment`) + empty state.
  - (supervise reuses existing `_render_supervise_body`.)
- Modify: `app/routers/htmx_views.py` — 5 partial routes `GET /v2/partials/approvals/<tab>` building ctx (call `build_queue_view(db, user, tab=<gate-key>)` for the pinned section + existing read models for the work surface; pass `show_pending = can_approve_<gate>(user)`).

**Interfaces:**
- Consumes: `services/approvals/queue.build_queue_view(db, user, tab)`; existing `deals_board`/`completed_archive`/`buyer_line_queue`/`team_line_queue`/`resourcing_pool_queue`.
- Produces: `GET /v2/partials/approvals/{sales-orders|buy-plans|purchase-orders|prepayments|supervise}`.

- [ ] **Step 1 — failing tests** (per tab; assert work surface + gated pending section):
```python
def test_buy_plans_tab_has_board(client, db_session, test_user, test_requisition):
    r = client.get("/v2/partials/approvals/buy-plans")
    assert r.status_code == 200
    assert "open deal" in r.text  # board metric strip

def test_pending_section_shown_only_for_approver(client, db_session, test_user):
    # test_user is a buyer without can_approve_buy_plans by default
    r = client.get("/v2/partials/approvals/buy-plans")
    assert "Pending approvals" not in r.text
```
- [ ] **Step 2 — run, expect FAIL** (routes 404).
- [ ] **Step 3 — implement** the 5 routes + templates + shared section.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit** `feat(approvals): per-stage tab bodies + pinned per-gate approval section`.

### Task 4: Nav rename

**Files:**
- Modify: `app/templates/htmx/partials/shared/mobile_nav.html:33` — entry label `Buy Plans`→`Approvals`, href `/v2/buy-plans`→`/v2/approvals`, partial `/v2/partials/buy-plans`→`/v2/partials/approvals` (keep id `'buy-plans'`). The JS active-path map already maps `/v2/approvals`→`buy-plans` (line 14) — leave it.

- [ ] **Step 1 — failing test**:
```python
def test_nav_label_is_approvals(client):
    r = client.get("/v2/partials/requisitions") or client.get("/v2")
    # nav is in base shell; assert via a page that includes mobile_nav
```
(Use whichever page test already asserts nav; assert `>Approvals<` present and the href is `/v2/approvals`.)
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** nav entry edit.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit** `feat(approvals): rename Buy Plans nav item to Approvals`.

### Task 5: Retire combined approvals lens + repoint legacy queue redirect

**Files:**
- Modify: `app/routers/htmx_views.py` — remove the standalone `buy_plans_approvals_partial` lens route (its 4-sub-tab queue), now superseded by per-tab pinned sections; repoint the legacy `/v2/approvals/queue` redirect target to `/v2/approvals?lens=buy_plans`.
- Delete/retire: `app/templates/htmx/partials/approvals/_queue.html` only if no longer referenced (else leave).

- [ ] **Step 1 — failing test**:
```python
def test_legacy_queue_redirects_to_first_tab(client):
    r = client.get("/v2/approvals/queue", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/v2/approvals?lens=buy_plans"
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** redirect repoint + remove the dead lens route; grep for any caller of the old route/template and update.
- [ ] **Step 4 — run, expect PASS** + full suite.
- [ ] **Step 5 — commit** `refactor(approvals): retire combined approvals lens; repoint legacy queue redirect`.

### Task 6: APP_MAP docs + full-suite + pre-commit

**Files:**
- Modify: `docs/APP_MAP_ARCHITECTURE.md` (the Buy Plans row) + `docs/APP_MAP_INTERACTIONS.md` (the Deal-Hub read-flow section) → describe the Approvals module + stage tabs + the `/v2/buy-plans`→`/v2/approvals` redirect.

- [ ] **Step 1** — update both APP_MAP docs.
- [ ] **Step 2** — `pre-commit run --files <changed>` (twice if docformatter mutates).
- [ ] **Step 3** — full suite `TESTING=1 PYTHONPATH=<worktree> pytest tests/ -q` → green.
- [ ] **Step 4 — commit** `docs(approvals): APP_MAP updates for the Approvals module shell`.

---

## Self-Review

- **Spec coverage:** nav+route (T1,T4), tabs+shell (T2), tab contents+in-tab layout (T3), default landing (T2), gating (T3), retire combined lens (T5), tests+docs (T6). ✓ all spec sections mapped.
- **Placeholder scan:** none — each task has concrete tests + edits. Template Jinja written during execution against the existing `_macros.html`/`_board.html` patterns (no new conventions).
- **Type consistency:** lens keys are the single tuple `("sales_orders","buy_plans","purchase_orders","prepayments","supervise")` used in v2_page threading (T1), `active_lens` validation + hub tabs (T2), and per-tab routes (T3). Gate keys map: buy_plans→buy_plan, sales_orders→sales_order, purchase_orders→purchase_order, prepayments→prepayment.
