# Settings Menu Refinement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the full Settings menu safe, refined, and ready for non-admin group testing — gate admin surfaces, add a shared feedback/safety backbone, and bring every tab (Profile, System, Connectors, Data Ops, Ops Group, Tickets) up to that bar.

**Architecture:** Reuse existing conventions verbatim (server `HX-Trigger: showToast`, global `htmx:responseError` toast, `hx-confirm`, house empty-state/card styles). Tester-facing Profile becomes functional (editable name, 8×8 extension, two notification toggles). System gets real typed controls wired to actually control behavior. Tickets is gated admin-only and kept inside the settings shell with its broken wiring fixed.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind 3.x. Tests: pytest (`TESTING=1`, in-memory SQLite, xdist). Spec: `docs/superpowers/specs/2026-06-24-settings-menu-refinement-design.md`.

## Global Constraints

- **Worktree:** all work in `/root/availai/.claude/worktrees/settings-refine` (branch `feat/settings-refine`). Run pytest from **inside** the worktree: `TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/settings-refine pytest ... -p no:cacheprovider`.
- **Stack is HTMX + Alpine + Jinja2 — never React.** Server-render + HTMX swap.
- **No band-aids** — root-cause only. **Tests with every change.** **Loguru, never print().**
- **Toast contract:** `$store.toast` has `message`, `type` (`success|error|warning|info`), `show` (boolean field). Server success feedback = `response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "...", "type": "success"}})`. Error feedback = return 4xx with JSON `{"error": "..."}` (global handler auto-toasts).
- **JSON error contract:** `{"error": "...", "status_code": N, "request_id": "..."}` — tests assert `["error"]`, not `["detail"]`.
- **DB access:** `db.get(Model, id)`, never `db.query().get()`. Status values via `app/constants.py` enums.
- **Migrations:** Alembic only (no DDL in startup.py). Revision id ≤32 chars. Run upgrade→downgrade→upgrade + `alembic heads` (single head). Migration **149** claimed in `MIGRATION_NUMBERS_IN_FLIGHT.txt`, chains onto `148_archive_dnc`.
- **New file header comment** on every created file (what it does / what calls it / depends on).
- **Every code change updates the relevant `docs/APP_MAP_*.md`** (Task 13).
- **Git:** merge (never rebase) on the pushed branch. `gh pr edit` is broken — use `gh api -X PATCH`.
- **Tailwind:** stay within safelisted families (`brand/emerald/rose/amber/slate/gray/blue/...`) + `.btn-*/.badge-*` component classes; verify new classes survive the build.

### Test fixtures (from `tests/conftest.py`)
- `client` — `TestClient` authed as `test_user` (role `buyer`); **overrides `require_admin` to pass**, so use it for admin-capable endpoints.
- `admin_user` — a real `role="admin"` user. `test_user` — `role="buyer"`. `db_session` — the test session.
- For **403 gating tests** you need a client where `require_admin` runs for real. Task 2 adds a `nonadmin_client` fixture to `conftest.py` that overrides `get_db` + `require_user` (→ buyer) but **leaves `require_admin` real**.

---

## WAVE 1 — Backbone + gating + safety (test-readiness blockers)

### Task 1: Shared settings toast helper

**Files:**
- Modify: `app/routers/htmx_views.py` (add one module-level helper near the existing toast helper `:13146`)
- Test: `tests/test_settings_toast_helper.py` (create)

**Interfaces:**
- Produces: `settings_toast(response: Response, message: str, kind: str = "success") -> None` — sets `response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": kind}})`. Later tasks call this from settings mutation handlers.

