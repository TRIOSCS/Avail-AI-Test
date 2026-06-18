# Real Prospect Enrichment via a Credit-Aware Provider Router — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Owner:** prospecting

## Goal

Make the prospecting tab's "Enrich" action pull **real** procurement contacts and
firmographics (not just SAM.gov + Google News), so enrichment actually moves a prospect's
fit score, readiness tier, and buyer-ready ranking. Do this by adding **Lusha** as the
high-bandwidth contact provider and generalizing the shared enrichment chain into a small
**credit-aware, task-fit provider router** that "uses whatever tool best suits the task,
among those with credit available."

## Context

Two enrichment paths exist today:

- **Prospecting** (`app/services/prospect_free_enrichment.py`): free-only — SAM.gov +
  Google News. No contacts, no firmographics beyond NAICS. This is what the "Enrich"
  button drives.
- **CRM/vendor** (`app/enrichment_service.py`): a real provider chain —
  `enrich_entity()` (Explorium → Apollo → AI, fill-only/gap-driven) and
  `find_suggested_contacts()` (Explorium + AI). A working Apollo connector exists at
  `app/connectors/apollo.py`. Lusha has only a *test connector*
  (`app/routers/sources.py` `_LushaTestConnector`); Clay is not in the codebase.

**Decision (Approach A):** wire Lusha into the **shared** chain and have prospecting reuse
it, so CRM/vendor enrichment also benefits. CRM enrichment will begin spending Lusha
credits — accepted.

**Provider access (confirmed):** Lusha has a REST API key the worker can call inline
(like Apollo). Clay is UI/MCP-only — it stays a Claude/manual seeding feed and is **out
of scope** for in-app enrichment.

## Global Constraints

- Stack: FastAPI + SQLAlchemy 2.0 (sync) + HTMX + Jinja2. No React. No new UI elements.
- All outbound HTTP via the shared `app/http_client.py` singletons (the Lusha connector
  must use `http`, unlike the legacy Apollo connector which makes its own client).
- Loguru for logging, never `print()`. Ruff + mypy clean. Tests with every change.
- No schema changes: all new data lands in existing JSONB columns
  (`enrichment_data`, `readiness_signals`) and existing scalar columns
  (`fit_score`, `readiness_score`, `industry`, `employee_count_range`, `naics_code`,
  `hq_location`, `revenue_range`, `contacts_preview`). **No Alembic migration.**
- Fire-and-forget safety: `run_enrichment_job` must never raise; on unexpected failure it
  sets `enrichment_data['enrich_status'] = 'error'` (existing behavior, preserved).
