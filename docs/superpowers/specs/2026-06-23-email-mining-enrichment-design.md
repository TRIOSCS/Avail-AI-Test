# Email-Mining Enrichment (Hybrid) — Design

**Date:** 2026-06-23
**Status:** Approved

## Problem

`prospect_scheduler` calls `run_email_mining_batch(batch_id, graph, db)` with no
`enrich_fn`. `enrich_email_domains` skips every domain whose `enrich_fn` returns nothing,
so with `enrich_fn=None` the inbox-mining path discovers unknown domains but creates
**zero** prospects. The feature is inert (pre-existing; surfaced during the Apollo
removal). `mine_unknown_domains` already does the hard part: it excludes known
customer/vendor/prospect/freemail/internal domains and keeps only domains with 2+ inbox
emails over 90 days, sorted by email volume.

## Goal

Make email-mining actually produce prospects, hybrid-style: capture every qualifying
domain cheaply, eagerly enrich the most important ones, defer the long tail.

## Behavior

1. **Discovery (always, zero cost).** Every domain from `mine_unknown_domains` becomes a
   `ProspectAccountCreate` from the email signal alone: `name = domain`,
   `website = https://{domain}`, `discovery_source = "email_history"` (unchanged), with
   `email_count` + `sample_senders` in `enrichment_data["email_mining"]`. No domain is
   dropped.
2. **Eager enrichment (capped, cheap).** The top `enrich_cap` domains by email volume
   (domains are already sorted desc) get **one Explorium domain-match each**; on success
   the firmographics are merged into that prospect. Explorium-only — not the Clay/Lusha
   blend — so cost is predictable: ≤ `enrich_cap` Explorium calls per run.
3. **Deferred (long tail).** Domains past the cap, and Explorium misses, are created
   unenriched and remain enrichable later via the prospect's existing on-demand path. No
   new mechanism.

## Components

- **`_explorium_domain_enrich(domain) -> dict | None`** (new, in
  `app/services/prospect_discovery_email.py`): the injectable `enrich_fn`. Self-gates:
  returns `None` when `explorium_enrichment_enabled` is false, the credential is missing,
  or the `explorium` circuit is open. Otherwise calls
  `explorium.enrich_company(domain, "", api_key)` and maps the **CRM shape → prospect
  shape** via `_map_explorium_to_prospect`. Raises nothing the caller must handle beyond
  the existing `try/except` (returns `None` on `ProviderQuotaError` after tripping the
  circuit, matching the connector contract).
- **`_map_explorium_to_prospect(c: dict) -> dict`** (new helper): `legal_name→name`,
  `industry→industry`, `naics→naics_code`, `employee_size→employee_count_range`,
  `revenue_range→revenue_range`, `website→website`, `(hq_city, hq_state, hq_country)→
  hq_location` (comma-joined, non-empty), `hq_country→region` (reusing the existing
  `prospect_discovery_explorium._detect_region` mapping), `discovery_source="explorium"`.
- **`enrich_email_domains(domains, enrich_fn=None, enrich_cap=25)`** (rewritten): always
  build a base prospect per domain; for the first `enrich_cap` domains call `enrich_fn`
  and merge any result over the base. Scoring (`calculate_fit_score`,
  `calculate_readiness_score`) runs on the merged data (bare prospects score low, which
  is correct).
- **`run_email_mining_batch(..., enrich_fn=None, enrich_cap=None, days_back=90)`**:
  defaults `enrich_cap` from `settings.email_mining_enrich_cap`; passes both through.
- **`prospect_scheduler`**: the email-mining call passes
  `enrich_fn=_explorium_domain_enrich`. Dependency-injected so tests stay hermetic.
- **`config.py`**: `email_mining_enrich_cap: int = 25` (the only spend lever for this path).

## Data flow

`mine_unknown_domains` (sorted by email_count desc) → `enrich_email_domains`: for each
domain build base prospect; for index < cap, `enrich_fn(domain)` → merge → score →
`ProspectAccountCreate` → `_persist_discovery_results` (existing) writes `ProspectAccount`
rows.

## Error handling

- Explorium failure / quota / disabled / circuit-open → `enrich_fn` returns `None` → that
  domain stays a bare prospect; the batch continues (no domain lost).
- Graph/API failure in `mine_unknown_domains` is already handled (returns `[]`).
- The whole email-mining block in the scheduler is already wrapped in try/except +
  rollback.

## Testing

- `_map_explorium_to_prospect`: CRM→prospect field mapping incl. `hq_location` join and
  `region` from country; missing fields → `None`.
- `_explorium_domain_enrich`: disabled flag → `None` (no call); circuit-open → `None`;
  success → mapped dict; `ProviderQuotaError` → `None`.
- `enrich_email_domains`: every domain → a prospect (signal-only when no enrich_fn); cap
  honored (only first N call `enrich_fn`); enriched prospect carries firmographics; an
  Explorium miss → bare prospect; `enrichment_data["email_mining"]` populated.
- `run_email_mining_batch`: pulls `enrich_cap` from settings; no domains → `[]`.

## Cost

≤ `email_mining_enrich_cap` (default 25) Explorium calls per scheduled run; zero
Clay/Lusha. Discovery itself is free.

## Out of scope

- Changing the scheduler frequency or `mine_unknown_domains` filters.
- A dedicated background re-enrichment pass for the deferred tail (the existing on-demand
  path covers it).
