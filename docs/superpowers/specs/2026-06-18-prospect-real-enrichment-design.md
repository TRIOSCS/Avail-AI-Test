# Real Prospect Enrichment — SP1: Lusha in the Enrichment Chain

**Date:** 2026-06-18 (revised after architect + simplify review)
**Status:** Approved (design); pending implementation plan
**Owner:** prospecting
**Program:** Sub-project 1 of 4. **SP2** = Clay async-primary layer (separate spec:
`2026-06-18-prospect-enrichment-sp2-clay-async-design.md`); **SP3** = AI screening;
**SP4** = account reclamation. SP1 ships real enrichment via the *synchronous* providers
now and is the **gap-fill engine** SP2's Clay callback reuses. No Clay dependency;
independently shippable.

> **Design note (why no router):** an earlier draft introduced a provider-router
> abstraction (registry + Protocol + capability-map). A simplicity review showed that for
> four providers this is speculative generality — the existing `enrich_entity` *already is*
> an ordered, fill-only provider chain, and `find_suggested_contacts` *already* runs
> providers concurrently with dedup + relevance filtering. SP1 therefore **adds Lusha into
> those two existing functions** and reuses their machinery. The seams SP2/SP3 need already
> exist (they call these two functions). A router can be extracted later if a 5th+
> operator-reorderable provider ever appears.

## Goal

Make the prospecting "Enrich" action pull **real** procurement contacts and firmographics
(not just SAM.gov + Google News), so enrichment moves a prospect's fit score, readiness
tier, and buyer-ready ranking. Add **Lusha** (the high-bandwidth contact provider) to the
shared enrichment chain so both prospecting **and** CRM/vendor enrichment benefit.

## Context

- **Prospecting** (`app/services/prospect_free_enrichment.py`) is free-only today (SAM.gov +
  news) and never touches the real provider chain.
- **Shared chain** (`app/enrichment_service.py`): `enrich_entity()` runs Explorium → Apollo
  → AI fill-only (inner `_merge`, gap-gated AI); `find_suggested_contacts()` runs Explorium
  + AI concurrently (`asyncio.gather`) then dedups + relevance-filters. A working **Apollo
  connector** exists (`app/connectors/apollo.py`). **Lusha** has only a *test connector*
  (`app/routers/sources.py` `_LushaTestConnector`, which already reads
  `get_credential_cached("lusha_enrichment", "LUSHA_API_KEY")`). **Clay** is not in the
  codebase — it's SP2.

**Decision (Approach A):** wire Lusha into the **shared** chain. CRM/vendor enrichment will
begin spending Lusha credits — accepted.

## Global Constraints

- Stack: FastAPI + SQLAlchemy 2.0 (sync). No React. **No new UI elements** (only status copy).
- Outbound HTTP via the shared `app/http_client.py` `http` singleton (the new Lusha connector
  must use it — *not* a per-call `httpx.AsyncClient` like the legacy Apollo connector).
- Loguru, never `print()`. Ruff + mypy clean. Tests with every task. `db.get`, not
  `db.query(...).get`. New files get a header comment.
- **No schema change / no Alembic migration** — all new data lands in existing JSONB
  (`enrichment_data`, `readiness_signals`) + existing scalar columns (`industry`,
  `employee_count_range`, `naics_code`, `hq_location`, `revenue_range`, `fit_score`,
  `readiness_score`, `contacts_preview`).
- Firmographic writes are **fill-only** — never clobber an existing value (especially
  SAM.gov's `naics_code`); reuse the existing fill-only `_merge` pattern.
- Fire-and-forget safety: `run_enrichment_job` never raises; unexpected failure →
  `enrichment_data['enrich_status']='error'` (existing).
- **Graceful degradation:** Lusha disabled / no key → the chain behaves exactly as today.

## Architecture

### 1. Lusha connector (`app/connectors/lusha.py` — new)

Mirrors `apollo.py` but uses the shared `http` client.

```python
async def enrich_company(domain: str, api_key: str) -> dict | None
# -> {"source":"lusha","legal_name","domain","industry","employee_size",
#     "hq_city","hq_state","hq_country","linkedin_url"} | None

async def search_contacts(domain: str, api_key: str, limit: int) -> list[dict]
# -> [{"source":"lusha","full_name","email","phone","title","verified"}]  # verified per Lusha
```

Auth header per Lusha v2 (same as `_LushaTestConnector`). On **402/429** raise
`ProviderQuotaError` (so the caller trips the cooldown). On other `httpx.HTTPError` /
`KeyError` / `ValueError` → log warn + return `None`/`[]`.

### 2. Credit-guard helper (`app/services/enrichment_credit_guard.py` — new, ~15 lines)

Holds `ProviderQuotaError` plus a minimal cooldown ("circuit") so a Lusha quota/rate-limit
error isn't re-hit on every click (the one real credit guard — graceful fall-through alone
does NOT stop repeat spend across separate Enrich clicks):