- [ ] **Step 1 — failing test** `tests/test_settings_toast_helper.py`:
```python
import json
from starlette.responses import Response
from app.routers.htmx_views import settings_toast

def test_settings_toast_sets_hx_trigger():
    r = Response()
    settings_toast(r, "Saved", "success")
    payload = json.loads(r.headers["HX-Trigger"])
    assert payload["showToast"] == {"message": "Saved", "type": "success"}

def test_settings_toast_defaults_to_success():
    r = Response()
    settings_toast(r, "Done")
    assert json.loads(r.headers["HX-Trigger"])["showToast"]["type"] == "success"
```
- [ ] **Step 2 — run, expect ImportError/FAIL:** `... pytest tests/test_settings_toast_helper.py -v`
- [ ] **Step 3 — implement** `settings_toast` in `htmx_views.py` (mirror the existing `:13146-13148` helper; reuse `json` already imported).
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(settings): shared settings_toast HX-Trigger helper`

---

### Task 2: Gate Tickets admin-only (button + route deps)

**Files:**
- Modify: `app/templates/htmx/partials/settings/index.html:23` (move Tickets `tab_button(...)` inside the `{% if is_admin %}` block, after the `ops_group` button)
- Modify: `app/routers/htmx_views.py` — workspace view `:16149`, list view `:16161`, detail view `:16211`: change dep `require_user` → `require_admin`
- Modify: `app/routers/error_reports.py:267` — screenshot endpoint dep `require_user` → `require_admin`
- Modify: `tests/conftest.py` — add `nonadmin_client` fixture
- Test: `tests/test_tickets_gating.py` (create)

**Interfaces:**
- Consumes: `require_admin` (already imported in `htmx_views.py`; confirm import in `error_reports.py`).
- Produces: `nonadmin_client` fixture (a `TestClient` whose `require_admin` is unmocked).

- [ ] **Step 1 — add fixture** to `tests/conftest.py`:
```python
@pytest.fixture()
def nonadmin_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient authed as a non-admin buyer with require_admin LEFT REAL (for 403 gating tests)."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)
```
- [ ] **Step 2 — failing tests** `tests/test_tickets_gating.py`:
```python
def test_nonadmin_blocked_from_tickets_workspace(nonadmin_client):
    assert nonadmin_client.get("/v2/partials/trouble-tickets/workspace").status_code == 403

def test_nonadmin_blocked_from_tickets_list(nonadmin_client):
    assert nonadmin_client.get("/v2/partials/trouble-tickets/list").status_code == 403

def test_admin_can_load_tickets_workspace(client):
    assert client.get("/v2/partials/trouble-tickets/workspace").status_code == 200

def test_nonadmin_can_still_submit_report(nonadmin_client):
    # the floating "Report a Problem" form must stay open to all
    assert nonadmin_client.get("/api/trouble-tickets/form").status_code == 200

def test_tickets_tab_button_hidden_for_nonadmin(nonadmin_client):
    # settings index renders with is_admin False -> no Tickets tab button
    html = nonadmin_client.get("/v2/partials/settings").text
    assert "trouble-tickets/workspace" not in html
```
- [ ] **Step 3 — run, expect FAIL** (workspace/list currently 200 for non-admin; button present).
- [ ] **Step 4 — implement** the dep changes (3 view fns in `htmx_views.py`, screenshot in `error_reports.py`) and move the Tickets `tab_button` inside `{% if is_admin %}`. Verify `require_admin` is imported in `error_reports.py`.
- [ ] **Step 5 — run, expect PASS.** Also run existing `tests/test_connectors_settings.py tests/test_admin_settings.py` to confirm no regressions.
- [ ] **Step 6 — commit:** `feat(tickets): gate workspace/list/detail/screenshot admin-only + hide tab from non-admins`

---

### Task 3: Fix Tickets wiring (Analyze, Open filter, detail toast, in-shell drill-in)

**Files:**
- Modify: `app/routers/error_reports.py` — `analyze` `:454-456` (return list partial, drop dead `HX-Trigger`); extract list-building into a shared helper
- Modify: `app/routers/htmx_views.py` — `trouble_tickets_list` `:16158-16204` (accept logical `status="open"` → `status in (submitted, in_progress)`; share the helper with analyze); full-page route `:319` → redirect to settings with Tickets active
- Modify: `app/templates/htmx/partials/settings/_macros.html` `tab_button` — add optional `target="#settings-content"` param
- Modify: `app/templates/htmx/partials/tickets/workspace.html` — Open pill `('submitted','Open')` → `('open','Open')`
- Modify: `app/templates/htmx/partials/tickets/_row.html:1-2`, `detail.html:6` — retarget `#main-content` → `#settings-content`, drop `hx-push-url`
- Modify: `app/templates/htmx/partials/tickets/detail.html:24-37` — gate status PATCH toast on `r.ok` + `.catch`
- Test: `tests/test_tickets_wiring.py` (create), extend `tests/test_tickets_gating.py`

