# Design: Unified "Connectors" Settings rebuild

- **Date:** 2026-06-23
- **Status:** Draft — awaiting user review
- **Scope:** The connectors/API area of Settings only (not System/Data-Ops/Ops-Group/Profile/Tickets).

## 1. Background

External data providers are managed today across **two disconnected surfaces over the same `ApiSource` rows**, with **three independent dimensions** that never reconcile:

- **Sources tab** (`settings/sources.html`, route `/v2/partials/settings/sources`): an `is_active` on/off **toggle** (`PUT /api/sources/{id}/activate`), a `status` health badge (set only by `health_monitor` / the per-row **Test** button `POST /api/sources/{id}/test`), last-success/error.
- **API-Keys tab** (`settings/api_keys.html`, route `/v2/partials/settings/api-keys`): credential entry (`PUT /api/sources/{name}/credentials`, encrypted into `ApiSource.credentials`). **This tab is ORPHANED** — its `tab_button` was never wired into `settings/index.html`, so it is unreachable in the UI. (Consequence: the new "Connect Clay" button, plus the Explorium/Apollo/Hunter/Lusha key cards, can't be reached.)

So a source can be `is_active=True` with `status=error` and empty `credentials`, and the user has no single place to see or fix that. Plus: two **dead** providers (`rocketreach_enrichment`, `clearbit_enrichment`) linger as `ApiSource` rows (no connector, not in the catalog) + a legacy `startup.py` quota entry; and two **live-but-uncatalogued** providers (`ai_live_web`, SAM.gov) don't appear in Settings at all.

This rebuild consolidates everything into **one "Connectors" tab**: per-provider cards that reconcile credentials + enablement + health into a single clear state, with inline credential/Connect controls, grouped by category. It prunes the dead, surfaces the invisible, and gives an at-a-glance "is each one working."

## 2. Goals & non-goals

### Goals
- One **"Connectors"** Settings tab replacing the Sources tab and the orphaned API-Keys tab.
- Per-provider card with **one reconciled status** + **inline credential/Connect control** + **enable toggle** + **Test** + health, grouped by category.
- Make every kept provider's management reachable (Clay Connect, Explorium/Apollo/Hunter/Lusha keys, 8×8, browser-worker logins).
- **Prune** the two dead providers; **catalog** the two uncatalogued ones.
- An at-a-glance + on-demand ("Test all") way to confirm working providers; live-verify after deploy.

### Non-goals
- Other Settings tabs (System, Data Ops, Ops Group, Profile, Tickets) — unchanged.
- Changing the part-search or enrichment *logic* — this is the management surface only.
- Removing any used provider — **all 23 used providers are kept** (user-confirmed one-by-one). Only RocketReach + Clearbit are removed.
- Per-provider deep config beyond credentials + enable + test.

## 3. Decisions captured (user-confirmed)

- **Keep (23):** Part Sourcing — Nexar, BrokerBin, DigiKey, Mouser, OEMSecrets, Sourcengine, Element14, eBay; Enrichment — Apollo, Hunter, Lusha, Clay (OAuth), Explorium, SAM.gov; AI — Anthropic/Claude, AI Web Search; Comms/Platform — Azure/M365 OAuth, Email Mining, Teams, 8×8; Browser Workers — NetComponents, IC Source; Manual — Stock-List import.
- **Remove (dead):** `rocketreach_enrichment`, `clearbit_enrichment`.
- **Add cards (uncatalogued but live):** `ai_live_web`, `sam_gov_enrichment`.
- **Tab label:** "Connectors".
- Known "needs setup" at build time (surface as such, don't treat as errors): Element14 (no key), IC Source (no login), Apollo (key 401-failing), Hunter (disabled), Teams (no webhook).

## 4. Architecture

Build a new partial **`app/templates/htmx/partials/settings/connectors.html`** by extending the existing `sources.html` row (which already has status badge / toggle / test / health) and folding in the credential controls from `api_keys.html`. New view route `GET /v2/partials/settings/connectors` (mirrors `settings_sources_tab`, adds per-source credential state to the context). Wire the tab into `settings/index.html` (`tab_button('connectors', '/v2/partials/settings/connectors', 'Connectors', [icon])`), replacing the `sources` button. Retire `api_keys.html` + `sources.html`:
- `GET /v2/partials/settings/sources` → keep as a thin **302 redirect** to `/connectors` (bookmarks/old links).
- `GET /v2/partials/settings/api-keys` → **302 redirect** to `/connectors`.
- `app/routers/clay_oauth.py` `_SETTINGS_URL` → repoint to `/v2/partials/settings/connectors`.

**Data endpoints unchanged** (reused as-is): `PUT /api/sources/{id}/activate`, `POST /api/sources/{id}/test`, `PUT /api/sources/{name}/credentials`, and the Clay OAuth routes `/auth/clay/{connect,callback,disconnect}`.

The page groups cards by **category** in this order: Part Sourcing · Enrichment · AI · Communications · Browser Workers · Manual. Mapping from `ApiSource.category`/`source_type`/`name` to these display groups lives in one helper (`_connector_group(source) -> str`) in the view; render as collapsible sections with a per-group count.

## 5. The reconciled per-card status

A single `connector_state(source, credential_set, oauth_connected)` helper (pure, unit-tested) collapses the three dimensions into one display state used for the badge:

| State | Condition | Badge |
|---|---|---|
| **Live** | (creds set OR oauth_connected OR keyless) AND `is_active` AND `status in (live, active)` | green "Live" |
| **Error** | (creds/oauth/keyless) AND `is_active` AND (`status==error` OR `last_error`) | red "Error" + message + Test |
| **Off** | (creds/oauth/keyless) AND NOT `is_active` | gray "Off" (toggle to enable) |
| **Needs setup** | NOT creds AND NOT oauth_connected AND NOT keyless | amber "Needs setup" (shows the credential/Connect control; **toggle disabled**) |
| **Untested** | creds present, `is_active`, `status==pending` (never tested) | blue "Untested" + Test |

- **keyless** providers (`env_vars == []` and not OAuth): SAM.gov, AI Web Search, Email Mining, Stock-List, Azure-derived — treated as always-credentialed (no field).
- Clay: `oauth_connected = clay_oauth.is_connected()`; `needs_reconnect()` → amber "Needs reconnect".
- The **toggle is disabled** in "Needs setup" (can't enable a source with no credentials).

## 6. Inline credential control by provider type

Within each card, the control rendered depends on the provider:
- **Single-key** (Nexar uses 2; most use 1–2): masked field(s) + Save → `PUT /api/sources/{name}/credentials` with `{credentials:{ENV_VAR: val}}`. (Logic lifted verbatim from `api_keys.html`'s Lusha card — single-quoted `x-data`, `.btn-md` buttons, `hx-on::before-request` value-pack per the per-card id.) Field set comes from `source.env_vars`.
- **Clay (OAuth):** Connect / Reconnect / Disconnect (`<a href="/auth/clay/connect">` full-page; `hx-post="/auth/clay/disconnect"`). No key field.
- **Multi-field (8×8):** the existing 8×8 multi-field form from `api_keys.html` (API key, PBX ID, username, password, timezone).
- **Keyless** (SAM.gov, AI Web Search, Email Mining, Stock-List): no field; status + a short note ("No key required" / "Uses Anthropic key").
- **Browser workers** (NetComponents, IC Source): login credential fields (their `env_vars`, e.g. `ICS_USERNAME`/`ICS_PASSWORD`) via the same `PUT /api/sources/{name}/credentials` path.
- **Azure/M365 + Teams:** Azure shows consented Graph scopes (existing Teams-scopes block from `api_keys.html`); Teams shows the webhook fields. Keep the existing markup.

Credential **set/not-set** state per field: reuse `credential_service.credential_is_set(db, name, env_var)` (already used by `api_keys.html`), passed into the context per source. Never render secret values (masked display only).

## 7. Prune + catalog

- **Prune (targeted):** in `startup.py seed_api_sources`, after seeding, delete the `ApiSource` rows whose `name in {"rocketreach_enrichment","clearbit_enrichment"}` (idempotent), and remove those keys from the legacy quota backfill map (`startup.py:~1096`). Do **NOT** add a blanket "delete rows not in catalog" sweep — browser-workers (`icsource`, `netcomponents`) and `azure_oauth` are seeded outside the catalog and must survive. (Mirror the existing targeted "remove legacy `newark`" deletion already in seed.)
- **Catalog additions** to `app/data/api_sources.json` (so they seed + render): `ai_live_web` (category `api`, env_vars `[]`, "Claude-powered live web search; uses ANTHROPIC_API_KEY") and `sam_gov_enrichment` (category `enrichment`, env_vars `[]`, "Free U.S. federal supplier data; no key"). Both keyless. Confirm the enrichment_router/search code references resolve them by these exact names.

## 8. "Working" verification

- A **"Test all"** button on the Connectors page: fires the existing `POST /api/sources/{id}/test` for each source that has credentials + `is_active`, updating each card's status (HTMX, per-card OOB swaps or a refresh of the page partial). Sources without creds are skipped (shown "Needs setup").
- After deploy: I live-verify each configured connector (real Test) and **complete Clay Connect + a real Clay enrichment** here (the card is now reachable), plus confirm Explorium still enriches.

## 9. Error handling

- Credential save / toggle / test failures surface inline (toast / status text) — reuse existing endpoints' error responses; never 500 the page.
- "Test all" tolerates individual failures (one failing source doesn't abort the rest).
- Pruning is idempotent and guarded (only the two named rows; safe if already absent).

## 10. Testing (TDD)

- `connector_state(...)` helper: unit tests for each of the 5 states (Live / Error / Off / Needs-setup / Untested) incl. keyless + Clay-OAuth + needs-reconnect branches.
- `_connector_group(...)`: each provider maps to the right display group.
- View `GET /v2/partials/settings/connectors`: admin-gated; renders all groups; a key-based source shows its credential field; Clay shows Connect/Connected; SAM.gov/AI-Web-Search render as keyless; dead providers absent.
- Redirects: `/…/sources` and `/…/api-keys` → 302 to `/connectors`.
- Prune: after seed, `rocketreach_enrichment`/`clearbit_enrichment` rows absent; browser-workers + azure still present.
- Catalog: `ai_live_web` + `sam_gov_enrichment` seed + appear.
- Static guards respected: `.btn-md` (no inline px/py), single-quoted Alpine attrs, `hx-target` on lazy sub-containers.
- Full suite green (run in a clean env — note the `.env` default-override artifact for lusha/ai-screen settings tests); `/qa`; PR-review fleet.

## 11. Rollout

Build behind no flag (it's a settings-UI consolidation; no behavior change to search/enrichment). Merge → `./deploy.sh` (no migration). Then live-verify: open Settings → Connectors, confirm each group renders, **complete Clay Connect**, run Test-all, confirm working providers show Live and the "needs setup" ones show correctly.

## 12. Open risks

1. **Status reconciliation vs `is_active` semantics:** today `is_active` gates whether a source is queried. Disabling the toggle when "Needs setup" must NOT silently flip `is_active` for sources already active-without-creds — the toggle just renders disabled; existing `is_active` values are untouched. Verify no source is currently `is_active` with empty creds in a way that would surprise.
2. **`sources.html` retirement:** confirm nothing else `hx-get`s `/v2/partials/settings/sources` besides the tab (grep); the 302 covers stragglers.
3. **Test-all load:** firing Test for ~15 live sources at once → bound concurrency / make it sequential-ish to avoid hammering provider APIs + the event loop.
4. **8×8 / Teams / Azure markup** moved from `api_keys.html` must keep their exact field names + endpoints.

## 13. File list

- **New:** `app/templates/htmx/partials/settings/connectors.html`; tests `tests/test_connectors_settings.py` (+ helper tests).
- **Modified:** `app/routers/htmx_views.py` (new `/connectors` view + context with credential/oauth state + group helper; `/sources` + `/api-keys` → 302), `app/templates/htmx/partials/settings/index.html` (tab Sources→Connectors), `app/routers/clay_oauth.py` (`_SETTINGS_URL`→connectors), `app/startup.py` (prune rocketreach/clearbit + quota-map cleanup), `app/data/api_sources.json` (add ai_live_web + sam_gov_enrichment), `app/routers/sources.py` (a "test-all" endpoint if not derivable from existing).
- **Retired:** `app/templates/htmx/partials/settings/api_keys.html`, `app/templates/htmx/partials/settings/sources.html` (content folded into connectors.html; routes 302).
- **Docs:** update `docs/APP_MAP_INTERACTIONS.md` (Settings connectors surface).
