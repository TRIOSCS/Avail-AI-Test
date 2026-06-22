# Design: Unified Company/Contact Enrichment Blending — Explorium + Clay activation

- **Date:** 2026-06-22
- **Status:** Draft — awaiting user review
- **Author:** Claude (brainstorming → spec)
- **Scope owner:** CRM / vendor enrichment path (`enrich_entity` + `find_suggested_contacts`)

---

## 1. Background & motivation

The user asked to "activate Explorium and Clay." Investigation showed this is **not** a flag-flip:

- **Explorium** is wired into the waterfall but the connector is **broken on every axis** — it calls a
  nonexistent endpoint, sends `Authorization: Bearer` when Explorium requires an `api_key:` header,
  sends a flat body when Explorium wants `{filters:{…}}`, and assumes a one-shot call when Explorium is a
  multi-step `match → firmographics-enrich → prospects → contacts-enrich` pipeline. No key is set in live config.
  The user **has** an Explorium subscription + API key.
- **Clay** has only the webhook architecture coded (`CLAY_WEBHOOK_URL` + `CLAY_CALLBACK_SECRET`), and the
  Settings UI saves a `CLAY_API_KEY` the service ignores. The webhook + HTTP-API-column features require Clay's
  **Growth (~$495/mo)** tier; the user is on **Launch**. Research + a live spike confirmed the right path is the
  **Clay MCP** (`https://api.clay.com/v3/mcp`, auth via `CLAY_API_KEY`), which works on any paid plan and
  returns **company firmographics synchronously** (only contact emails are async via a `get-task-context` poll).
- Adding these brings the company/contact enrichment roster to **6 providers** (SAM.gov, Apollo, Hunter, Clay,
  Lusha, Explorium, + AI fallback). SP1 explicitly deferred a real router as "speculative generality for 4
  providers" but said to revisit "if a 5th+ operator-reorderable provider ever appears." We are past that line.

The current orchestration has two latent defects that make naïve "blending" wrong:

1. **`_merge` is first-write-wins, no authority model** (`enrichment_service.py:518-527`). Whichever provider
   runs first and returns a non-empty value owns that field forever. "Priority" is purely call order — so any
   cost-driven reorder risks a low-authority source (e.g. an AI guess) beating a high-authority one (Explorium).
2. **`apply_enrichment_to_*` is fill-only and drops fields** (`enrichment_service.py:687-742`). It never writes
   `ticker/naics/revenue_range` even though Explorium returns them, and it can never correct a wrong value.

This spec activates Explorium + Clay **and** replaces first-write-wins with a per-field **authority ladder**
(ported from the proven materials F1 tier ladder, `app/services/spec_tiers.py`), so all providers blend in the
most advantageous way: free sources first for cost, highest-authority source wins per field for quality.

## 2. Goals & non-goals

### Goals
- A correct, tested **Explorium connector** against the real API.
- A correct, tested **Clay MCP connector** (sync firmographics + bounded contact-email poll); delete the dead webhook path.
- A **unified blending layer**: cost-tiered + gap-gated routing across all providers, per-field source-authority
  arbitration, cross-provider contact dedup with quality tiebreak.
- Normalize provider config so every provider has a feature flag, key resolution, cooldown, and circuit (today
  Hunter has *no* config; Apollo has no flag/cooldown/circuit).
- Settings → API Keys cards for Explorium, Apollo, Hunter (Lusha + Clay already have cards).
- Ship behind flags off → enable → deploy → live-verify.

### Non-goals (explicitly out of scope)
- **SP2** (the async "Clay-primary" *prospecting* layer). This spec is the synchronous CRM/vendor enrichment path only.
- The **part-sourcing connectors** (Nexar, BrokerBin, DigiKey, Mouser, OEMSecrets, Element14, Sourcengine, eBay).
  They answer "where can I buy this MPN" via parallel search in `search_service.py` — a different subsystem.
- Backfilling per-field provenance for historical rows (we protect existing values; see §6.3).

## 3. Architecture overview

Three components, built behind flags:

```
A. Connectors (data in)                 B. Blending layer (arbitration)        C. Surface
   app/connectors/explorium.py  ──┐
   app/connectors/clay_mcp.py    ──┤                                            Settings → API Keys cards
   (existing: lusha, apollo,      ──┼──▶ app/services/enrichment_router.py ──▶  (Explorium/Apollo/Hunter)
    hunter, sam_gov, ai)          ──┤      • cost-tiered, gap-gated call order
                                   ──┘      • per-run gather of (source, dict)   feature flags + cooldowns
                                            • delegates per-field arbitration
                                              to app/services/firmo_tiers.py
                                                   │
                                                   ▼
                                   ladder-aware apply_enrichment_to_{company,vendor}
                                   (JSONB enrichment_provenance; safe overwrite)
```

`enrich_entity()` and `find_suggested_contacts()` remain the **stable public façade** (callers in
`app/routers/crm/enrichment.py` and `companies.py` keep importing them) but delegate field-merging to the router.

## 4. Component A — Explorium connector rewrite

**New file `app/connectors/explorium.py`** (mirrors `lusha.py` structure; uses the shared `app/http_client.py`).
Replaces the broken logic in `enrichment_service.py:_explorium_find_*` and `app/services/prospect_discovery_explorium.py`.

- **Auth:** header `api_key: {EXPLORIUM_API_KEY}` (NOT `Authorization: Bearer`). Base `https://api.explorium.ai/v1`.
- **Company pipeline (2 calls):**
  1. `POST /v1/businesses/match` — body `{"businesses_to_match":[{"name":<name>,"domain":<domain>}]}` →
     `matched_businesses[0].business_id` (null → no match → return `None`).
  2. `POST /v1/businesses/firmographics/enrich` — body `{"business_id":<id>}` → firmographics.
- **Contact pipeline (2 calls):**
  1. `POST /v1/prospects` — body `{"filters":{"business_id":{"values":[<id>]}, "job_title":{...}, "has_email":true}, "size":<limit>}` → `prospect_id[]` + base attributes.
  2. `POST /v1/prospects/contacts_information/enrich` — body `{"prospect_id":<id>}` → `professional_email`, `professional_email_status`, `mobile_phone`/`phone_numbers[]`.
- **Envelope:** parse `{response_context, data}` — read `data`, not `businesses`/`results`.
- **Normalization → app shape:**
  | App field | Explorium source |
  |---|---|
  | `legal_name` | `name` |
  | `domain` | host of `website` (fallback: input domain) |
  | `website` | `website` |
  | `industry` | `linkedin_industry_category` |
  | `employee_size` | `number_of_employees_range` (format band → e.g. `"1001-5000"`) |
  | `hq_city / hq_state / hq_country` | `city_name / region_name / country_name` |
  | `linkedin_url` | `linkedin_profile` |
  | `naics` | `naics` |
  | `ticker` | `ticker` |
  | `revenue_range` | `yearly_revenue_range` (format band) |
  | contact `email` / `verified` | `professional_email` / `professional_email_status == "valid"` |
  | contact `phone` | `mobile_phone` or `phone_numbers[0]` |
  | contact `title` | `job_title` |
- **Errors:** `429` → read `Retry-After`/`X-RateLimit-*`, raise `ProviderQuotaError` (trip circuit). `403`
  (insufficient credits OR permission) → raise `ProviderQuotaError` (trip circuit). `401` → log auth error, return `None`/`[]`.
  `400/422` → log + degrade. All other non-200 → degrade to `None`/`[]`.

## 5. Component B — Clay MCP connector

**New file `app/connectors/clay_mcp.py`.** Server-to-server MCP client to `https://api.clay.com/v3/mcp`.

- **Transport/auth (FIRST SPIKE — see §11):** use the Python `mcp` SDK streamable-HTTP client with the Clay API
  key. Auth handshake (expected `Authorization: Bearer {CLAY_API_KEY}`) is the **one unvalidated risk** — the
  spike confirms the exact header/flow against a real call before the rest of the connector is built.
- **`enrich_company(domain) -> dict | None`** — calls MCP tool `find-and-enrich-company` with `companyIdentifier=domain`,
  **no paid `companyDataPoints`** (base firmographics are returned synchronously). Normalization from the **real
  spike response**:
  | App field | Clay source |
  |---|---|
  | `legal_name` | `name` |
  | `domain` | `domain` |
  | `website` | `website` |
  | `industry` | `industry` |
  | `employee_size` | `size` (e.g. `"10,001+ employees"`) or band from `employee_count` |
  | `hq_city / hq_state / hq_country` | parse `locality` (`"Centennial, Colorado"`) + `locations[].inferred_location` (`locality / admin_district / country_iso`) |
  | `linkedin_url` | `url` (company LinkedIn) |
  | `revenue_range` | `annual_revenue` (e.g. `"10B-100B"`) |
  | `ticker` | best-effort regex on `description` (`"(NYSE:ARW)"`); else null |
  Clay does **not** supply `naics` (leave to SAM/Explorium).