**Interfaces:**
- Produces: `_build_ticket_list_context(db, status: str | None) -> dict` (shared by `trouble_tickets_list` and `analyze`); recognizes `status == "open"` as the `(submitted, in_progress)` set using `TicketStatus` from `app/constants.py`.

- [ ] **Step 1 — failing tests** `tests/test_tickets_wiring.py` (use `client` = admin-capable; seed tickets via the TroubleTicket model — follow the create pattern in `error_reports.py:_create_ticket` / existing ticket tests):
```python
# Seed one 'submitted' and one 'in_progress' ticket, then:
def test_open_filter_includes_in_progress(client, db_session):
    # ... seed submitted + in_progress tickets ...
    html = client.get("/v2/partials/trouble-tickets/list?status=open").text
    # both tickets' numbers appear
    assert "<submitted_number>" in html and "<in_progress_number>" in html

def test_analyze_returns_nonempty_list_partial(client, db_session, monkeypatch):
    # stub the AI grouping call so analyze runs offline; assert response body is the
    # rendered list (contains ticket markup), NOT empty string, and has no ticketsUpdated trigger
    resp = client.post("/api/trouble-tickets/analyze")
    assert resp.status_code == 200
    assert resp.text.strip() != ""
    assert "ticketsUpdated" not in resp.headers.get("HX-Trigger", "")
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:** extract `_build_ticket_list_context`; teach it the `open` logical filter; make `analyze` render+return the list partial via that context (drop the `HTMLResponse("")` + `HX-Trigger`). Template edits: `tab_button` `target` param; retarget row/detail to `#settings-content` (no push-url); Open pill value; detail PATCH `r.ok` gating per spec §3.6.5. Redirect `/v2/trouble-tickets` full-page route to the settings page with `tab=tickets`.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `fix(tickets): analyze returns grouped list, Open filter includes in_progress, in-shell drill-in, honest status toast`

---

### Task 4: Ops Group safety guards + feedback

**Files:**
- Modify: `app/routers/admin/buy_plan_ops.py` — `toggle_ops_member` (last-member guard, self-removal guard, success toast via `settings_toast`)
- Modify: `app/templates/htmx/partials/settings/ops_group.html` — `hx-confirm` on Add; `data-loading-disable` on Add/Remove; humanize Role; fix "Member since" on removed users
- Test: `tests/test_ops_group_guards.py` (create)

**Interfaces:**
- Consumes: `settings_toast` (Task 1).

- [ ] **Step 1 — failing tests** `tests/test_ops_group_guards.py` (`client` is admin-capable; `toggle` posts `{"user_id": N}`):
```python
def test_cannot_remove_last_active_member(client, db_session, admin_user):
    # make admin_user the ONLY active ops-group member, then try to remove -> 4xx + error
    # (seed one active OpsGroupMember for admin_user)
    resp = client.post("/api/admin/ops-group/toggle", data={"user_id": admin_user.id})
    assert resp.status_code == 400
    assert "error" in resp.json()

def test_cannot_remove_self(client, db_session, test_user):
    # test_user is the authed user AND an active member alongside others -> self-removal blocked
    ...
    assert resp.status_code == 400 and "yourself" in resp.json()["error"].lower()

def test_add_member_succeeds_with_toast(client, db_session, sales_user):
    resp = client.post("/api/admin/ops-group/toggle", data={"user_id": sales_user.id})
    assert resp.status_code == 200  # added; HX-Trigger showToast present
    assert "showToast" in resp.headers.get("HX-Trigger", "")
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** guards in `toggle_ops_member`: before deactivating, if this would drop active-member count to 0 → `JSONResponse({"error": "At least one active member is required to verify buy plans.", "status_code": 400, ...}, status_code=400)`; if `user_id == current_user.id` and action is removal → 400 `{"error": "You can't remove yourself from the verification group."}`. On success attach `settings_toast`. Template: `hx-confirm` on Add (brand chip), `data-loading-disable`, role humanization, conditional Member-since.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(ops-group): guard last-member/self-removal, confirm Add, success toast, role/status polish`

