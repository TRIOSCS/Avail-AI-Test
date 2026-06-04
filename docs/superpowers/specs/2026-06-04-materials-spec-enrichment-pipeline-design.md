# Materials Structured-Spec Enrichment Pipeline + Dependent Filter Fixes

**Date:** 2026-06-04
**Branch:** `feat/materials-spec-enrichment`
**Status:** Design — awaiting review
**Author:** Claude (Opus 4.8), with mkhoury

---

## 1. Problem

A verified audit of the Materials tab (32 active material cards, live DB) found the
spec-driven faceting layer is **dormant**, and the bugs that surface once it has data
are latent:

- `material_spec_facets` has **0 rows**; `specs_structured` is **NULL on all 32 cards**.
- `record_spec()` (`app/services/spec_write_service.py`) is the *only* writer of both
  `specs_structured` and `material_spec_facets`, and it is **unreachable from any
  automated path**. The live card-level enrichment (`enrich_material_cards` /
  `batch_enrich_materials`) and the scheduled job (`jobs/tagging_jobs.py:_job_material_enrichment`,
  every 2h) only write `description`/`category`/`lifecycle_status`. The only callers of
  `record_spec` are the manual scripts `scripts/enrich_specs_batch.py` and
  `app/management/reenrich.py`, which nobody has run.
- Consequence — **spec sub-filters are broken**: enum filters silently render nothing,
  numeric filters show "No data available", and **boolean toggles render clickable
  Yes/No controls that return ZERO results** (they query the empty facet table). The
  **primary-spec chips** on every list card are likewise always blank.

The cards *were* card-level enriched (`enriched_at` set 2026-03-30, `claude_haiku`); only
the **spec→facet projection** never ran, and nothing makes it run.

### Verified findings this spec resolves

| Sev | Finding | Resolved by |
|---|---|---|
| CRITICAL | Spec/facet layer never populated; no automated path calls `record_spec` | §4 pipeline + §6 backfill + §5 wiring |
| HIGH | Boolean toggles render with zero data and return 0 results | §4 (data) + §7.2 (guard) |
| HIGH | `/filters/sub` doesn't pop `manufacturers` → zeroes facet counts once specs exist | §7.1 |
| LOW | Primary-spec chips always blank | §4 (data) |
| LOW | `$0.00` best price renders `--` (Jinja falsy-zero) | §7.3 |
| LOW | Currency symbol `$` hardcoded, ignores `last_currency` | §7.4 |
| LOW | Active-filter chip label disagrees with tree (`Analog Ic` vs `Analog ICs`) | §7.5 |

### Deferred (explicit follow-ups, NOT in this spec)

- Manufacturer alias canonicalization (`ST` vs `STMicroelectronics`).
- Faceted self-exclusion (within-facet pivot counts).
- Full multi-currency best-price normalization (this spec only fixes the *label*).

---

## 2. Goals / Non-goals

**Goals**
1. Populate `specs_structured` + `material_spec_facets` for the existing 32 cards
   (one-time backfill) and keep them populated automatically as cards are added/enriched.
2. Make the spec sub-filters and primary-spec chips work because real data now exists.
3. Fix the verified filter/render bugs that the new spec data activates or that are
   independently dormant.

**Non-goals**
- No UI elements added, removed, or rearranged. Filters and cards keep their current
  shape; they gain data and correct behavior only.
- No change to the existing card-level enrichment contract.
- No new commodity schemas; reuse `commodity_spec_schemas` (92 rows seeded).
- The legacy Batch-API script (`scripts/enrich_specs_batch.py`) is retained for very
  large bulk runs; it is refactored to share helpers, not deleted.

---

## 3. Key decisions (confirmed)

- **Trigger model:** background scheduled job + live Enrich button (synchronous for the
  one clicked card). One-time backfill of the 32 existing cards.
- **PR scope:** spec pipeline + backfill + the five dependent filter fixes (§7).
  Manufacturer aliasing deferred.
- **Model / thresholds (reuse existing, proven values):** `model_tier="smart"` for spec
  extraction; record a facet when `confidence >= 0.70`; include in the human-readable
  `specs_summary` when `confidence >= 0.85`.
- **Spec extraction is a SECOND pass** after card-level enrichment, because specs are
  per-commodity (need `category` first) and cannot be folded into the single fixed
  card-level schema.

---

## 4. Component: `app/services/spec_enrichment_service.py` (new)

Real-time, per-commodity spec extraction. The synchronous analogue of
`scripts/enrich_specs_batch.py`, using `claude_structured` (like the live card-level
path) instead of the Batch API. The prompt/schema/summary helpers become the single
source of truth here; the legacy script imports them.

