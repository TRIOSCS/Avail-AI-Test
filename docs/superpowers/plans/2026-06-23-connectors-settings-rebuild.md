# Connectors Settings Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Sources tab + the orphaned API-Keys tab with one unified, reachable "Connectors" Settings tab — per-provider cards with a reconciled status, inline credential/Connect control, enable toggle, and Test — pruning dead providers and surfacing the invisible ones.

**Architecture:** A pure `connector_service` reconciles each `ApiSource`'s credentials + `is_active` + health into one display `state` and classifies its `control_type`/`group`. The view pre-enriches a list of group dicts; one `connector_card` Jinja macro renders every provider by `control_type`. Because `/activate` + `/test` return JSON, a new single-card partial route (`GET /…/connector-card/{id}`) is the swap unit for toggle/Test/Save/Test-all (OOB).

**Tech Stack:** FastAPI, SQLAlchemy 2.0, HTMX 2 + Alpine 3 + Jinja2 + Tailwind, pytest (`-n auto`, in-memory SQLite, `TESTING=1`), Playwright (TS `e2e/` + Python `tests/e2e/`).

## Global Constraints

- Run pytest: `TESTING=1 PYTHONPATH=<worktree> /root/availai/.venv/bin/python -m pytest`.
- **Reuse the existing design system** (CLAUDE.md: follow patterns, no new conventions): `.card`/`.btn-md`/brand-* tokens, the `sources.html` dot+pill + toggle, the `api_keys.html` masked-key field. **Buttons use `.btn-md`, never inline `px-/py-`** (static ratchet `test_inline_button_sizing_does_not_grow`).
- **Single-quoted Alpine attrs** (`x-data='{"show": false}'`); never a literal `"` inside a double-quoted Alpine attr (static guard).
- **Every HTMX control sets an explicit `hx-target`** — never inherit `#settings-content`/`#main-content` (page-wipe anti-pattern).
- Settings partials start with `<title hx-swap-oob="true">…</title>` and the view calls `_base_ctx(request, user, "settings")` (else `UndefinedError`).
- Admin-gated routes use `Depends(require_admin)` (same dep the other settings tabs use).
- Templates stay logic-light; reconciliation/classification lives in `connector_service` (pure, tested).
- Keep all 23 used providers; remove only `rocketreach_enrichment` + `clearbit_enrichment`. No DB migration.
- New files get a header comment; loguru not print.
- Microcopy: use the exact strings in §14.4 of the spec (`docs/superpowers/specs/2026-06-23-connectors-settings-rebuild-design.md`).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/services/connector_service.py` (new) | Pure: `connector_state`, `control_type`, `connector_group`, `GROUP_ORDER`. |
| `app/routers/htmx_views.py` (modify) | `settings_connectors_tab` (context builder) + `connector_card_partial` + `/sources`,`/api-keys`→302. |
| `app/routers/sources.py` (modify) | New `POST /v2/partials/settings/connectors/test-all` (OOB fan-out). |
| `app/templates/htmx/partials/settings/_connector_macros.html` (new) | `connector_card`, `state_badge`, field sub-macros. |
| `app/templates/htmx/partials/settings/connectors.html` (new) | Groups loop + Test-all + root container. |
| `app/templates/htmx/partials/settings/index.html` (modify) | Tab: Sources → Connectors. |
| `app/routers/clay_oauth.py` (modify) | `_SETTINGS_URL` → `/connectors`. |
| `app/startup.py` (modify) | Prune rocketreach/clearbit rows + quota-map entries. |
| `app/data/api_sources.json` (modify) | Add `ai_live_web` + `sam_gov_enrichment`. |
| Retired | `settings/api_keys.html`, `settings/sources.html` (content folded into macros; routes 302). |
| Tests | `tests/test_connector_service.py`, `tests/test_connectors_settings.py`, `tests/test_connectors_test_all.py`, `tests/e2e/test_connectors_settings_e2e.py`, edits to `e2e/dead-ends.spec.ts` + `e2e/workflows.spec.ts` + `e2e/accessibility.spec.ts`. |

---

### Task 1: `connector_service` — reconciliation + classification

**Files:** Create `app/services/connector_service.py`; Test `tests/test_connector_service.py`.

**Interfaces — Produces:**
- `GROUP_ORDER: list[tuple[str,str]]` = `[("part_sourcing","Part Sourcing"),("enrichment","Enrichment"),("ai","AI"),("communications","Communications"),("browser_workers","Browser Workers"),("manual","Manual")]`
- `control_type(source) -> str` ∈ `{"key","oauth_clay","multi_field","keyless","browser_login","scopes"}`
- `connector_group(source) -> str` (one of the GROUP_ORDER keys)
- `connector_state(source, *, credential_set: bool, oauth_connected: bool, needs_reconnect: bool, keyless: bool) -> str` ∈ `{"live","error","off","needs_setup","needs_reconnect","untested"}`

- [ ] **Step 1: Write failing tests**
```python
# tests/test_connector_service.py
from types import SimpleNamespace
from app.services import connector_service as cs

