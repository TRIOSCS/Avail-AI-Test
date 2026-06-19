# Task 4 + Task 5 — Nav swap (Buy Plans in / Reporting out) + Alert source re-point

All paths are relative to the worktree root `/root/availai/.claude/worktrees/buy-plan-deal-hub`.
These two tasks MUST land in one atomic commit — the nav swap and the test file that asserts
the OLD state contradict each other, so a split leaves CI red between commits.

Canonical task text + intended code: `docs/superpowers/plans/2026-06-19-buy-plan-deal-hub.md`
(read Tasks 4 and 5 there). This brief carries the CURRENT-main corrections that override any
stale line numbers in the plan — trust the line numbers here.

## Task 4 — restore the Buy Plans primary-nav item, remove the Reporting item

### `app/templates/htmx/partials/shared/mobile_nav.html`
1. Line ~14, `urlToNav` Alpine map: it currently contains `'/v2/reporting':'reporting','/v2/buy-plans':'reporting'`.
   Replace both with a single `'/v2/buy-plans':'buy-plans'` (remove the `/v2/reporting` entry, repoint buy-plans to its own id).
2. Line ~33, `nav_items` Jinja `{% set %}` tuple list: replace the reporting tuple
   `('reporting', 'Reporting', '/v2/reporting', '/v2/partials/reporting', <bar-chart-svg-path>)`
   with `('buy-plans', 'Buy Plans', '/v2/buy-plans', '/v2/partials/buy-plans', <cart-svg-path>)`.
   Cart icon path (same one used in the old reporting/dashboard.html):
   `M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 100 4 2 2 0 000-4z`
3. Line ~66, badge `{% elif id in (...) %}`: change `'reporting'` → `'buy-plans'`.

### `app/routers/htmx_views.py`
4. Line ~112, `_NAV_ID_ALIAS = {"buy-plans": "reporting", "quotes": "reporting"}`:
   remove ONLY the `"buy-plans"` key → `_NAV_ID_ALIAS = {"quotes": "reporting"}`.
   KEEP the `"quotes": "reporting"` entry (quotes nav is still demoted under reporting-era logic).
5. Line ~291, inside the multi-route `v2_page`: remove the `@router.get("/v2/reporting", response_class=HTMLResponse)`
   decorator line ONLY (the function body stays — it serves other segments).
6. Lines ~317–321, `_VIEW_SEGMENTS` tuple: remove the `"reporting",` entry.

### Tests — atomic with the above
7. DELETE `tests/test_reporting.py` entirely. It asserts the OLD state (reporting present in nav,
   `_NAV_ID_ALIAS["buy-plans"] == "reporting"`, `/v2/partials/reporting` returns 200). Keeping it = 8+ failures.
8. CREATE `tests/test_buyplan_nav.py` with the inverse assertions:
   - `mobile_nav.html` source contains `('buy-plans',` and does NOT contain `('reporting',`.
   - `urlToNav` contains `'/v2/buy-plans':'buy-plans'` and not `'/v2/reporting'`.
   - badge elif contains `'buy-plans'`.
   - `_NAV_ID_ALIAS` has no `"buy-plans"` key and `_NAV_ID_ALIAS.get("quotes") == "reporting"`.
   - `GET /v2/buy-plans` → 200; `GET /v2/reporting` → 404.
   Mirror the style/fixtures of the deleted `tests/test_reporting.py` (read it before deleting) and of
   `tests/test_alert_source_buyplan.py` for the client/auth fixtures.
9. `tests/test_browser_back_navigation.py` (~line 69): if it asserts a nav section id `"reporting"`,
   change it to `"buy-plans"`. Read lines 60–80 first; if it does not reference `"reporting"`, leave it.

## Task 5 — re-point the buy-plan alert source from the `reporting` tab key to `buy-plans`

### `app/services/alerts/sources/__init__.py`
10. Line ~14: `register("reporting", BuyplanActionSource())` → `register("buy-plans", BuyplanActionSource())`.
    Update the adjacent comment (currently says Buy Plans lives under Reporting nav) to reflect that
    Buy Plans is now its own primary nav tab.

### `app/routers/htmx_views.py`
11. Line ~7301: `alert_markers = markers_for_tab(db, user, "reporting")` → `markers_for_tab(db, user, "buy-plans")`.
    Update the nearby comment (~7298) likewise.

### Why the OOB badge still works (no extra change needed, but assert it)
After the rename, `tab_for_kind(AlertKind.BUYPLAN_ACTION)` returns `"buy-plans"`, so the alert-seen
endpoint (`app/routers/alerts.py`) emits an OOB `<span id="buy-plans-nav-badge" ...>` which matches the
nav item's generated `id="{{ id }}-nav-badge"` from Task 4. Add coverage:

### Tests for Task 5 — extend `tests/test_alert_source_buyplan.py`
12. `test_buyplan_source_registered_under_buy_plans_tab`: assert `tab_for_kind(AlertKind.BUYPLAN_ACTION) == "buy-plans"`.
13. `test_sources_for_buy_plans_tab_contains_buyplan_source`: assert some source in `sources_for_tab("buy-plans")`
    has `kind == AlertKind.BUYPLAN_ACTION`.
14. A test that POSTing to the buyplan_action alert-seen endpoint returns a body containing
    `id="buy-plans-nav-badge"` (find the exact endpoint path in `app/routers/alerts.py` — it is
    `/v2/partials/alerts/{kind}/seen` style; confirm the kind slug for BUYPLAN_ACTION).

## Landmines (must respect)
- The `"quotes": "reporting"` alias MUST survive — only the `"buy-plans"` key is removed.
- Delete `test_reporting.py` and add `test_buyplan_nav.py` in the SAME commit as the nav code change.
- `GET /v2/buy-plans` route already exists (it's the partial-serving page); this task does not create it,
  only repoints nav to it. The hub body of that page is built in Task 6 — do NOT build the hub here.
- Do not touch the proactive nav badge (line ~59) or any unrelated nav item.

## Process
- TDD where practical: write/adjust the test assertions, watch them fail, make the code change, watch them pass.
- Run the touched tests: `python -m pytest tests/test_buyplan_nav.py tests/test_alert_source_buyplan.py tests/test_browser_back_navigation.py -q`
  and confirm `tests/test_reporting.py` is gone (no collection error).
- Commit once, atomically, with a clear message. Report status (DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED),
  the commit sha, a one-line test summary, and any concerns to `sdd/report-task-4-5.md`.