```python
class ProviderQuotaError(Exception): ...
def circuit_open(provider: str) -> bool      # get_cached("enrich:circuit:{provider}") is not None
def trip_circuit(provider: str, minutes: int) -> None  # set_cached(..., {"tripped":1}, ttl_days=minutes/1440)
```

Reuses `app/cache/intel_cache.py` `get_cached`/`set_cached` (Redis → PG fallback already
handles availability; **no** extra in-memory tier). Used only around the Lusha call.

### 3. Lusha in `enrich_entity()` (company) — modify

Insert a Lusha phase after Explorium, before Apollo, and **gap-gate the later paid
providers** (early-stop) by reusing the existing `any(not result.get(f) for f in
_enrichable)` check that already gates AI:

```
_merge(await _explorium_find_company(...), "explorium")
if _lusha_enabled() and not circuit_open("lusha") and gaps_remain():
    try: _merge(await lusha.enrich_company(domain, key), "lusha")
    except ProviderQuotaError: trip_circuit("lusha", settings.lusha_cooldown_minutes)
if settings.apollo_api_key and gaps_remain():          # now gap-gated → spares Apollo
    _merge(await apollo.search_company(...), "apollo")
if gaps_remain():
    _merge(await _ai_find_company(...), "ai")
```

`_lusha_enabled()` = `settings.lusha_enrichment_enabled and bool(get_credential_cached(
"lusha_enrichment","LUSHA_API_KEY"))`. The 14-day IntelCache + input/output normalization
are **unchanged**. Gap-gating Apollo is result-equivalent (fill-only Apollo only ever added
to gaps) while saving credits when Explorium+Lusha already filled everything.

### 4. Lusha in `find_suggested_contacts()` — modify

