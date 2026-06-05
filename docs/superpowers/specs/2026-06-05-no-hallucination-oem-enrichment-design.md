# No-Hallucination OEM / FRU Enrichment

**Date:** 2026-06-05
**Status:** Approved (design) — ready for implementation plan
**Author:** Claude (Opus 4.8) + user
**Branch:** `worktree-oem-enrichment` (worktree)

---

## 1. Problem

1,029 of 1,859 `material_cards` sit at `enrichment_status = not_found`. Sampling shows
they are overwhelmingly **OEM / system-vendor FRU / spare / service part numbers**, not
manufacturer MPNs:

| Pattern | Vendor | Examples (real data) |
|---|---|---|
| `00xxxxx`, `01xxxxx`, `xxLxxxx`, `xxCxxxx` | IBM / Lenovo FRU | `01HW917`, `00E2891`, `38L7669`, `46C9040` |
| `5Bxx…`, `5Cxx…`, `5Txx…` | Lenovo FRU | `5B20L64949`, `5C10Q59981`, `5T10Q96500` |
| `xxxxxx-xxx` | HP / HPE spare | `918042-601`, `619559-001`, `486301-001` |
| `NB.xxxxx.xxx`, `KT.xxxxx.xxx`, `33.xxxxx.xxx` | Acer | `NB.MBC11.003`, `KT.00403.025` |
| `60NB…`, `0Bxxx-…`, `90NB…` | ASUS | `60NB0690-MB1820`, `0b200-00930000` |
| 5-char alnum | Dell | `HV52W`, `66YYK` |

They are `not_found` because every existing tier structurally cannot reach them — **not
because of any hallucination risk**:

1. **Verified tier** — DigiKey / Mouser / Element14 / OEMSecrets / Nexar are *component-
   distributor* catalogs keyed by **manufacturer MPN**. They do not index OEM FRU/spare
   codes. → no hit.
2. **Web-sourced tier** — `trusted_domains.py` allowlists only distributors + chip-maker
   domains (`ti.com`, `st.com`, …). Claude web search *does* find these codes on
   `support.lenovo.com` / HPE PartSurfer, but Gate 1 (trusted-domain) discards the
   correct authoritative page. → rejected.
3. **AI tier** — `ai_inference_fallback` correctly declines obscure FRU codes by design.
   → `not_found`.

The gap is precise: **these parts have real authoritative sources (the OEM's own parts
pages and cross-reference data), but the pipeline does not trust those domains.** The
fix is to *extend authoritative coverage to OEM sources under the same Python-enforced
gate discipline* — never to loosen the guardrails.

A smaller, separate bucket exists: a few genuine component MPNs the verified/web tiers
should have caught (e.g. `M393A2K40EB3-CWEB/C`, a real Samsung DDR4 RDIMM). These are
out of scope here; they are addressed only incidentally by the backfill re-running them.

## 2. Goals / Non-Goals

**Goals**
- Resolve OEM/FRU codes to real data with **zero fabrication** — every written field
  traceable to an authoritative source, enforced in Python (never trusting LLM claims).
- Cover existing 1,029 (one-time backfill) **and** all future OEM/FRU parts (paced worker).
- Surface the new provenance honestly in the UI.

**Non-Goals**
- No scraping of OEM sites (brittle/anti-scraping — a band-aid). Grounded web search only.
- No paid OEM-parts API (none with usable public coverage for FRU codes).
- No structured spec guessing (lifecycle/package/pins/rohs) from the LLM — ever.
- Not solving the genuine-MPN "retry miss" bucket beyond what backfill re-runs incidentally.

## 3. Approach (decided)

Extend the proven grounded-web-search pattern (`web_extractor.py`) to OEM sources with
two new Claude calls, each gated entirely in Python. Two new enrichment tiers slot into
`enrich_card` between the distributor web tier and the AI fallback, gated by a cheap
**OEM-vendor classifier** so non-OEM parts never incur OEM web calls.

```
1. Verified         (distributor connectors)              ── unchanged
2. Web-sourced      (distributor / mfr web search)        ── unchanged
3. NEW ▸ OEM cross-ref     (only if classifier matches)   ── strict double-verify → verified
4. NEW ▸ OEM description   (only if classifier matches)   ── single official page → oem_sourced
5. AI inference     (Opus, flagged)                       ── unchanged
6. Terminal: not_catalogued (classifier matched + OEM tiers ran + nothing) ELSE not_found
```

### 3.1 The no-hallucination guarantees