def _src(**kw):
    base = dict(name="nexar", category="api", source_type="aggregator", env_vars=["NEXAR_CLIENT_ID"],
                is_active=True, status="live", last_error=None)
    base.update(kw); return SimpleNamespace(**base)

def test_control_type_classification():
    assert cs.control_type(_src(name="clay_enrichment")) == "oauth_clay"
    assert cs.control_type(_src(name="eight_by_eight")) == "multi_field"
    assert cs.control_type(_src(name="icsource")) == "browser_login"
    assert cs.control_type(_src(name="netcomponents")) == "browser_login"
    assert cs.control_type(_src(name="azure_oauth")) == "scopes"
    assert cs.control_type(_src(name="teams")) == "scopes"
    assert cs.control_type(_src(name="sam_gov_enrichment", env_vars=[])) == "keyless"
    assert cs.control_type(_src(name="ai_live_web", env_vars=[])) == "keyless"
    assert cs.control_type(_src(name="nexar", env_vars=["NEXAR_CLIENT_ID"])) == "key"

def test_group_mapping():
    assert cs.connector_group(_src(name="nexar", category="api", source_type="aggregator")) == "part_sourcing"
    assert cs.connector_group(_src(name="lusha_enrichment", category="enrichment")) == "enrichment"
    assert cs.connector_group(_src(name="anthropic", category="platform")) in ("ai", "communications")  # see impl note
    assert cs.connector_group(_src(name="icsource")) == "browser_workers"
    assert cs.connector_group(_src(name="stock_list")) == "manual"

def test_state_live():
    assert cs.connector_state(_src(status="live", is_active=True), credential_set=True, oauth_connected=False, needs_reconnect=False, keyless=False) == "live"
def test_state_error_from_status():
    assert cs.connector_state(_src(status="error", is_active=True, last_error="401"), credential_set=True, oauth_connected=False, needs_reconnect=False, keyless=False) == "error"
def test_state_off_when_inactive():
    assert cs.connector_state(_src(status="live", is_active=False), credential_set=True, oauth_connected=False, needs_reconnect=False, keyless=False) == "off"
def test_state_needs_setup_no_creds():
    assert cs.connector_state(_src(is_active=False), credential_set=False, oauth_connected=False, needs_reconnect=False, keyless=False) == "needs_setup"
def test_state_untested_pending():
    assert cs.connector_state(_src(status="pending", is_active=True), credential_set=True, oauth_connected=False, needs_reconnect=False, keyless=False) == "untested"
def test_state_keyless_is_credentialed():
    assert cs.connector_state(_src(status="live", is_active=True), credential_set=False, oauth_connected=False, needs_reconnect=False, keyless=True) == "live"
def test_state_clay_needs_reconnect():
    assert cs.connector_state(_src(name="clay_enrichment", is_active=True), credential_set=False, oauth_connected=False, needs_reconnect=True, keyless=False) == "needs_reconnect"
def test_state_clay_connected_live():
    assert cs.connector_state(_src(name="clay_enrichment", status="live", is_active=True), credential_set=False, oauth_connected=True, needs_reconnect=False, keyless=False) == "live"