Add Lusha **first** (it's the verified source); early-stop if it satisfies the need,
otherwise fall through to the existing concurrent Explorium+AI gather and merge:

```
def find_suggested_contacts(domain, name="", title_filter="", limit=10):
    out = []
    if _lusha_enabled() and not circuit_open("lusha"):
        try: out = await lusha.search_contacts(domain, key, limit)
        except ProviderQuotaError: trip_circuit("lusha", settings.lusha_cooldown_minutes)
    if not (len(out) >= limit and any(c.get("verified") for c in out)):
        gathered = await asyncio.gather(_explorium_find_contacts(...), _ai_find_contacts(...),
                                        return_exceptions=True)   # existing
        out.extend(...)                                           # existing extend
    # existing dedup (email/linkedin/name) + _RELEVANT_KEYWORDS relevance filter, unchanged
```

New optional `limit` kwarg defaults to 10 (today's behavior). No `seniorities` param —
seniority is derived in the prospect adapter (below). **Apollo is NOT added to contacts**
(it isn't called for contacts today; don't introduce surprise CRM spend).

### 5. Prospect adapter (`run_enrichment_job` in `prospect_free_enrichment.py`) — modify

Insert the paid step between `run_free_enrichment` and warm-intro, with the **three review
fixes** baked in:

```
1. run_free_enrichment(...)                              # SAM.gov + news (existing)
2. 24h skip gate: if enrichment_data['contacts_enriched_at'] within 24h → skip to step 4
3. company  = await enrich_entity(domain, name)
   contacts = await find_suggested_contacts(domain, name, limit=settings.prospect_enrich_contacts_per_account)
   _apply_company_to_prospect(prospect, company)   # FILL-ONLY field-name MAPPING (fix #3):
       industry→industry, employee_size→employee_count_range,
       hq_city+hq_state→hq_location ("City, ST"), naics→naics_code (only if empty; keep SAM.gov),
       revenue_range→revenue_range
   mapped = _apply_contacts_to_prospect(prospect, contacts)   # canonical {name,title,seniority,email,verified}
       name   = full_name
       seniority = infer_seniority(title)   # fix #1: title-keyword → decision_maker|influencer|contributor
       verified  = bool(c.get("verified"))  # fix #1: default False (only Lusha sets it true)
       cap at prospect_enrich_contacts_per_account; dedup
   readiness_signals['contacts_verified_count']   = sum(verified)        # fix #1: moves readiness
   readiness_signals['contacts_unverified_count'] = sum(not verified)    # fix #1: +3 pts path
   enrichment_data['contact_provider']   = sources
   enrichment_data['contacts_enriched_at'] = now
4. detect_warm_intros / generate_one_liner            # existing
5. fit_score, fit_reasoning = calculate_fit_score({industry, naics_code, employee_count_range,
       region, has_procurement_staff:None, uses_brokers:None})   # NEW recompute; None → neutral sub-scores
   readiness_score = calculate_readiness_score({name}, readiness_signals)   # existing recompute
6. enrich_status='done'; commit                       # existing
```

`infer_seniority(title)`: lowercased title → `decision_maker` if any of
{vp, vice president, director, chief, ceo, coo, cfo, cto, cpo, head of, owner, president};
`influencer` if any of {manager, lead, senior, principal, buyer, sourcing, procurement,
purchasing, commodity}; else `contributor`.

### 6. Config (`app/config.py` + `.env.example`)

```
lusha_enrichment_enabled: bool = False      # feature gate
lusha_cooldown_minutes: int = 15            # Lusha quota/rate-limit cooldown
prospect_enrich_contacts_per_account: int = 5
```

**No `lusha_api_key` in Settings** — the key flows through
`get_credential_cached("lusha_enrichment","LUSHA_API_KEY")` (DB-managed via the Sources UI,
env fallback), matching Explorium + the existing Lusha test connector (fix #2).
`.env.example` documents `LUSHA_API_KEY=` for the env fallback.

### 7. UI

No new elements. `app/templates/htmx/partials/prospecting/enrich_status.html` running copy
changes "Enriching… (SAM.gov + news)" → "Enriching… contacts + firmographics".

## Error handling & graceful degradation

- Connector errors → `None`/`[]` + warn. 402/429 → `ProviderQuotaError` → trip cooldown →
  the chain simply omits Lusha for the cooldown window and uses the other providers.
- Lusha disabled / no key → `_lusha_enabled()` false → chain == today exactly.
- All providers empty → prospect keeps free-enrichment data; `enrich_status='done'`.
- `run_enrichment_job` unexpected exception → `enrich_status='error'` (existing).

## Testing strategy

- **Lusha connector** (`tests/test_lusha_connector.py`): field mapping; empty → `None`/`[]`;
  402 + 429 → `ProviderQuotaError`; `httpx.HTTPError` → `None`/`[]`.
- **Credit guard** (`tests/test_enrichment_credit_guard.py`): `trip_circuit` then
  `circuit_open` true; expiry semantics (TTL minutes→days conversion).
- **`enrich_entity` with Lusha** (extend CRM tests): Lusha fills gaps fill-only; Apollo
  gap-gated (skipped when no gaps); circuit-open skips Lusha; disabled → unchanged.
- **`find_suggested_contacts` with Lusha**: Lusha-first early-stop (gather not called when
  Lusha returns ≥limit verified); fallback runs otherwise; existing dedup/filter intact.
- **Prospect adapter** (`tests/test_prospect_real_enrichment.py`): fill-only firmographic
  mapping incl. field-name mapping + SAM.gov `naics_code` preserved; seniority inference;
  `verified` default False; `contacts_verified_count`/`contacts_unverified_count` set; fit +
  readiness recomputed (cold → buyer-ready when strong data returned); 24h skip avoids paid
  step but still recomputes.
- **CRM regression:** existing `enrich_entity`/`find_suggested_contacts` tests stay green
  (internals preserved; Lusha gated off by default).

## Out of scope (explicit)

- A provider-router abstraction (rejected as speculative; revisit only at 5+ reorderable
  providers).
- **Clay** async-primary integration — that is **SP2** (reuses these two functions as its
  gap-fill engine).
- Batch "Enrich top N" UI/action (on-demand only).
- Per-provider monthly caps (cooldown guard only).
- Persisting `buyer_ready_score`/`is_buyer_ready` columns; `apply_historical_bonus` (until SFDC).