- Firmographic writes are **fill-only** — never clobber a value already set (in
  particular SAM.gov's `naics_code`), mirroring the existing `_merge` strategy.

## Architecture

### 1. Provider router (`app/services/enrichment_router.py` — new)

Generalizes the hardcoded Explorium→Apollo→AI sequence into a registry-driven router.

**Provider protocol** — each provider is an adapter exposing a uniform interface:

```python
class EnrichmentProvider(Protocol):
    name: str  # "explorium" | "lusha" | "apollo" | "ai"
    def is_configured(self) -> bool: ...
    async def fetch_company(self, domain: str, name: str | None) -> dict | None: ...
    async def fetch_contacts(
        self, domain: str, titles: list[str], seniorities: list[str], limit: int
    ) -> list[dict]: ...
```

Existing Explorium / Apollo / AI functions are wrapped into adapters (thin shims around
`_explorium_find_company`, `connectors/apollo.search_company`, `_ai_find_company`, etc.).
Lusha is a new adapter over `app/connectors/lusha.py`. A provider that does not support a
task returns `None` / `[]`.

**Capability map** (best tool per task; module-level constant, easy to reorder):

```python
CAPABILITY_ORDER = {
    "company":  ["explorium", "lusha", "apollo", "ai"],   # firmographic specialist first
    "contacts": ["lusha", "apollo", "explorium", "ai"],   # high-bandwidth contacts first
}
```

**Availability gate** (circuit-breaker only — no monthly caps). A provider is eligible iff:
1. `is_configured()` is true (API key present + its enable flag on), **and**
2. its circuit is **closed** — no `402` (payment/quota) or `429` (rate-limit) recorded for
   it within the last `provider_cooldown_minutes` (default 15).

**Circuit-breaker mechanism:** Redis key `enrich:circuit:{provider}` with
`TTL = provider_cooldown_minutes * 60`, set when a provider call returns 402/429 or raises
a quota/rate-limit error. Eligibility = key absent. In-memory fallback dict (keyed by
provider → expiry epoch) when Redis is unavailable — mirrors the existing
`app/rate_limit.py` Redis-with-in-memory-fallback pattern. No DB, no migration.

**Router algorithm** (`route(task, domain, name=...) -> dict | list`):

```
result = {} (company) or [] (contacts)
for provider_name in CAPABILITY_ORDER[task]:
    provider = registry[provider_name]
    if not provider.is_configured() or circuit_open(provider_name):
        continue
    try:
        data = await provider.fetch_company(...) | fetch_contacts(...)
    except QuotaOrRateLimit:        # 402/429 surfaced by the adapter
        trip_circuit(provider_name)
        continue
    if data:
        merge_fill_only(result, data, source=provider_name)   # company: gap-fill
        # contacts: extend, de-dupe by (name, email), keep verified first
    if is_complete(result, task):   # early-stop — save credits
        break
return result
```

- `is_complete("company")` → all of `industry`, `employee_count_range`, `naics_code`,
  `hq_location` present.
- `is_complete("contacts")` → at least `prospect_enrich_contacts_per_account` (5) entries.
- **Early-stop** means Apollo (small membership) is only called when higher-priority
  providers left gaps — satisfying "use whatever has credit; don't waste it."

`enrich_entity()` and `find_suggested_contacts()` in `enrichment_service.py` are refactored
to delegate to the router. `find_suggested_contacts()` **gains optional, defaulted** kwargs
(`titles=None`, `seniorities=None`, `limit=…`) whose defaults reproduce today's behavior, so
existing CRM callers that pass none are unaffected; return shapes are unchanged — verified by
the existing CRM enrichment tests.

`ProviderQuotaError` is defined in `app/services/enrichment_router.py` and imported by the
connectors that raise it.

**Circuit-tripping reliability:** the Lusha adapter raises `ProviderQuotaError` on 402/429
(typed), so its circuit trips reliably — this is the credit-sensitive provider we care about.
The legacy Explorium/Apollo/AI functions already swallow HTTP errors and return `None`/`[]`;
their adapters trip a circuit only if a quota/rate-limit status is detectable, otherwise the
router simply falls through to the next provider (correct either way). Circuit-tripping is
therefore *guaranteed for Lusha, best-effort for legacy providers.*

### 2. Lusha connector (`app/connectors/lusha.py` — new)

Mirrors `apollo.py` but uses the shared `http` client.

```python
async def enrich_company(domain: str, api_key: str) -> dict | None
# -> {"source":"lusha","legal_name","domain","industry","employee_size",
#     "hq_city","hq_state","hq_country","linkedin_url"} | None

async def search_contacts(
    domain: str, api_key: str, limit: int, titles: list[str], seniorities: list[str]
) -> list[dict]
# -> [{"source":"lusha","full_name","email","phone","title","seniority","verified"}]
```

Auth header per Lusha v2 (`api_key` header). Catches `httpx.HTTPError/KeyError/ValueError`
→ logs + returns `None`/`[]`. Surfaces 402/429 as a typed signal the router catches to trip
the circuit (e.g. raise `ProviderQuotaError`).

### 3. Prospect adapter (modify `app/services/prospect_free_enrichment.py`)

Insert a contact+firmographic step into `run_enrichment_job`, between free enrichment and
the warm-intro/score-recompute step:

```
run_enrichment_job(prospect_id, db):
  1. run_free_enrichment(...)                     # SAM.gov + news (existing)
  2. NEW: company = await enrich_entity(domain, name)        # via router
          contacts = await find_suggested_contacts(domain, titles=PROCUREMENT_TITLES,
                       seniorities=["decision_maker","influencer"], limit=5)
          - map company → prospect firmographics (FILL-ONLY)
          - map contacts → prospect.contacts_preview (canonical {name,title,seniority,
            email,verified}); de-dupe; cap 5
          - readiness_signals["contacts_verified_count"] = count(verified)
          - enrichment_data["contact_provider"] = company/contacts source labels
          - enrichment_data["contacts_enriched_at"] = now
  3. detect_warm_intros / generate_one_liner       # existing
  4. recompute readiness_score (existing) AND fit_score (NEW)   # both, post-mapping
  5. enrich_status='done'; commit                  # existing
```

**Guardrail — 24h double-click skip:** if `enrichment_data['contacts_enriched_at']` is
within the last 24h, skip the paid contact/firmographic step (free enrichment + news still
run, scores still recompute). On-demand otherwise always runs (explicit buyer intent).
There is **no** batch path, so no multi-day cooldown is needed.

**Fit recompute:** call `calculate_fit_score({...prospect firmographic fields...})` and
assign `prospect.fit_score`. Pair it with the existing readiness recompute so a single
enrichment pass updates both scores and therefore `build_priority_snapshot`'s buyer-ready
ranking.

`PROCUREMENT_TITLES` = `["procurement","supply chain","sourcing","purchasing","buyer",
"commodity","materials","operations"]` (maximizes verified-decision-maker proof points per
the scoring contract).

### 4. UI

No new elements. `app/templates/htmx/partials/prospecting/enrich_status.html` running-state
copy changes from "Enriching… (SAM.gov + news)" to "Enriching… contacts + firmographics".
The existing 2s poll / HTTP-286 stop is unchanged.

### 5. Config (`app/config.py` + `.env.example`)

```
lusha_api_key: str = ""
lusha_enrichment_enabled: bool = False        # gate; off until key present
provider_cooldown_minutes: int = 15           # circuit-breaker TTL
prospect_enrich_contacts_per_account: int = 5
```

(No `*_monthly_cap` — circuit-breaker only, per approval.)

## Data flow

- **On-demand (prospecting):** "Enrich" button → `POST …/enrich` sets `enrich_status=running`
  + spawns `run_enrichment_job` → router pulls contacts/firmographics → fields + scores
  updated → poll returns HTTP 286 → detail re-renders with real contacts, higher fit/
  readiness, updated buyer-ready badge.
- **CRM/vendor:** unchanged call sites; `enrich_entity`/`find_suggested_contacts` now route
  through the credit-aware router and can use Lusha.

## Error handling

- Connector errors → `None`/`[]` + Loguru warn; never propagate.
- 402/429 → trip provider circuit, fall through to next provider; surfaced nowhere to the
  user (degraded silently to the next-best tool).
- All providers unavailable → router returns empty → prospect keeps free-enrichment data;
  `enrich_status='done'` (not error — free enrichment succeeded).
- `run_enrichment_job` unexpected exception → `enrich_status='error'` (existing).

## Graceful degradation

No Lusha key or `lusha_enrichment_enabled=False` → router omits Lusha → behavior is exactly
today's Explorium→Apollo→AI chain for CRM and free-only for prospecting (prospecting only
gains contacts/firmographics when at least one configured provider answers).