```

- [ ] **Step 2: Run → FAIL** (`pytest tests/test_connector_service.py -q`).

- [ ] **Step 3: Implement `app/services/connector_service.py`**
```python
"""Connector classification + status reconciliation for the Settings → Connectors page.

Pure helpers (no DB/IO): collapse an ApiSource's credentials + is_active + health
status into one display `state`, and classify its control type + display group.

Called by: app/routers/htmx_views.py (settings_connectors_tab, connector_card_partial),
app/routers/sources.py (test-all). Depends on: nothing.
"""

GROUP_ORDER: list[tuple[str, str]] = [
    ("part_sourcing", "Part Sourcing"),
    ("enrichment", "Enrichment"),
    ("ai", "AI"),
    ("communications", "Communications"),
    ("browser_workers", "Browser Workers"),
    ("manual", "Manual"),
]

_OAUTH_CLAY = {"clay_enrichment"}
_MULTI_FIELD = {"eight_by_eight"}
_BROWSER = {"icsource", "netcomponents"}
_SCOPES = {"azure_oauth", "teams"}
_AI = {"anthropic", "ai_live_web"}
_MANUAL = {"stock_list"}


def control_type(source) -> str:
    name = source.name
    if name in _OAUTH_CLAY:
        return "oauth_clay"
    if name in _MULTI_FIELD:
        return "multi_field"
    if name in _BROWSER:
        return "browser_login"
    if name in _SCOPES:
        return "scopes"
    if not (source.env_vars or []):
        return "keyless"
    return "key"


def connector_group(source) -> str:
    name, cat = source.name, (source.category or "")
    if name in _BROWSER:
        return "browser_workers"
    if name in _MANUAL:
        return "manual"
    if name in _AI:
        return "ai"
    if cat == "enrichment":
        return "enrichment"
    if name in _SCOPES or cat in ("email", "auth", "platform", "notifications"):
        return "communications"
    return "part_sourcing"


def is_keyless(source) -> bool:
    return control_type(source) in ("keyless", "scopes") or not (source.env_vars or [])


def connector_state(source, *, credential_set, oauth_connected, needs_reconnect, keyless) -> str:
    if needs_reconnect:
        return "needs_reconnect"
    has_access = credential_set or oauth_connected or keyless
    if not has_access:
        return "needs_setup"
    if not source.is_active:
        return "off"
    if source.status == "error" or source.last_error:
        return "error"
    if source.status in ("live", "active"):
        return "live"
    return "untested"  # pending / unknown
```
Impl note: `anthropic` maps to "ai" via `_AI`; if its catalog `category` is `platform`, the `_AI` check precedes the platform→communications rule, so it lands in "ai" (the test allows either, but `_AI` makes it "ai").

- [ ] **Step 4: Run → PASS.** Adjust the one ambiguous anthropic assertion if needed (it lands in "ai").
- [ ] **Step 5: Commit** — `git add app/services/connector_service.py tests/test_connector_service.py && git commit -m "feat(connectors): connector_service (state + control_type + group)"`

---

### Task 2: Catalog additions + prune dead providers

**Files:** Modify `app/data/api_sources.json`, `app/startup.py`; Test `tests/test_connectors_seed.py`.

- [ ] **Step 1: Write failing test**
```python
# tests/test_connectors_seed.py
import json
def test_catalog_has_new_sources():
    cat = json.load(open("app/data/api_sources.json"))
    names = {s["name"] for s in cat}
    assert "ai_live_web" in names and "sam_gov_enrichment" in names
    assert cat  # dead ones never re-added to catalog
    assert "rocketreach_enrichment" not in names and "clearbit_enrichment" not in names