**Cross-ref (tier 3) — strict, double-verified.** The danger is not a *fake* MPN (the
distributor pipeline rejects those); it is a **real-but-wrong** MPN the LLM confidently
asserts. Two independent guarantees, **both required**:

- **(a) Linkage is sourced.** An allowlisted authoritative page (`is_crossref_domain`)
  must contain *both* the OEM code *and* the candidate MPN verbatim. Verified in Python
  from the returned `source_urls` + an explicit `linkage_quote` field: the OEM code and
  candidate MPN, after `normalize_mpn_key`, must both be substrings of the normalized
  `linkage_quote`, and at least one `source_url` host must be an allowlisted cross-ref
  domain. The FRU↔MPN link itself is evidence — not the model's say-so.
- **(b) MPN is real.** The candidate MPN is fed back through the existing
  `fetch_authoritative` pipeline and must independently clear an exact normalized-MPN
  match at a distributor.

Only if **both** pass do we write the card — as `verified` — using the resolved MPN's
distributor data (via the existing `apply_authoritative` merge), with the FRU↔MPN
linkage recorded in `cross_references` and a `cross_ref` provenance block. Either gate
fails → write nothing, fall through to tier 4.

**OEM description (tier 4) — lenient, single official source.** Claude grounded web
search restricted to OEM-official domains (`is_oem_domain`); the **same four Python
gates** as the distributor web tier (allowlisted OEM domain, exact OEM code verbatim on
the page via `normalize_mpn_key`, confidence ≥ `_MIN_OEM_CONFIDENCE`, non-trivial
description + non-empty vendor). One official page is sufficient → `oem_sourced`.
Writes **description + category only** (+ datasheet_url if present) — never structured
specs. Provenance records the OEM domain + source URL.

### 3.2 Rejected alternatives
- **Scrape PartSurfer / PSREF** — brittle, anti-scraping, high upkeep. Violates "no band-aids".
- **Tiered "unconfirmed cross-ref" badge** — rejected per the chosen trust bar (a wrong
  MPN is dangerous; show nothing rather than a half-confirmed MPN).

## 4. Components

All new worker modules live under `app/services/enrichment_worker/` and mirror the
structure / gate discipline of the existing `web_extractor.py` + `trusted_domains.py`.

### 4.1 `oem_classifier.py` (new, pure)
```python
def classify_oem_vendor(display_mpn: str) -> str | None
```
Returns one of `"lenovo" | "ibm" | "hp" | "hpe" | "dell" | "acer" | "asus"` or `None`,
by matching ordered, anchored regexes against the **raw display_mpn** (uppercased). Pure
and fully unit-tested against the real samples in §1. `None` means "not a recognized
OEM/FRU pattern" → OEM tiers are skipped and a generic failure remains `not_found`.

Patterns (initial; full table fixed in the plan, each justified by a real sample):
- Lenovo/IBM FRU: `^0[01][A-Z0-9]{5}$`, `^\d{2}[A-Z]\d{4}$`, `^5[BCT]\d{2}[A-Z]\d{5}$`
- HP/HPE spare: `^\d{6}-\d{3}$`
- Acer: `^(NB|KT|TC|33|60)\.[A-Z0-9]{5}\.[A-Z0-9]{3}$`
- ASUS: `^(90|60)NB[A-Z0-9]{4}-[A-Z0-9]+$`, `^0[A-Z]\d{3}-\d{8}$`
- Dell: `^[0-9A-Z]{5}$` (last, lowest-priority; 5-char alnum)

Ambiguity is acceptable here (the vendor label only seeds the search prompt; the gates,
not the label, enforce correctness). False positives cost at most a wasted web call;
false negatives just leave a part `not_found` (unchanged from today).

### 4.2 `oem_domains.py` (new)
Mirrors `trusted_domains.py`. Two frozensets + two predicates:
- `OEM_OFFICIAL_DOMAINS` — official vendor parts/support hosts:
  `support.lenovo.com`, `pcsupport.lenovo.com`, `partsurfer.hpe.com`,
  `support.hp.com`, `www.dell.com`, `dell.com`, `www.acer.com`, `www.asus.com`, etc.
  (full list fixed in plan).
- `CROSS_REF_AUTHORITATIVE_DOMAINS` — `OEM_OFFICIAL_DOMAINS` ∪ the existing distributor +
  manufacturer allowlist (`trusted_domains`), since a distributor/mfr page that lists the
  FRU alongside the commodity MPN is also authoritative for the *linkage*.