---

### Task 5: Connectors confirmations + feedback + empty state + polish

**Files:**
- Modify: `app/templates/htmx/partials/settings/connectors.html` (+ `_connector_macros.html`) — empty state; `hx-confirm` on Clay Disconnect + on disabling a *live* source; unify status vocabulary; "Test all" aggregate line
- Modify: `app/routers/sources.py` (save key/credentials + activate handlers) — `settings_toast` on success
- Modify: `app/routers/clay_oauth.py` (disconnect) — confirm is client-side; ensure success toast
- Modify: connector group builder — consolidate duplicated `_DEAD` set into one shared constant
- Test: extend `tests/test_connectors_settings.py`

- [ ] **Step 1 — failing tests** (extend `tests/test_connectors_settings.py`):
```python
def test_connectors_empty_state_when_no_sources(client, db_session):
    # with no ApiSource rows, the tab shows an empty-state message, not a bare header
    html = client.get("/v2/partials/settings/connectors").text
    assert "No connectors" in html  # exact copy per implementation

def test_save_key_emits_success_toast(client, db_session):
    # save a key for a seeded source -> response carries showToast HX-Trigger
    ...
    assert "showToast" in resp.headers.get("HX-Trigger", "")
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** per spec §3.3. For "disable a *live* source" confirm: add `hx-confirm` only when the card state is live (Jinja conditional on the enable toggle). Disconnect `hx-confirm="Disconnect Clay enrichment for everyone? This stops enrichment until reconnected."`. Add empty-state block (house style). Aggregate line after Test-all (the test-all handler already OOB-swaps cards; add a summary fragment). Consolidate `_DEAD`.
- [ ] **Step 4 — run, expect PASS** (full `tests/test_connectors_settings.py` + `tests/e2e` excluded).
- [ ] **Step 5 — commit:** `feat(connectors): empty state, destructive confirms, success toasts, test-all summary, unified status vocabulary`

---

## WAVE 2 — Profile (tester-facing) + migration

### Task 6: Migration 149 + User notification columns

**Files:**
- Modify: `app/models/auth.py` — add `notify_buyplan_email_enabled`, `notify_new_offer_alert_enabled` (both `Boolean`, `nullable=False`, `default=True`) near the 8×8 fields (`:42`)
- Create: `alembic/versions/149_user_notify_prefs.py`
- Test: `tests/test_user_notify_columns.py` (create)

- [ ] **Step 1 — failing test**:
```python
def test_user_has_notify_pref_columns_default_true(db_session):
    from app.models.auth import User
    from datetime import datetime, timezone
    u = User(email="n@trioscs.com", name="N", role="buyer", azure_id="az-notify",
             created_at=datetime.now(timezone.utc))
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    assert u.notify_buyplan_email_enabled is True
    assert u.notify_new_offer_alert_enabled is True
```
- [ ] **Step 2 — run, expect FAIL** (AttributeError).
- [ ] **Step 3 — implement** the two columns on the model. Write migration `149_user_notify_prefs.py` (revision `149_user_notify_prefs`, down_revision `148_archive_dnc`) per spec §5 (add_column with `server_default=sa.text("true")`; downgrade drops both with `IF EXISTS`).
- [ ] **Step 4 — run, expect PASS.** Then: `cd <worktree> && alembic upgrade head && alembic downgrade -1 && alembic upgrade head && alembic heads` → expect a single head.
- [ ] **Step 5 — commit:** `feat(profile): migration 149 + User notification-preference columns`

---

### Task 7: Profile mutation endpoints

**Files:**
- Modify: `app/routers/htmx_views.py` — add `POST /api/user/profile` (name + 8×8 extension), `POST /api/user/toggle-buyplan-email`, `POST /api/user/toggle-new-offer-alert` (clone `toggle_8x8` `:13837-13850`)
- Test: `tests/test_profile_endpoints.py` (create)

**Interfaces:**
- Produces: endpoints returning `settings_toast` success + re-rendered profile fragment (or 204 + toast). Name validated 1–255 non-empty (else 400 `{"error":...}`); extension ≤20 chars.

- [ ] **Step 1 — failing tests**:
```python
def test_update_display_name(client, db_session, test_user):
    resp = client.post("/api/user/profile", data={"name": "New Name", "extension": "1234"})
    assert resp.status_code == 200
    db_session.refresh(test_user)
    assert test_user.name == "New Name" and test_user.eight_by_eight_extension == "1234"