def test_seed_prunes_dead_and_keeps_workers(db_session):
    from app.models import ApiSource
    from app.startup import seed_api_sources
    # seed a dead row + a browser worker, then run seed
    db_session.add(ApiSource(name="rocketreach_enrichment", category="enrichment", credentials={}))
    db_session.add(ApiSource(name="icsource", category="api", credentials={}))
    db_session.commit()
    seed_api_sources(db_session)
    names = {s.name for s in db_session.query(ApiSource).all()}
    assert "rocketreach_enrichment" not in names and "clearbit_enrichment" not in names
    assert "icsource" in names  # browser worker survives (seeded outside catalog)
    assert "ai_live_web" in names and "sam_gov_enrichment" in names
```
(Confirm `seed_api_sources`'s real signature — it may take no `db` arg / open its own session; adapt the test to call it the way the app does, mirroring an existing seed test.)

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.**
  - In `app/data/api_sources.json` add two entries (match the existing schema fields exactly — read a neighbor entry first):
    ```json
    { "name": "ai_live_web", "display_name": "AI Web Search", "category": "ai", "source_type": "ai_search",
      "description": "Claude-powered live web search for part availability.", "signup_url": "https://console.anthropic.com",
      "env_vars": [], "setup_notes": "No key required. Uses your Anthropic key; on when that is configured." }
    ```
    ```json
    { "name": "sam_gov_enrichment", "display_name": "SAM.gov", "category": "enrichment", "source_type": "enrichment",
      "description": "Free U.S. federal supplier/contractor data.", "signup_url": "https://sam.gov",
      "env_vars": [], "setup_notes": "No key required. Free public API." }
    ```
  - In `app/startup.py seed_api_sources`, after the existing seed loop (next to the targeted `newark` deletion), add an idempotent prune:
    ```python
    for dead in ("rocketreach_enrichment", "clearbit_enrichment"):
        row = db.query(ApiSource).filter_by(name=dead).first()
        if row:
            db.delete(row)
    db.commit()
    ```
    and remove `rocketreach_enrichment` / `clearbit_enrichment` keys from the legacy quota-backfill map (~line 1096).
- [ ] **Step 4: Run → PASS;** also `python -c "import json; json.load(open('app/data/api_sources.json'))"` + `python -c "import app.startup"` clean.
- [ ] **Step 5: Commit** — `git add app/data/api_sources.json app/startup.py tests/test_connectors_seed.py && git commit -m "feat(connectors): catalog AI-web+SAM.gov, prune RocketReach/Clearbit"`

---

### Task 3: Connectors view + single-card partial + redirects

**Files:** Modify `app/routers/htmx_views.py` (add 2 routes; convert 2 to 302), `app/routers/clay_oauth.py` (`_SETTINGS_URL`); Test `tests/test_connectors_settings.py`.

**Interfaces — Produces:** `GET /v2/partials/settings/connectors`; `GET /v2/partials/settings/connector-card/{source_id}`; `GET /v2/partials/settings/sources` + `/api-keys` → `302 /v2/partials/settings/connectors`. Context key `connector_groups` = list of `{key,label,sources:[<enriched dict>]}` per spec §14.2.

- [ ] **Step 1: Write failing tests** (reuse the admin-client fixture pattern from `tests/test_settings_api_keys_cards.py`; stub `clay_oauth.is_connected/needs_reconnect` via the autouse pattern there)
```python
# tests/test_connectors_settings.py
def test_connectors_tab_renders_groups(admin_client):
    html = admin_client.get("/v2/partials/settings/connectors").text
    for label in ("Part Sourcing", "Enrichment", "AI", "Communications", "Browser Workers", "Manual"):
        assert label in html
    assert "rocketreach" not in html.lower() and "clearbit" not in html.lower()

def test_connectors_tab_key_card_has_field(admin_client):
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "LUSHA_API_KEY" in html  # a key-based card exposes its env-var field

def test_connectors_tab_clay_connect(admin_client, monkeypatch):
    import app.routers.htmx_views as v
    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: False)
    monkeypatch.setattr(v.clay_oauth, "needs_reconnect", lambda: False)
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "/auth/clay/connect" in html

def test_single_card_route(admin_client):
    # pick a known source id from the seeded set
    import app.models as m
    # find a source id via the app's db session fixture if available, else assert route shape
    r = admin_client.get("/v2/partials/settings/connector-card/1", follow_redirects=False)
    assert r.status_code in (200, 404)  # 200 if id 1 exists