- **`find_contacts(domain, title_filter, limit, want_email) -> list[dict]`** — calls `find-and-enrich-contacts-at-company`
  with `contactFilters.job_title_keywords` from `title_filter`. Base contacts return inline. If `want_email`, pass
  `contactDataPoints:[{type:"Email"}]`, take the returned `taskId`, then **bounded-poll** `get-task-context`
  (e.g. 5 tries × ~3s, total ≤ ~20s) until each contact's `enrichments[]` Email reaches state `"completed"`;
  degrade to email-less contacts on timeout. **Filter to the target domain** — the spike showed the contact list
  includes ex-employees at other domains (`getgroup.com`, `infineon.com`); keep only `contact.domain == domain`
  (or `latest_experience_company` matches the account). Map `name→full_name`, `latest_experience_title→title`,
  `url→linkedin_url`, `location_name→location`.
- **Delete the dead webhook path:** remove `clay_service.request_enrichment`, the `POST /api/webhooks/clay`
  endpoint + secret/signature verification (`app/routers/v13_features/activity.py:116-159`), the
  `CLAY_WEBHOOK_URL`/`CLAY_CALLBACK_SECRET` config + `api_sources.json` env vars, and the webhook trigger calls in
  `app/routers/crm/enrichment.py`. **`clay_service.py` is deleted entirely** — contact auto-persist is gone; contacts flow via `find_suggested_contacts()` calling `clay_mcp.find_contacts()`. The Settings `CLAY_API_KEY` card becomes correct.
- **Errors:** MCP error / quota → `ProviderQuotaError` → `trip_circuit("clay", clay_cooldown_minutes)`.

## 6. Component C — Unified blending layer

### 6.1 `app/services/firmo_tiers.py` (new) — the authority ladder

Ports the `spec_tiers.py` mechanic: a per-field authority lookup + a pure `resolve(existing, incoming)` comparator
over `(tier, confidence, updated_at)`; unknown source → tier 0 (with a once-per-process warning); strictly-greater
tuple wins; equal tier breaks on confidence then timestamp.

- **`FIRMO_FIELD_TIER: dict[field, dict[source, int]]`** with a `FIRMO_BASE_TIER` fallback. Base:
  `manual=100, explorium=85, lusha=75, clay=70, apollo=65, sam_gov=60, hunter=40, ai=30`.
  Per-field overrides (only where authority diverges from base):
  | Field | Winner ranking (tier) |
  |---|---|
  | `legal_name` | sam_gov 95 > explorium 85 > lusha 75 > clay 70 > apollo 65 > ai 30 |
  | `naics` | sam_gov 95 > explorium 85 > (others n/a) |
  | `ticker`, `revenue_range` | explorium 90 > clay 75 > ai 30 |
  | `employee_size` | explorium 85 > apollo 75 > lusha 70 > clay 70 > ai 30 |
  | `industry` | explorium 85 > apollo 70 > clay 70 > lusha 65 > ai 30 |
  | `hq_city/state/country` | explorium 85 > sam_gov 80 > apollo 65 > lusha 60 > clay 60 > ai 30 |
  | `website`, `domain` | explorium 80 > clay 70 > apollo 65 > lusha 60 > ai 30 |
  | `linkedin_url` | explorium 85 > lusha 80 > apollo 65 > clay 60 > ai 30 |
- **`CONTACT_FIELD_TIER`** (per contact field):
  | Field | Winner ranking (tier) |
  |---|---|
  | `phone` | lusha 95 > apollo 70 > explorium 65 > hunter 50 > ai 30 |
  | `email` | lusha 95 > hunter 85 > apollo 70 > explorium 65 > ai 30 |
  | `title` | explorium 80 > apollo 75 > lusha 70 > clay 65 > hunter 50 > ai 30 |
  | `full_name`, `linkedin_url` | lusha 80 > apollo 70 > explorium 70 > clay 65 > hunter 50 > ai 30 |
  A **verified** email/phone carries `confidence=0.9` (else `0.5`), so a verified value beats an unverified one of
  the same person within a field even at equal tier.