def test_blank_name_rejected(client):
    resp = client.post("/api/user/profile", data={"name": "  ", "extension": ""})
    assert resp.status_code == 400 and "error" in resp.json()

def test_toggle_buyplan_email_off(client, db_session, test_user):
    resp = client.post("/api/user/toggle-buyplan-email")
    assert resp.status_code == 200
    db_session.refresh(test_user)
    assert test_user.notify_buyplan_email_enabled is False  # default True -> toggled off

def test_toggle_new_offer_alert_off(client, db_session, test_user):
    client.post("/api/user/toggle-new-offer-alert")
    db_session.refresh(test_user)
    assert test_user.notify_new_offer_alert_enabled is False
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** the three endpoints (`require_user`), each `settings_toast` on success, 400 `{"error":...}` on bad name. Register routes near the existing user endpoints.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(profile): endpoints for editable name, 8x8 extension, notification toggles`

---

### Task 8: Profile template — polish + functional UI

**Files:**
- Modify: `app/templates/htmx/partials/settings/profile.html` (remove dup email + "coming soon"; inline-editable name; extension input; Notifications card with two toggles; unified card styling; relabel 8×8)
- Modify: `app/templates/htmx/partials/settings/_mailbox_sync_card.html` (friendly copy; disconnected empty state; friendly error)
- Test: `tests/test_profile_render.py` (create) — assert rendered HTML

- [ ] **Step 1 — failing tests**:
```python
def test_profile_has_no_coming_soon_or_dup_email(client):
    html = client.get("/v2/partials/settings/profile").text
    assert "coming soon" not in html.lower()

def test_profile_has_notification_toggles(client):
    html = client.get("/v2/partials/settings/profile").text
    assert "/api/user/toggle-buyplan-email" in html
    assert "/api/user/toggle-new-offer-alert" in html

def test_profile_has_name_edit_and_extension(client):
    html = client.get("/v2/partials/settings/profile").text
    assert "/api/user/profile" in html  # the name/extension form posts here
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** the template per spec §3.1 (Alpine inline-edit for name; bind extension; two toggle cards cloned from the 8×8 block; mailbox card copy/empty state). Use single-quoted Alpine attrs where interpolating values; no literal `"` inside double-quoted Alpine attrs.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(profile): functional, polished profile tab (editable name, extension, notifications, mailbox states)`

---

### Task 9: Notification suppression wiring (guards)

**Files:**
- Modify: `app/services/buyplan_notifications.py:145` — early-return in `_send_email()` when recipient `notify_buyplan_email_enabled` is False (keep in-app `ActivityLog`)
- Modify: `app/services/alerts/sources/offers.py` — `OfferConfirmedSource.count_for_user`/`markers_for_tab` return 0/empty when user `notify_new_offer_alert_enabled` is False
- Test: `tests/test_notification_guards.py` (create)

- [ ] **Step 1 — failing tests**:
```python
def test_buyplan_email_suppressed_when_disabled(db_session, monkeypatch):
    # spy on the Graph send; set recipient.notify_buyplan_email_enabled=False; call _send_email
    # assert the send was NOT invoked, but the ActivityLog row WAS written
    ...