- `is_oem_domain(url) -> bool`, `is_crossref_domain(url) -> bool` — exact-host match,
  dot-suffix for vendor roots, reject non-http(s); identical safety model to
  `is_trusted_domain` (`evil-lenovo.com` must NOT match `lenovo.com`).

### 4.3 `oem_extractor.py` (new)
Mirrors `web_extractor.py`. Two dataclasses + two functions; **all gates in Python**,
never trusting the model's gate claims. Each raises `ClaudeError` on backend failure
(so the circuit breaker sees outages); a genuine "not found" reply is parsed, not raised.

```python
@dataclass
class CrossRefResult:
    status: str  # "resolved" | "failed"
    resolved_mpn: str | None = None
    manufacturer: str | None = None
    linkage_source_url: str | None = None
    linkage_source_domain: str | None = None
    confidence: float = 0.0

async def cross_reference_mpn(display_mpn, normalized_mpn, vendor, *, timeout=90) -> CrossRefResult
```
- Claude `web_search_20250305`, `model_tier="smart"`. Prompt asks for the manufacturer
  MPN the OEM code corresponds to **and** a `linkage_quote` (verbatim text from a page
  showing both), plus `source_urls`, `confidence`.
- Gates: (1) ≥1 `source_url` host ∈ cross-ref allowlist; (2) `normalize_mpn_key(oem) in
  norm(linkage_quote)` AND `normalize_mpn_key(resolved_mpn) in norm(linkage_quote)`;
  (3) `resolved_mpn` normalizes to something ≠ the OEM code (a real cross-ref, not echo);
  (4) `confidence ≥ _MIN_CROSSREF_CONFIDENCE` (0.90). Failure → `status="failed"`.
- Returns only the *candidate*; distributor re-verification (gate b) happens in
  `enrich_card`, not here (single responsibility).

```python
@dataclass
class OemExtractResult:  # same shape as WebExtractResult, minus distributor semantics
    status: str  # "oem_sourced" | "failed"
    description / manufacturer / category / datasheet_url / confidence / source_urls / source_domains

async def extract_oem_description(display_mpn, normalized_mpn, vendor, *, timeout=90) -> OemExtractResult
```
- Same four gates as `extract_part_from_web`, but Gate 1 uses `is_oem_domain` and
  `_MIN_OEM_CONFIDENCE` (0.90). Description + category + optional datasheet only.

### 4.4 `authoritative_enrichment_service.py` (modify `enrich_card` + 2 new appliers)

New parameter (backward-compatible): `web_meter: dict[str, int] | None = None`.
`enrich_card` increments `web_meter["calls"]` by **1 for every billable web-search Claude
call it makes** (distributor web tier, cross-ref, OEM description). The worker reads the
delta to keep `web_daily_cap` exact regardless of how many web tiers fire. Default `None`
→ no metering (tests / `enrich_cards`).

Insertion (after the distributor web tier fails, before AI fallback), only when
`classify_oem_vendor(card.display_mpn)` is truthy **and** the web tier is enabled
(`"web_search" not in disabled`):

```
vendor = classify_oem_vendor(card.display_mpn)
if vendor and web_enabled:
    # Tier 3: cross-ref (strict double-verify)
    meter web call
    xr = await cross_reference_mpn(display_mpn, normalized_mpn, vendor)
    if xr.status == "resolved":
        results2 = await fetch_authoritative(xr.resolved_mpn, normalize_mpn_key(xr.resolved_mpn), conns, disabled, cooldown)
        merged2, prov2, contrib2 = merge_authoritative(normalize_mpn_key(xr.resolved_mpn), results2)
        if merged2:                       # gate (b): MPN independently confirmed
            apply_cross_ref_verified(card, merged2, prov2, contrib2, xr)
            return VERIFIED
    # Tier 4: OEM description (single official page)
    meter web call
    oem = await extract_oem_description(display_mpn, normalized_mpn, vendor)
    if oem.status == "oem_sourced":
        apply_oem_sourced(card, oem)
        return OEM_SOURCED
    oem_attempted = True
```

`apply_cross_ref_verified(card, merged, provenance, contributors, xr)` — writes merged
distributor fields (as `apply_authoritative`), sets `enrichment_status=verified`,
`enrichment_source = contributors[0]`, appends `{mpn, manufacturer}` to
`card.cross_references`, and adds a top-level `cross_ref` provenance block:
`{"oem_part": display_mpn, "resolved_mpn": xr.resolved_mpn, "linkage_source_url": …,
"linkage_source_domain": …, "confirmed_by": contributors[0], "confidence": xr.confidence}`.