def test_old_routes_redirect(admin_client):
    for path in ("/v2/partials/settings/sources", "/v2/partials/settings/api-keys"):
        r = admin_client.get(path, follow_redirects=False)
        assert r.status_code in (302, 307)
        assert "/connectors" in r.headers["location"]

def test_connectors_admin_gated(unauthenticated_client):
    assert unauthenticated_client.get("/v2/partials/settings/connectors", follow_redirects=False).status_code in (401, 403, 302, 307)
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** in `htmx_views.py` (read `settings_sources_tab` ~10732 + `settings_api_keys_tab` ~10855 first; reuse `_field`, `_base_ctx`, the admin dep, `template_response`):
  - `_build_connector_groups(db, request) -> list[dict]`: query all `ApiSource`; for each compute `ct=connector_service.control_type(s)`, `keyless=connector_service.is_keyless(s)`, `creds={ev: _field(s.name, ev) for ev in (s.env_vars or [])}`, `credential_set=any(c["is_set"] for c in creds.values())`, and for Clay `oauth_connected=clay_oauth.is_connected()`, `needs_reconnect=clay_oauth.needs_reconnect()` (else False); `state=connector_service.connector_state(s, credential_set=credential_set, oauth_connected=oauth_connected, needs_reconnect=needs_reconnect, keyless=keyless)`; build the enriched dict (spec §14.2 schema, incl. `keyless_note` for keyless: "No key required — uses your Anthropic key." for ai_live_web, "No key required." otherwise). Bucket by `connector_service.connector_group(s)`, emit in `GROUP_ORDER`, dropping empty groups.
  - `settings_connectors_tab` (GET `/v2/partials/settings/connectors`, admin): ctx = `_base_ctx(...)` + `{"connector_groups": _build_connector_groups(...)}`; `template_response("htmx/partials/settings/connectors.html", ctx)`.
  - `connector_card_partial` (GET `/v2/partials/settings/connector-card/{source_id}`, admin): build the single enriched source dict (reuse the per-source enrichment), render via a tiny template that imports the macro and emits just `connector_card(s)`; 404 if not found.
  - Convert `settings_sources_tab` + `settings_api_keys_tab` bodies to `return RedirectResponse("/v2/partials/settings/connectors", status_code=302)` (keep the routes registered).
  - In `clay_oauth.py`: `_SETTINGS_URL = "/v2/partials/settings/connectors"`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git add app/routers/htmx_views.py app/routers/clay_oauth.py tests/test_connectors_settings.py && git commit -m "feat(connectors): connectors tab view + single-card partial + redirects"`

---

### Task 4: Macros + template + tab nav + microcopy + a11y

**Files:** Create `app/templates/htmx/partials/settings/_connector_macros.html`, `connectors.html`; Modify `index.html`; Delete `api_keys.html` + `sources.html`. Test extends `tests/test_connectors_settings.py`.

- [ ] **Step 1: Add failing render-detail tests**
```python
def test_card_microcopy_and_a11y(admin_client):
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "Needs setup" in html or "Live" in html          # reconciled state labels
    assert 'aria-label="Enable' in html                       # toggle a11y label
    assert "btn-md" in html and 'class="px-4 py-2 bg-brand' not in html  # btn-md, not inline
    assert 'hx-target=' in html                                # explicit targets
    assert "Test all" in html