def test_new_offer_badge_zero_when_disabled(db_session, ...):
    # user with notify_new_offer_alert_enabled=False -> OfferConfirmedSource count is 0
    ...
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** both guards (root-cause: gate at the send/count site; the recipient `User` must be in scope — fetch by id if only an email/id is available).
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(notifications): honor per-user buyplan-email and new-offer-alert preferences`

---

## WAVE 3 — System (curated, real controls)

### Task 10: System flag resolver + consumer rewiring + startup reconcile

**Files:**
- Modify: `app/services/admin_service.py` — add `get_effective_flag(db, key, env_default: bool) -> bool` and `get_effective_int(db, key, env_default: int) -> int`; invalidate the config cache on `set_config_value`
- Modify consumers: `app/routers/sources.py:119,771`; `app/jobs/offers_jobs.py:21`; `app/jobs/email_jobs.py:34,42,67`, `app/jobs/core_jobs.py:42,27,169`; `app/services/activity_service.py:1582` — read via resolver (env value as fallback)
- Modify: `app/startup.py` — idempotent reconcile of the 4 keys to env value when `updated_by IS NULL`
- Test: `tests/test_system_flag_resolver.py` (create)

- [ ] **Step 1 — failing tests**:
```python
def test_effective_flag_db_overrides_env(db_session):
    from app.services.admin_service import set_config_value, get_effective_flag
    set_config_value(db_session, "email_mining_enabled", "true", "admin@trioscs.com")
    assert get_effective_flag(db_session, "email_mining_enabled", env_default=False) is True

def test_effective_flag_falls_back_to_env_when_missing(db_session):
    from app.services.admin_service import get_effective_flag
    assert get_effective_flag(db_session, "no_such_key", env_default=True) is True

def test_effective_int_min_and_parse(db_session):
    from app.services.admin_service import set_config_value, get_effective_int
    set_config_value(db_session, "inbox_scan_interval_min", "45", "admin@trioscs.com")
    assert get_effective_int(db_session, "inbox_scan_interval_min", env_default=30) == 45
```
(Note: `set_config_value` 404s on unknown keys — seed the row first in the test, or test resolver directly against a seeded SystemConfig row.)
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** resolvers + cache invalidation; switch each consumer; add the startup reconcile (runtime op, no DDL) per spec §4.3.
- [ ] **Step 4 — run, expect PASS** + run the touched jobs' existing tests.
- [ ] **Step 5 — commit:** `feat(system): DB-overrides-env flag resolver + consumer rewiring + no-surprise startup reconcile`

---

### Task 11: System curated UI

**Files:**
- Modify: `app/templates/htmx/partials/settings/system.html` — replace raw table with curated typed controls from a key→meta map; hide watermark keys; collapsed read-only "Job state"; drop fake masking
- Modify: `app/routers/admin/system.py` — pass the curated meta + values; interval PUT validates `>= 5` (400 `{"error":...}`); `settings_toast` on success
- Modify: `app/routers/htmx_views.py:13785` (system view) — provide the curated context
- Test: `tests/test_system_curated.py` (create), extend `tests/test_admin_settings.py`

**Interfaces:**
- Produces: a `SYSTEM_SETTINGS_META` dict `{key: {"type": "bool"|"int", "label": str, "help": str, "group": str}}` (owned in code — `app/routers/admin/system.py` or a small module).

- [ ] **Step 1 — failing tests**:
```python
def test_system_renders_friendly_toggles(client):
    html = client.get("/v2/partials/settings/system").text
    assert "Email mining" in html and "Proactive offer matching" in html

def test_system_hides_watermark_rows(client, db_session):
    # seed a teams_calls_last_poll row -> it must NOT appear as an editable control
    html = client.get("/v2/partials/settings/system").text
    assert "teams_calls_last_poll" not in html  # not in the editable section

def test_interval_below_min_rejected(client, db_session):
    resp = client.put("/api/admin/config/inbox_scan_interval_min", json={"value": "2"})
    assert resp.status_code == 400 and "error" in resp.json()
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** the meta map + curated template (toggles + number input + descriptions; collapsed read-only job-state) + interval validation + success toast.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(system): curated typed settings UI (toggles + interval), hide internal watermarks, drop cosmetic masking`

---

## WAVE 4 — Data Ops polish

### Task 12: Data Ops scan-error state + merge disambiguation + refresh

**Files:**
- Modify: the Data Ops route in `app/routers/htmx_views.py` — distinguish scan-failure from clean (don't swallow into empty state)
- Modify: `app/templates/htmx/partials/settings/data_ops.html` + `_macros.html` (`merge_button`) — error banner; vendor "suggested keep" hint; keeper vs drop button styling; consistent refresh; company/customer wording; post-merge list refresh + company-row `x-data` merged guard
- Test: extend `tests/` (create `tests/test_data_ops.py`)

- [ ] **Step 1 — failing tests**:
```python
def test_scan_error_shows_error_not_empty(client, db_session, monkeypatch):
    # force the vendor dedup scan to raise -> response shows an error state, NOT
    # "No duplicate vendors found"
    ...
    assert "couldn't" in html.lower() or "error" in html.lower()
    assert "No duplicate vendors found" not in html