**Shared helpers (moved from the script, made public):**
- `build_spec_prompt(category: str, cards: list[dict]) -> str`
- `build_spec_schema(category: str) -> dict`  (dynamic per-commodity JSON schema)
- `specs_to_summary(category: str, ai_part: dict, *, min_conf: float = 0.85) -> str | None`

These read commodity specs from `commodity_registry.get_batch_spec_schema()`.

**`async def enrich_card_specs(card_ids, db, *, force=False) -> dict`**
1. Load active cards (`deleted_at IS NULL`) in `card_ids` with `category` set and a
   non-empty `description`. When `force=False`, additionally require
   `specs_enriched_at IS NULL`.
2. Group cards by `category`. Skip any category absent from
   `get_batch_spec_schema()` (count as `skipped_no_schema`).
3. For each category, chunk (`batch_size=25`) and call:
   `claude_structured(prompt, schema, system=<spec-extraction system>, model_tier="smart", max_tokens=8192, timeout=120)`.
4. Map returned `parts` back to cards by `mpn` (dict `display_mpn → card_id`, positional
   fallback when the AI preserves order). For each spec key:
   - if `value is not None and confidence >= 0.70`: call
     `record_spec(db, card_id, key, value, source="spec_extraction", confidence=conf, unit=<canonical_unit from registry>)`.
     `record_spec` already validates enum membership / numeric parsing and writes both
     `specs_structured` and the facet row.
   - Build `specs_summary` via `specs_to_summary` (>= 0.85) and set it on the card.
5. **Always** set `card.specs_enriched_at = now()` for every processed card (even if no
   spec cleared the threshold) so it is not reprocessed and re-billed.
6. Commit per category. A `claude_structured` failure for one category is logged, counted
   in `errors`, and does not abort the other categories.
7. Returns `{"cards_processed", "specs_written", "cards_with_specs", "errors", "skipped_no_schema"}`.

**`async def enrich_pending_specs(db, *, limit=300, batch_size=25) -> dict`**
- Selects eligible card ids: `specs_enriched_at IS NULL`, `category IS NOT NULL`,
  `description` non-empty, `deleted_at IS NULL`, ordered by `search_count DESC NULLS LAST`,
  limited. Delegates to `enrich_card_specs(force=False)`.

> Implementation note: confirm `claude_structured` accepts `model_tier="smart"`
> (card-level uses `"fast"`; the Batch script uses `"smart"`). If the tier name differs,
> use the smart/quality tier constant the codebase already exposes.

---

## 5. Component: wiring the three triggers

**5a. Scheduled job** — `app/jobs/tagging_jobs.py:_job_material_enrichment`
After the existing `enrich_pending_cards(...)` call, add a spec pass:
`stats = await enrich_pending_specs(db); logger.info("material spec enrichment: {}", stats)`.
This also performs the backfill of the 32 cards on the first tick after deploy.

**5b. Enrich button** — `app/routers/htmx_views.py` POST
`/v2/partials/materials/{material_id}/enrich` (~line 8679)
After `await enrich_material_cards([material_id], db)`, add:
```python
try:
    await enrich_card_specs([material_id], db, force=True)
except Exception as e:                      # noqa: BLE001 — card-level enrich already succeeded
    logger.warning("spec enrichment failed for card {}: {}", material_id, e)
db.refresh(card)                            # re-read so the returned detail shows specs
```
Spec failure must NOT 500 the endpoint — the card-level enrichment still succeeded.

**5c. Backfill (one-time, in-session)** — `app/management/enrich_specs.py` (new, thin)
A management entrypoint that calls `enrich_pending_specs(db, limit=100)` and logs stats.
Run once after merge/deploy with explicit authorization. (The scheduled job would also
pick the cards up, but the command makes the backfill deterministic.)

---

## 6. Component: migration (idempotency marker)

`alembic revision --autogenerate -m "add specs_enriched_at to material_cards"`

Add `material_cards.specs_enriched_at` — `UTCDateTime`, nullable, indexed. Mirrors the
existing `enriched_at`. Prevents re-paying for AI on cards that legitimately yielded no
confident specs (a bare `specs_structured IS NULL` check would reprocess them forever).
Review the generated migration, test upgrade → downgrade → upgrade. Rollback drops the
index + column. Add the column to the `MaterialCard` model in `app/models/intelligence.py`.

---

## 7. Component: dependent filter fixes (same PR)

**7.1 `/filters/sub` manufacturers pop** — `htmx_views.py:materials_filters_sub_partial` (~7159)
Before `get_facet_counts(...)`, `parsed_filters.pop("manufacturers", None)` (mirror the
faceted endpoint at ~7214). Otherwise a selected manufacturer is treated as a bogus
`spec_key` and zeroes every sibling facet count once facets exist.