- A **tier-table sync test** (mirroring `tests/test_migration_096_spec_provenance.py`) guards the tables.

### 6.2 `app/services/enrichment_router.py` (new) — cost-tiered routing + per-run blend

- **Company call order** (each provider's result appended as `(source, dict)` to a per-run list; paid stages
  circuit-guarded + gap-gated via `_gaps_remain()` over the enrichable field set):
  1. **Free, always-run:** `sam_gov`, `apollo` (within free quota).
  2. **Metered, gap-gated, in order:** `clay` (bill-on-result, meta-broker) → `explorium` → `lusha` → `ai` (last).
  Then `blend_company(results)` arbitrates **per field** via `firmo_tiers` → one best-of dict.
- **Contact discovery:** gather free/cheap (`apollo`, `hunter`, `clay` base) concurrently; if `< limit` verified
  contacts, escalate to `lusha` (verified) + `explorium` contacts-enrich + Clay `Email` poll; **dedup across all**
  by `email → linkedin_url → full_name`; on a dedup collision **merge per-field via `CONTACT_FIELD_TIER`** (replaces
  today's first-seen-wins). Keep the existing `_RELEVANT_KEYWORDS` relevance filter.
- This **inverts** today's Explorium-first order to free→metered→AI for cost, while the **tier table — not call
  order — owns conflicts**, so the reorder is safe.

### 6.3 Ladder-aware apply + provenance (migration)

Evolve `apply_enrichment_to_company` / `apply_enrichment_to_vendor` from blind fill-only to ladder-aware, using a
single JSONB column (mirrors `record_spec`'s `specs_structured` approach — no 36-column sidecar explosion):

- **Migration (Alembic):** add `enrichment_provenance JSONB DEFAULT '{}'` to `companies` and `vendor_cards`
  (downgrade drops it). Stores `{field: {source, tier, confidence, updated_at}}`.
- **Extend the written field set** to include `ticker, naics, revenue_range` (currently dropped). Requires those
  columns to exist on `Company`/`VendorCard` — audit in planning; add via the same migration if missing.
- **Safe overwrite rule (per field):**
  - field empty → write the blended value + provenance.
  - field has value **with** provenance tier `T` → overwrite **iff** incoming tier-tuple `> T`'s tuple.
  - field has value **without** provenance → **leave it** (could be a manual edit or trusted legacy; never clobber
    what we can't vouch for). Manual edits should additionally write `provenance.source="manual", tier=100`
    going forward (planning to wire the manual-edit handlers).
- `enrichment_source` stays the composite summary string for display; per-field truth lives in `enrichment_provenance`.

### 6.4 Config normalization

In `app/config.py` add, to remove provider asymmetry: `hunter_enrichment_enabled`, `hunter_cooldown_minutes`,
`apollo_enrichment_enabled`, `apollo_cooldown_minutes`, `sam_gov_*` as needed; keep `clay_enrichment_enabled` (now
gates the MCP path). Make the **Apollo and Hunter connectors raise `ProviderQuotaError` on 402/429** so they get
circuit coverage like Explorium/Lusha. Reuse `get_credential_cached` for every key and
`enrichment_credit_guard` verbatim (new provider keys are just new strings).

### 6.5 Settings UI

Copy the Lusha card (`api_keys.html:9-58`) to add **Explorium** (`EXPLORIUM_API_KEY` →
`/api/sources/explorium_enrichment/credentials`), **Apollo** (`APOLLO_API_KEY` → `apollo_enrichment`), and
**Hunter** (`HUNTER_API_KEY` → `hunter_enrichment`) cards. Extend the `settings_api_keys_tab` view context
(`htmx_views.py`) to pass their masked states. Keep the corrected `CLAY_API_KEY` card.

## 7. Data model / migration summary

- `companies.enrichment_provenance JSONB DEFAULT '{}'`
- `vendor_cards.enrichment_provenance JSONB DEFAULT '{}'`
- (conditional) `ticker / naics / revenue_range` columns on `Company` / `VendorCard` if absent.
- Single Alembic revision, with downgrade. Verify single head (`alembic heads`). Migration-bearing branch deploys
  from `main` post-merge (per project convention).

## 8. Error handling & resilience

- Every paid/metered provider: `circuit_open(name)` guard before call; `trip_circuit(name, cooldown)` on
  `ProviderQuotaError`. Circuit state is process-wide (Redis→PG), so cooldowns span clicks.
- All connectors degrade to `None`/`[]` on non-quota errors — enrichment never raises to the caller.
- Clay MCP poll is bounded; timeout returns email-less contacts rather than hanging the request.

## 9. Testing plan (TDD — write tests first)

Model after `tests/test_enrich_entity_lusha.py` + `tests/test_enrichment_credit_guard.py`:
- `firmo_tiers`: per-field arbitration (higher tier wins, confidence tiebreak, verified bump, unknown→0).
- `enrichment_router`: company call order + gap-gating + cost tier; contact gather/escalate/dedup with per-field merge.
- `explorium` connector: match→firmographics→prospects→contacts mapping; envelope parse; 429/403/401 handling (mock HTTP).
- `clay_mcp` connector: company sync mapping; contact domain-filter; bounded Email poll; quota→circuit (mock MCP client).
- ladder-aware apply: empty-write, higher-tier overwrite, unprovenanced-left-alone, `ticker/naics/revenue` now written.
- migration: upgrade→downgrade→upgrade; column present.
- tier-table sync test (guards `FIRMO_FIELD_TIER`/`CONTACT_FIELD_TIER` against drift).
- Settings: new cards render; credential PUT round-trips (masked).
- Full suite green (`-n auto`), then `/qa`, then the PR-review fleet.

## 10. Rollout

Build behind flags **off** → TDD green → full suite → `/qa` → PR-review fleet → fix all findings →
set `EXPLORIUM_ENRICHMENT_ENABLED=true` + key, `CLAY_ENRICHMENT_ENABLED=true` + key, enable Apollo/Hunter flags →
`./deploy.sh` (migration-bearing → from `main`) → **live-verify**: Clay via the MCP (I have access), Explorium via
the user's key, on a real domain; confirm fields land + provenance recorded.

## 11. Open risks

1. **Clay MCP headless auth (highest):** validated MCP *behavior* via the claude.ai connector, not the
   server-to-server `CLAY_API_KEY` handshake. **First build step is a spike** to confirm the exact transport/header
   before building the connector. If a headless API-key handshake proves unavailable on the Launch plan, fall back
   to documenting the blocker (do not silently degrade).
2. **Explorium credit cost:** paid per-record; gap-gating + circuit limit spend, but firmographics-enrich +
   contacts-enrich are separate billable calls — keep contacts behind the existing per-account cap.
3. **`Company`/`VendorCard` may lack `ticker/naics/revenue_range` columns** — confirm in planning; add if missing.
4. **Manual-edit provenance:** until manual-edit handlers write `source="manual"`, manual values are protected by
   the "no provenance → leave alone" rule (conservative, correct).

## 12. File-by-file change list

- **New:** `app/connectors/explorium.py`, `app/connectors/clay_mcp.py`, `app/services/firmo_tiers.py`,
  `app/services/enrichment_router.py`, `alembic/versions/<rev>_enrichment_provenance.py`, + test files above.
- **Modified:** `app/enrichment_service.py` (façade delegates to router; drop broken Explorium; ladder-aware apply),
  `app/services/prospect_discovery_explorium.py` (use new connector), `app/services/clay_service.py` (keep contact
  helpers, drop webhook), `app/routers/crm/enrichment.py` (drop webhook trigger), `app/routers/v13_features/activity.py`
  (remove webhook endpoint), `app/config.py` (provider flags/cooldowns), `app/connectors/apollo.py` +
  `app/connectors/hunter.py` (raise `ProviderQuotaError` on 402/429), `app/data/api_sources.json` (clay env vars →
  `CLAY_API_KEY`), `app/templates/htmx/partials/settings/api_keys.html` + `app/routers/htmx_views.py` (new cards),
  `app/models/` (provenance columns, conditional firmographic columns).
- **Docs:** update `docs/APP_MAP_INTERACTIONS.md` (enrichment flow + new tier ladder) and `docs/APP_MAP_DATABASE.md`
  (provenance columns) in the same PR.