## Testing strategy

- **Connector** (`tests/test_lusha_connector.py`): mock `http`; success maps fields;
  empty → `None`/`[]`; 402/429 → raises `ProviderQuotaError`; network error → `None`/`[]`.
- **Router** (`tests/test_enrichment_router.py`): capability order honored; unconfigured
  provider skipped; circuit-open provider skipped; 429 trips circuit + falls through;
  early-stop stops after completion (asserts lower-priority provider NOT called);
  fill-only merge; contacts de-dupe + verified-first + cap.
- **Prospect adapter** (`tests/test_prospect_real_enrichment.py`): router output maps to
  `contacts_preview` + firmographics (fill-only, SAM.gov `naics_code` preserved);
  `contacts_verified_count` set; fit + readiness recomputed (cold → buyer-ready when strong
  signals returned); 24h skip avoids the paid step but still recomputes.
- **CRM regression:** existing `enrich_entity`/`find_suggested_contacts` tests stay green
  (router refactor preserves signatures/shapes).

## Out of scope (explicit)

- **Clay** in-app integration (UI/MCP-only; stays a manual/Claude seeding feed).
- **Batch "Enrich top N"** UI/action (on-demand only).
- **Per-provider monthly caps** (circuit-breaker only).
- Persisting `buyer_ready_score`/`is_buyer_ready` as columns (separate parked item).
- `apply_historical_bonus` wiring (parked until SFDC import).