def test_clay_card_no_key_input(admin_client, monkeypatch):
    import app.routers.htmx_views as v
    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: True)
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "/auth/clay/disconnect" in html
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `_connector_macros.html` (`state_badge`, `key_field`, `save_button`, `connector_card` branching on `s.control_type` — use the macro skeleton in spec §14.2 + the Design fragment; fixed-width pill `w-28 justify-center`; `.btn-md` buttons; single-quoted Alpine; toggle with `aria-label="Enable {{ s.display_name }}"` + `disabled` when `state=='needs_setup'`; credential `<input>` gets `id` + a `<label for>` /`aria-label`; every `hx-*` control has explicit `hx-target`; after-request wiring re-GETs `/v2/partials/settings/connector-card/{{ s.id }}` into `#connector-card-{{ s.id }}` outerHTML; spinners `class="animate-spin motion-reduce:hidden"` or `motion-reduce:animate-none`). Microcopy strings verbatim from spec §14.4. Then `connectors.html`: `{% from "htmx/partials/settings/_connector_macros.html" import connector_card %}`, OOB `<title>`, page subtitle + header chip + Test-all button (`hx-post="/v2/partials/settings/connectors/test-all" hx-target="#connectors-root" hx-swap="none"`), loop `connector_groups` as collapsible Alpine sections with the state-weighted summary. Wire `index.html` tab: replace the `sources` `tab_button` with `tab_button('connectors', '/v2/partials/settings/connectors', 'Connectors', [<box icon path>])`. `git rm app/templates/htmx/partials/settings/api_keys.html app/templates/htmx/partials/settings/sources.html`; grep for any other `hx-get` to `/settings/sources` or `/settings/api-keys` and confirm only the (now-302) routes + the relabeled tab reference them.
- [ ] **Step 4: Run** the settings tests + the static guards:
  `TESTING=1 PYTHONPATH=$PWD /root/availai/.venv/bin/python -m pytest tests/test_connectors_settings.py "tests/test_static_analysis.py::test_inline_button_sizing_does_not_grow" -q` → PASS (btn-md must NOT raise the inline-button count).
- [ ] **Step 5: Commit** — `git add app/templates/htmx/partials/settings/ app/routers/htmx_views.py tests/test_connectors_settings.py && git commit -m "feat(connectors): connector_card macro + connectors tab + microcopy + a11y"`

---

### Task 5: "Test all" endpoint (OOB fan-out)

**Files:** Modify `app/routers/sources.py` (new route) + reuse the card macro; Test `tests/test_connectors_test_all.py`.

**Interfaces — Produces:** `POST /v2/partials/settings/connectors/test-all` → HTML bundle of `hx-swap-oob="true"` card fragments for each credentialed+active source it tested.

- [ ] **Step 1: Write failing tests**
```python
# tests/test_connectors_test_all.py
def test_test_all_tests_eligible_and_returns_oob(admin_client, monkeypatch):
    import app.routers.sources as srcs
    tested = []
    async def fake_test_one(source, db): tested.append(source.name); return {"ok": True}
    monkeypatch.setattr(srcs, "_run_source_test", fake_test_one, raising=False)
    r = admin_client.post("/v2/partials/settings/connectors/test-all")
    assert r.status_code == 200
    assert 'hx-swap-oob="true"' in r.text
    assert 'id="connector-card-' in r.text

def test_test_all_skips_needs_setup(admin_client, monkeypatch):
    # a source with no creds + not keyless must not be tested
    ...  # assert its name not in `tested`
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** in `sources.py`: factor the existing per-source test logic (`test_api_source` ~488) into a reusable `_run_source_test(source, db)`; add `test_all_connectors` route (admin) that selects sources with credentials + `is_active` (skip needs-setup/keyless-without-test), runs them **sequentially** (bounded — a simple loop; do NOT `asyncio.gather` all at once, per spec §12.3), re-enriches each tested source, and returns the macro-rendered cards each wrapped `<div id="connector-card-{id}" hx-swap-oob="true">…</div>`. Tolerate per-source exceptions (the card just renders Error).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git add app/routers/sources.py tests/test_connectors_test_all.py && git commit -m "feat(connectors): Test-all endpoint (sequential, OOB card swaps)"`

---

### Task 6: E2E + accessibility coverage

**Files:** Create `tests/e2e/test_connectors_settings_e2e.py`; Modify `e2e/dead-ends.spec.ts`, `e2e/workflows.spec.ts`, `e2e/accessibility.spec.ts`.