**7.2 Boolean toggle guard** — `faceted_search_service.get_subfilter_options` + `subfilters.html`
In `get_subfilter_options`, set boolean `option["values"] = ["true","false"]` **only if**
`schema.spec_key in text_map` (i.e. it has facet rows); else `[]`. In `subfilters.html`,
change the boolean branch to `{% elif opt.data_type == 'boolean' and opt['values'] %}`
(same emptiness guard as enum). No more clickable dead toggles.

**7.3 `$0` best price** — `list.html:65`
`{% if m._best_price is not none %}` (a genuine `$0.0000` offer must render, not `--`).

**7.4 Currency label (bounded)** — faceted endpoint + `list.html:66`
Extend the per-card vendor aggregate to also select
`count(distinct last_currency)` and `max(last_currency)`. Derive
`m._best_currency = <that currency> if distinct_count == 1 else None`. Template renders
`{{ '$' if m._best_currency in (None, 'USD') else m._best_currency ~ ' ' }}{{ '%.4f'|format(m._best_price) }}`.
Portable across SQLite (tests) and PostgreSQL. Correct for single-currency cards (the real
case); mixed-currency cards keep `$` — full normalization is the deferred follow-up.

**7.5 Chip label casing** — `workspace.html` + `htmx_app.js` + `materials_workspace_partial`
- In `materials_workspace_partial` ctx, add `display_names` (the full
  `{sub: get_display_name(sub)}` map, as the tree partial already builds).
- In `workspace.html`, put the map on the root element as a **single-quoted** attribute
  (`tojson` escapes `'`, per the CLAUDE.md Alpine-quote rule):
  `data-display-names='{{ display_names|tojson }}'` on `#materials-workspace`.
- In `materialsFilter.init()`: `this.displayNames = JSON.parse(this.$el.dataset.displayNames || '{}')`.
- `commodityDisplayName` getter returns `this.displayNames[this.commodity] || <existing title-case fallback>`.
  Fixes both the click path and the URL-restore path.

---

## 8. Data flow (after this change)

1. Card created (sourcing) → **card-level** enrichment (description/category/lifecycle)
   via scheduled job / Enrich button → sets `enriched_at`.
2. **NEW spec pass** (scheduled job + Enrich button + one-time backfill) → per-commodity
   `claude_structured` extraction → `record_spec()` → `specs_structured` (JSONB) +
   `material_spec_facets` rows + `specs_summary`; sets `specs_enriched_at`.
3. Faceted UI reads facets → enum/numeric/boolean sub-filters populate, primary chips
   render, filtering returns real results.

---

## 9. Tests (always included)

**pytest** (`tests/test_spec_enrichment_service.py`, new) — mock `claude_structured`:
- conf ≥ 0.70 → `record_spec` called + facet row created; conf < 0.70 → skipped.
- `specs_summary` set from ≥ 0.85 values.
- `specs_enriched_at` stamped for every processed card (even zero-spec cards).
- cards without `description`/`category`/matching schema are skipped (counted).
- per-category grouping; multiple categories in one call.
- idempotency: `force=False` skips already-stamped cards; `force=True` reprocesses.
- one category's `claude_structured` raising → others still processed, `errors` counted.

**pytest** (extend `tests/` faceted coverage):
- `get_subfilter_options` boolean gating (no facet rows → boolean `values == []` → not rendered).
- `/filters/sub` with a selected manufacturer does NOT zero enum facet counts.
- list best-price `is not none`: a `$0` card renders `$0.0000`, not `--`.
- currency: single non-USD currency card renders the ISO code; USD renders `$`.

**pytest** (`tests/` enrich button): mock claude, POST `/enrich`, assert
`enrich_card_specs` invoked (and detail re-rendered without 500 on spec failure).

**Vitest** (`tests/frontend/`): `materialsFilter.commodityDisplayName` returns the
injected map value (e.g. `analog_ic → "Analog ICs"`) and falls back to title-case for
unmapped keys.

---

## 10. Docs (per repo rule: update APP_MAP in the same PR)

- `docs/APP_MAP_INTERACTIONS.md` — enrichment flow: add the spec second-pass and its
  three triggers.
- `docs/APP_MAP_DATABASE.md` — `material_cards.specs_enriched_at`.
- `docs/APP_MAP_ARCHITECTURE.md` — new `spec_enrichment_service`.

---

## 11. Build order

1. Migration + model field (§6).
2. `spec_enrichment_service` + unit tests (§4, §9) — TDD.
3. Wire scheduled job + Enrich button + backfill command (§5).
4. Filter fixes 7.1–7.5 + their tests (§7, §9).
5. Backfill the 32 cards in-session; verify facets populate and sub-filters work.
6. APP_MAP docs (§10). Full `pre-commit run --all-files`, suite, review, deploy.