```
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** per spec §3.4 (separate `scan_failed` flag in context → distinct error block; merge button styling/hint; refresh after merge).
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(data-ops): honest scan-error state, clearer merge direction, post-merge refresh`

---

## WAVE 5 — Docs, review, verify, ship

### Task 13: Update APP_MAP docs

**Files:**
- Modify: `docs/APP_MAP_DATABASE.md` (two new User columns), `docs/APP_MAP_INTERACTIONS.md` (System resolver flow, notification suppression, Tickets gating), `docs/APP_MAP_ARCHITECTURE.md` (new endpoints if it enumerates them)

- [ ] **Step 1** — add the new columns/endpoints/flows to the relevant maps (no tests).
- [ ] **Step 2 — commit:** `docs(app-map): settings-refine columns, endpoints, System resolver, tickets gating`

---

### Task 14: Full verification + review fleet + deploy

- [ ] **Step 1 — pre-commit:** `cd <worktree> && pre-commit run --all-files` (run twice if docformatter mutates). Fix all.
- [ ] **Step 2 — full suite:** `TESTING=1 PYTHONPATH=<worktree> pytest tests/ -q` → all green (xdist parallel catches shared-state regressions).
- [ ] **Step 3 — frontend build smoke:** `npm run build` (verify Tailwind classes survive; check the bundle smoke test passes).
- [ ] **Step 4 — push branch + open PR:** plain `git push -u origin feat/settings-refine`; `gh pr create`.
- [ ] **Step 5 — review fleet:** run the saved `pr-review-fleet` (inline values, per CLAUDE.md caveat) → fix ALL findings immediately.
- [ ] **Step 6 — merge to main** (merge, not rebase; resolve conflicts root-cause), then `./deploy.sh`.
- [ ] **Step 7 — live-verify** on staging real Postgres: non-admin sees only Profile (no admin tabs, no Tickets); Profile name/extension/toggles persist; System toggle flips a flag; Ops Group last-member guard returns the error toast; Tickets Analyze refreshes the list. Capture results.

---

## Self-Review (plan vs spec)

- **Spec §2.1 backbone** → Task 1 (toast helper), error/confirm/empty patterns applied per-tab in Tasks 3–5, 8, 11, 12. ✓
- **§3.1 Profile** → Tasks 6,7,8,9. ✓ **§3.2 System** → Tasks 10,11. ✓ **§3.3 Connectors** → Task 5. ✓ **§3.4 Data Ops** → Task 12. ✓ **§3.5 Ops Group** → Task 4. ✓ **§3.6 Tickets** → Tasks 2,3. ✓
- **§4.1/4.2 notification wiring** → Task 9. **§4.3 System real flags** → Task 10. ✓
- **§5 migration 149** → Task 6. ✓ **§6 testing** → tests in every task + Task 14. ✓ **§7 sequencing** → Waves 1–5 match. ✓
- **Placeholder scan:** template-edit steps reference exact spec sections + file:line patterns to copy (live-file read required per CLAUDE.md) rather than vague instructions; test code uses real fixtures (`client`, `nonadmin_client`, `db_session`, `admin_user`). Ticket-seeding and Graph-send-spy specifics are left to the implementer to mirror existing patterns in `error_reports.py` / buyplan-notification tests — flagged inline, not silently omitted.
- **Type consistency:** `settings_toast(response, message, kind)`, `_build_ticket_list_context(db, status)`, `get_effective_flag(db, key, env_default)`, `get_effective_int(db, key, env_default)`, `SYSTEM_SETTINGS_META` — names used consistently across tasks. ✓