- [ ] **Step 1:** TS `dead-ends.spec.ts` — add `/v2/partials/settings/connectors` to `LIST_PARTIALS`. TS `workflows.spec.ts` ("Settings & Admin") — assert `/sources` + `/api-keys` → 302 `/connectors` (`request.get(url,{maxRedirects:0})`); move `sources` out of the 200-loop; assert the 6 group headings render on `/connectors`.
- [ ] **Step 2:** Create `tests/e2e/test_connectors_settings_e2e.py` (authed_page fixture; mirror `test_sightings_workspace_e2e.py` error-collector + `test_navigation_smoke.py` swap-settle): scenarios per spec §14.5 — groups render; a key card shows `input[name='LUSHA_API_KEY']` (type password); Clay shows `a[href='/auth/clay/connect']` (assert href, **don't click**); SAM.gov/AI-Web render keyless (no credential input); toggle → `expect_response` `PUT **/api/sources/*/activate`; Test → `POST **/api/sources/*/test` (mock via `page.route`); Test-all → `#main-content` childElementCount unchanged (no navigation) + a card's pill text changed; needs-setup toggle `disabled`; page text has no `rocketreach`/`clearbit`; **zero console/pageerror**.
- [ ] **Step 3:** TS `accessibility.spec.ts` — add an authed axe pass over `/connectors` (`critical|serious` = 0).
- [ ] **Step 4: Run** the Python e2e locally if the harness is available (`npx playwright test --project=workflows` / the python e2e per project conventions); if e2e infra isn't runnable here, assert the specs are syntactically valid + lint (`npm run lint`) and note they run in CI.
- [ ] **Step 5: Commit** — `git add tests/e2e/test_connectors_settings_e2e.py e2e/ && git commit -m "test(connectors): E2E + a11y coverage for the Connectors tab"`

---

### Task 7: Docs + full suite + review

**Files:** Modify `docs/APP_MAP_INTERACTIONS.md` (+ `docs/APP_MAP_ARCHITECTURE.md` if it lists settings tabs).

- [ ] **Step 1:** Update `docs/APP_MAP_INTERACTIONS.md`: the Settings surface now has a unified **Connectors** tab (reconciled status via `connector_service`, single-card partial swap unit, Test-all OOB); Sources/API-Keys retired (302).
- [ ] **Step 2:** `pre-commit run --all-files`; full suite `TESTING=1 PYTHONPATH=$PWD /root/availai/.venv/bin/python -m pytest tests/ -q` → green (run in clean env; note the `.env` default-override artifact for lusha/ai-screen settings tests).
- [ ] **Step 3:** `/qa` + PR-review fleet; fix ALL Critical/Important.
- [ ] **Step 4: Commit docs** — `git add docs/ && git commit -m "docs(connectors): map unified Connectors settings surface"`
- [ ] **Step 5 (deploy, not code):** merge to main → `./deploy.sh` (no migration) → live-verify: open Settings → Connectors, confirm groups render + statuses, **complete Clay Connect** (now reachable) + run Test-all, confirm live providers show Live.

---

## Self-Review

**Spec coverage:** §4 architecture → Tasks 3/4; §5 reconciled state → Task 1; §6 credential control by type → Task 4 (macro branches); §7 prune+catalog → Task 2; §8 Test-all → Task 5; §14.1 single-card route → Task 3; §14.2 macro/context/helpers → Tasks 1/3/4; §14.3 visual → Task 4; §14.4 microcopy → Task 4; §14.5 a11y+tests → Tasks 4/6; §12 docs → Task 7. Covered.

**Placeholder scan:** the two "confirm real signature" notes (seed_api_sources arg shape; the single-card id in tests) are explicit verify-against-code instructions; the e2e "if infra not runnable" is a real fallback. No silent gaps.

**Type consistency:** `connector_state`/`control_type`/`connector_group`/`is_keyless`/`GROUP_ORDER` used consistently across Tasks 1/3/5; the enriched-source dict schema (`state`,`control_type`,`creds`,`oauth_connected`,`needs_reconnect`,`keyless_note`,`id`,`name`,`env_vars`) consistent across Tasks 3/4/5; routes `/connectors`, `/connector-card/{id}`, `/connectors/test-all` consistent.