`apply_oem_sourced(card, oem)` — mirrors `apply_web_sourced`, status `oem_sourced`,
`enrichment_source="oem_official"`, provenance `{"oem_sourced": True, confidence,
source_urls, source_domains, fetched_at, <field>: {...}}`.

**Terminal decision.** Replace the final `not_found` assignment with:
```
card.enrichment_status = NOT_CATALOGUED if (vendor and oem_attempted) else NOT_FOUND
```
`not_catalogued` only when an OEM pattern matched *and* the OEM tiers actually ran and
found nothing (so web-budget-skipped parts stay `not_found` and get a real attempt
later). Both terminals keep `enrichment_source=None`, `enrichment_provenance=None`.

### 4.4.1 Worker changes (`worker.py`, `run_one_batch`)
Two precise edits, both forced by the new tiers:
- **Budget accounting.** Replace the current "`web_calls_today += 1` per non-verified
  card" heuristic (which assumed exactly one web call) with **`web_calls_today +=
  web_meter delta`**: pass a fresh `web_meter={"calls":0}` into each `enrich_card`, read
  `web_meter["calls"]` after, add that to the running total, and persist to the cache.
  This stays exact when a single card fires 1–3 web calls.
- **Circuit-breaker reset.** The meter is `{"web_calls": int, "claude_ok": bool}`:
  `web_calls` counts billable web-search calls (budget); `claude_ok` is set True after
  **any** Claude call (web tier, cross-ref, OEM description, *or* `infer_part`) returns
  without raising. Worker resets the breaker when `claude_ok` is True, replacing the
  current `status != VERIFIED` proxy. That proxy is now wrong on both ends: a **cross-ref
  result is `verified` yet did call Claude** (must reset), while a plain distributor
  `verified` makes no Claude call (must not). `claude_ok` is the precise signal; budget
  uses `web_calls` so a non-web Claude success (AI-inferred) never miscounts spend.

### 4.5 `constants.py` + model validator
Add to `MaterialEnrichmentStatus`: `OEM_SOURCED = "oem_sourced"`, `NOT_CATALOGUED =
"not_catalogued"`. Both ≤ 20 chars → fit `String(20)`. The `@validates` validator is
data-driven (`MaterialEnrichmentStatus(value)`) and accepts them automatically. Update
the inline column comment in `intelligence.py`. **No migration** (varchar column, not a
native PG enum).

### 4.6 Worker retry backoff for `not_catalogued`
`config.py`: add `not_catalogued_retry_days: int = 30` (+ env
`ENRICHMENT_NOT_CATALOGUED_RETRY_DAYS`). `select_batch` gains a third eligibility arm:
`not_catalogued` cards become eligible only when `enriched_at < now - retry_days` (long
backoff — terminal-ish, but self-heals as OEM catalogs change). `not_catalogued` parts
are otherwise treated like the others for ordering/exclusions.

### 4.7 UI
- **Badge** `partials/materials/list.html` (the `{% elif es == … %}` chain, after
  `web_sourced`): add `oem_sourced` (indigo badge "OEM-SOURCED", linked to first
  `source_urls`, title cites OEM domain) and `not_catalogued` (slate badge "OEM SERVICE
  PART · NOT CATALOGUED", title "Recognised OEM service part; no public specs published").
- **Filter** `partials/materials/workspace.html`: add an `oemSourced` and a
  `notCatalogued` toggle following the exact `webSourced` toggle markup (lines 55-64) and
  the `statusList.push(...)` wiring (JS lines 194-204). New Alpine state keys default
  `false`. The faceted service already filters on `statuses` — no service change needed.

### 4.8 Backfill script `scripts/backfill_oem_enrichment.py` (new)
- Selects all `material_cards` with `enrichment_status='not_found'` (optionally
  `not_catalogued`) and `is_internal_part=False`, `deleted_at IS NULL`.
- **Dry-run default**: runs `enrich_card` against a **throwaway** flow that does NOT
  commit, tallies projected outcomes (`verified` / `oem_sourced` / `not_catalogued` /
  still-`not_found` / `ai_inferred`), and writes a coverage CSV
  (`backfill_oem_coverage_<runstamp>.csv`: display_mpn, vendor, projected_status,
  resolved_mpn, source). Prints a summary table. **Writes nothing.**
- `--commit`: same pass but persists (reusing `enrich_cards`' batched-commit + bounded
  concurrency), with its own `--max-web-calls` budget cap (default 300) enforced via a
  shared `web_meter`, and `--limit N`. Stops cleanly when the web budget is exhausted
  (remaining parts left for the worker).
- Mirrors `scripts/import_part_numbers.py` CLI conventions (argparse, Loguru, `TESTING`
  guard off). Run only under explicit user authorization.

## 5. Data flow (cross-ref happy path)

```
worker.select_batch → enrich_card(01HW917)
  fetch_authoritative(01HW917) → {} (distributors don't index FRU)
  distributor web tier → failed (no trusted distributor page for FRU)
  classify_oem_vendor("01HW917") → "lenovo"   [web_enabled]
  cross_reference_mpn → resolved_mpn="M393A2K40EB3", linkage_quote contains both, domain ok, conf 0.94
    fetch_authoritative("M393A2K40EB3") → mouser exact hit  (gate b ✓)
    apply_cross_ref_verified → status=verified, cross_references=[{mpn:M393…, mfr:Samsung}],
                               provenance.cross_ref={oem_part:01HW917, resolved_mpn:…, confirmed_by:mouser}
  return VERIFIED
```
Cross-ref miss → OEM description hit → `oem_sourced`. Both miss → `not_catalogued`.

## 6. Error handling
- `ClaudeError` from any OEM Claude call propagates out of `enrich_card` exactly like the
  web tier today → worker's circuit breaker counts it; card left unenriched & retried.
  Never silently marked `not_found`/`not_catalogued` on a backend outage.
- All OEM web calls are gated by `"web_search" not in disabled` and metered into
  `web_meter`, so `web_daily_cap` is respected to the call.
- Connector quota/auth during cross-ref gate (b) reuses the existing `disabled`/`cooldown`
  semantics of `fetch_authoritative`.
- Classifier and domain predicates never raise on malformed input (return `None`/`False`).

## 7. Testing (TDD — tests first for every unit)
- `test_oem_classifier.py` — truth table over every §1 sample + negatives (real MPNs like
  `LM2596S`, `M393A2K40EB3` must classify `None`).
- `test_oem_domains.py` — allowlist membership; `evil-lenovo.com`/`lenovo.com.evil.com`
  rejected; non-http rejected; cross-ref superset includes distributors.
- `test_oem_extractor.py` — mocked `claude_json`: cross-ref accept; reject on each gate
  (untrusted domain, linkage_quote missing either code, echo MPN, low confidence);
  `ClaudeError` re-raised. Same matrix for `extract_oem_description`.
- `test_authoritative_enrichment.py` (extend) — `enrich_card` tiers: cross-ref double-
  verify accepted→verified (+cross_references/provenance); resolved-but-unconfirmed MPN
  rejected→falls through; real-but-wrong MPN that fails distributor re-verify rejected;
  oem_sourced path; `not_catalogued` vs `not_found` terminal selection (vendor match +
  oem_attempted vs web-disabled); `web_meter` increments exactly per billable call.
- `test_enrichment_status_enum.py` / `test_constants.py` (extend) — new members valid;
  `@validates` accepts `oem_sourced`/`not_catalogued`, rejects junk.
- `test_enrichment_worker.py` (extend) — `select_batch` `not_catalogued` eligibility
  (long backoff window); budget accounting with metered multi-call cards.
- `test_materials_router*.py` / template test — both new badges render; both filter
  toggles produce the right `statuses`.
- `test_backfill_oem_enrichment.py` — dry-run writes coverage CSV + commits nothing;
  `--commit` persists; `--max-web-calls` halts cleanly.

## 8. Rollout
1. Land worker + tiers (covers all future OEM/FRU parts automatically).
2. Run backfill **dry-run** over the 1,029 → review coverage CSV with the user.
3. On explicit authorization, backfill `--commit` (bounded web budget; remainder drains
   via the worker).
4. Update `docs/APP_MAP_ARCHITECTURE.md`, `APP_MAP_DATABASE.md` (new statuses,
   `cross_references` usage), `APP_MAP_INTERACTIONS.md` (new tiers + flow) in the same PR.
5. Deploy via `./deploy.sh` (only on explicit authorization; verify new Tailwind badge
   colors — `indigo`/`slate` — appear in built CSS, per the safelist rule).

## 9. Risks & mitigations
- **Real-but-wrong cross-ref MPN** → the dual gate (sourced linkage + independent
  distributor confirm) is specifically designed to stop this; tested explicitly.
- **Web spend** → classifier gate + `web_meter` + `web_daily_cap` + backfill budget cap.
- **Tailwind new colors not in build** → verify post-deploy per existing rule; add to
  safelist if purged.
- **`String(20)` overflow** → longest new value `not_catalogued` = 14 chars. Safe.
```
