# Enrichment Pipeline Correctness & Data-Acquisition Program — Design Spec

**Date:** 2026-06-09 · **Status:** for review (no code until approved) · **Owner:** materials/enrichment

## 1. Why this exists

The materials filters are empty because the enrichment pipeline is broken in five compounding ways, all evidence-backed by a read-only audit (`/root/overnight_enrichment/pipeline_audit.md`) and an overnight performance capture (`/root/overnight_enrichment/metrics.jsonl`, 49 snapshots / 16 h):

1. **Active pollution** — an ungated legacy *Haiku* path writes vague guess descriptions ("Lenovo proprietary component, *likely* a battery…") with no confidence/provenance/status; the guess persists through `not_found` and seeds hallucinated specs. (`material_enrichment_enabled=true` in prod.)
2. **Categorization leaks junk** — two write paths store the raw AI/distributor string when it doesn't map (`… or raw`), producing 32 non-canonical category strings that match no schema → those cards get no specs and sit in an infinite re-skip loop.
3. **"Latest write wins"** — `record_spec` only protects vendor-API specs; otherwise the last writer wins (confidence ignored). The 0.95 deterministic decode is protected only by execution *ordering* — a band-aid.
4. **Cards freeze permanently** — six freeze traps mean a card that reaches `verified/web_sourced/oem_sourced/ai_inferred` is never re-selected; zero-facet spec passes are stamped terminal; the manual button doesn't even re-enter the ladder. There is **no re-trigger signal anywhere**. Stale `not_found` re-checks can burn the entire 80/day web budget for **zero** resolutions.
5. **OEM-resolution starved of sources** — distributor APIs return 0 for FRUs; the only resolver wired is a near-zero-yield Claude web tier; **BrokerBin is fully configured but not even in the source order**.

**Overnight baseline (measured):** 1,859 cards · 53% uncategorized · ~12% have any facet · 88% `not_found` · worker resolved **0** new parts overnight (25 enriched, all `not_found`).

## 2. Goal & success metrics

Make the pipeline **accurate** (no junk categories, no guess-derived specs, good data always beats worse) and **stable/self-healing** (no card frozen forever; new data sources automatically re-run the backlog; wasted re-checks eliminated), then **acquire real data** for the OEM/FRU mass.

| Metric | Baseline | Target |
|---|---|---|
| Non-canonical categories in DB | 32 strings / ~50 rows | **0** |
| Specs seeded from a guess description | 310 cards | **0** (gated out) |
| Cards permanently frozen (unreachable by worker) | all terminal statuses | **0** (version-bump re-run) |
| `not_found` web-budget waste | up to 80/80/day, 0 resolutions | ≤30/80 reserved for re-checks; ≥50 for new |
| Provenance on facets/category | none | source+confidence+tier on every write |
| `not_found` OEM cards resolved (sample of 200, post-BrokerBin) | ~0% | **≥15%** |

## 3. Shared foundation (F1–F4) — used across sub-projects

**F1 — Source→tier ladder** (new `app/services/spec_tiers.py`): `SOURCE_TIER = { manual:100; vendor_api (digikey/mouser/nexar/element14/oemsecrets _api):90; mpn_decode:85; oem_scrape (partsurfer/psref):80; web_search:70; brokerbin:65; spec_extraction:60; ai_guess/claude_opus_inferred:40 }`. `resolve(existing, incoming) -> bool`: incoming wins iff `(tier, confidence, updated_at)` is lexicographically greater. **Higher tier always overrides; equal tier → higher confidence; tie → newer.** Replaces the vendor-only special-case + "latest wins" in `record_spec`.

**F2 — Provenance model:** `specs_structured[key]` gains `tier`; `MaterialSpecFacet` gains `source/confidence/tier`; `MaterialCard` gains `category_source/category_confidence/category_tier`. All category writes route through one `set_category(card, value, source, confidence)` helper (lives in `spec_tiers.py`) applying the F1 ladder.

**F3 — Self-heal/re-trigger:** `ENRICHMENT_PIPELINE_VERSION` constant + `MaterialCard.enrichment_attempt_version` stamped on every status write; `select_batch` adds `OR enrichment_attempt_version < ENRICHMENT_PIPELINE_VERSION` (re-run each terminal card once after a bump). `MaterialCard.needs_reeval` bool set on new sighting/offer + manual button. Conditional `specs_enriched_at` stamp (only when a facet/summary written) + `spec_attempts` bound (`SPEC_MAX_ATTEMPTS=3`). `AI_INFERRED` becomes re-selectable on a long backoff.

**F4 — Migration discipline:** every schema change is an Alembic migration with upgrade+downgrade, revision id ≤32 chars, single head verified; data backfills via `op.get_bind()`+`text()` or a one-off script with a fresh `pg_dump` first; no DDL in `startup.py`.

## 4. Cross-cutting reconciliation (AUTHORITATIVE — overrides any per-section detail below)

The five sections were authored in parallel against F1–F4; where a section's build-order, migration revision-id, or column ownership conflicts with this section, **this section governs**.

**4.1 Revised build order** (the design surfaced that the provenance foundation must precede categorization):

1. **SP1 — Stop the bleeding** (independent, urgent): retire the Haiku path, gate the spec reader to `status ∈ {verified, web_sourced, oem_sourced}`, one-time cleanup of polluted cards. *Scope change vs the section below: SP1 does NOT add `spec_attempts` or the conditional-stamp/bounded-retry — those move to SP4 (their natural owner). SP1 keeps only the status-gate on the spec reader's INPUT + the data cleanup.*
2. **SP2 — Foundation: provenance & tier ladder** (the doc's "SP3:provenance-ladder" section): `spec_tiers.py` (`resolve`, `tier_for`, `set_category`), `record_spec` ladder rewrite, the F2 provenance columns (facet + category), route the decode writer through `set_category`. **Nothing else can correctly write provenance until this lands.**
3. **SP3 — Canonical categorization** (the doc's "SP2:categorization" section): `@validates("category")`, route the 4 remaining category writers through `set_category`, alias backfill + quarantine of the 32 dirty rows. *Depends on SP2's `set_category`.*
4. **SP4 — Self-healing state machine** (the doc's "SP4:state-machine" section): the version/`needs_reeval`/`spec_attempts` columns + constants, `select_batch` re-eligibility, conditional spec stamp + bounded retry (**owns the spec-freeze fix**), manual-button repoint, search-path `needs_reeval`.
5. **SP5 — OEM/FRU acquisition** (the doc's "SP5:oem-acquisition" section): BrokerBin tier, flagged scrapers, secondary connector fixes, version bump. *Depends on SP2 + SP4.*

**4.2 Migration chain** (linear; each `down_revision` = the prior SP's head; all ids ≤32 chars; current head `090_add_condition_mc`):

| SP | revision id | down_revision | adds |
|---|---|---|---|
| SP1 | `091_cleanup_vague_descs` | `090_add_condition_mc` | DATA: NULL vague descs + reset `specs_enriched_at` (+ backup table) |
| SP2 | `092_spec_provenance` | `091_cleanup_vague_descs` | facet `source/confidence/tier`; card `category_source/confidence/tier`; backfill |
| SP3 | `093_category_backfill` | `092_spec_provenance` | DATA: remap/quarantine the 32 dirty category rows |
| SP4 | `094_selfheal_state` | `093_category_backfill` | card `enrichment_attempt_version/needs_reeval/spec_attempts` + index |
| SP5 | `095_sourcing_leads` | `094_selfheal_state` | card `sourcing_leads` JSONB |

(SP1's section below proposes a `spec_attempts` column at `091`/`092` — **disregard**; `spec_attempts` is added once, by SP4 at `094`. SP1's section's `091_spec_attempts` schema migration is dropped; SP1 is data-only.)

**4.3 `set_category` / `spec_tiers.py` ownership:** defined once by **SP2** in `app/services/spec_tiers.py` (with `resolve`/`tier_for`). SP3, SP4-adjacent category writers, and SP5 import it. SP2's section is the source of truth for the helper's behavior.

**4.4 Spec-freeze fix ownership:** the conditional `specs_enriched_at` stamp, `spec_attempts`, and `SPEC_MAX_ATTEMPTS` are owned by **SP4** (its section change 1 + 6). SP1 only adds the trustworthy-status INPUT gate (its change 2's status filter) — not the stamp logic.

## 5. Sequencing & delivery

Each sub-project is its own branch + PR with the full pipeline (brainstorm-confirmed → TDD → build → review agents → simplify → /qa → deploy), shipped in the 4.1 order. Per-PR: update the relevant `docs/APP_MAP_*.md`; run `pre-commit run --all-files`; verify the migration on live PG (SQLite masks PG JSONB/ILIKE quirks); `alembic heads` single. SP4's `ENRICHMENT_PIPELINE_VERSION` bump is deliberately **held until SP5 ships a new resolution source**, so the one-time backlog re-run actually finds new data instead of re-confirming misses.

## 6. Open decision (must be resolved before SP5 build)

**SP5 OEM scrapers:** BrokerBin-only vs BrokerBin + PartSurfer/PSREF scrapers. Recommendation (in the SP5 section): **ship BrokerBin-only**, write the scraper connectors behind `OEM_SCRAPE_ENABLED=false`, and treat enabling them in prod as a separate go/no-go (ToS-gray, layout-fragile). Decision owner: user.

---

# Detailed sub-project specs

> The sections below are the per-sub-project specs as authored. **Read §4 (reconciliation) as authoritative** wherever a section's build-order/migration-id/column-ownership differs. Section headers retain their authoring labels (e.g. "SP3:provenance-ladder" is build-order SP2 per §4.1).

========== SP1:stop-bleeding ==========
I have everything grounded. The Haiku path writes `enrichment_source` to either `"claude_haiku"` or `"batch_api"` — but per the audit, the cleanup target is the 298 cards that are `not_found` with `enrichment_source IS NULL` (because the authoritative worker later nulled the source on the miss while leaving the fabricated description). That nuance is important and I'll spec it exactly.

Here is the SP1 design spec.

## SP1 — STOP THE BLEEDING (retire ungated Haiku, gate spec-extraction, clean up polluted cards)

**Goal & root cause addressed.**
Two enrichment systems write `MaterialCard.description`. The legacy **Claude Haiku** path (`material_enrichment_service._apply_enrichment_result`, `material_enrichment_service.py:80-85`) writes a fabricated description **unconditionally**, with no confidence, no provenance, and crucially **never sets `enrichment_status`**. The card then flows to the authoritative worker, lands `not_found`, and the worker nulls `enrichment_source`/`enrichment_provenance` (`authoritative_enrichment_service.py:399-405`) **but leaves the fabricated `description` in place** — producing a card that reads as a clean miss yet still shows a guess (the 298-card population). That guess then satisfies the spec-extraction eligibility filter (`spec_enrichment_service.py:119-128`, `:205-216`) and is fed verbatim into the spec prompt at `spec_enrichment_service.py:59` (`Desc: {c['description'][:200]}`), seeding hallucinated structured specs/facets (the 310-card population). SP1 stops the bleeding: kill the ungated writer, gate the spec reader so only trustworthy descriptions seed specs, and one-time-clean the already-polluted cards so they re-enter cleanly.

This SP does **not** redesign the manual button's destination — SP4 repoints the button to the authoritative ladder. SP1 only severs the button's call to the Haiku path (see Change 4).

---

**Changes** (numbered; each: file:line → now → should).

1. **Remove the scheduled Haiku enrichment job — flag-default-off is not enough, because prod env forces it on.**
   - `app/jobs/tagging_jobs.py:49-55` registers `_job_material_enrichment` under `if settings.material_enrichment_enabled:`. `material_enrichment_enabled` defaults `False` (`config.py:128`) **but is `MATERIAL_ENRICHMENT_ENABLED=true` in the live container** — so relying on the default does nothing.
   - **Now:** every 2h the job runs `enrich_pending_cards` (Haiku card descriptions) then `enrich_pending_specs`.
   - **Should:** **delete** the registration block (`tagging_jobs.py:49-55`) and **delete** `_job_material_enrichment` (`:205-238`). The spec pass it chained (`enrich_pending_specs`) is **not** Haiku — it is now driven by the enrichment worker (per the audit's worker design); SP1 must **not** orphan it. Re-home the `enrich_pending_specs` call: SP1 leaves spec-pass scheduling to the existing enrichment worker, which already calls the spec pass per-batch — confirm via the worker before deleting; if the worker does not yet schedule a standalone sweep, keep a thin `_job_spec_enrichment` that calls only `enrich_pending_specs` (no `enrich_pending_cards`). Decision: **removal, not flag** — a config flag that is `true` in prod is a live foot-gun; the writer has no confidence gate and is functionally superseded by the authoritative worker (root-cause fix, not a band-aid).
   - Remove the now-dead `settings.material_enrichment_enabled` and `settings.material_enrichment_batch_size` from `config.py:128-129` **only if** no other caller remains (grep first; `material_enrichment_batch_size` is referenced by the deleted job — if the re-homed spec job needs a batch size, rename to `spec_enrichment_batch_size` rather than reuse the Haiku-named one). Drop `MATERIAL_ENRICHMENT_ENABLED` from `.env.example`.

2. **Gate the spec-extraction reader so guesses/orphans never seed specs (F3 SPEC freeze pairing).**
   - `spec_enrichment_service.py:59` reads `c['description'][:200]`; eligibility at `:119-128` (`enrich_card_specs` query) and `:205-216` (`enrich_pending_specs` query) currently require only `category IS NOT NULL` + non-empty `description`.
   - **Should:** add to **both** queries `MaterialCard.enrichment_status.in_((VERIFIED, WEB_SOURCED, OEM_SOURCED))` using `MaterialEnrichmentStatus` constants from `app/constants.py` (never raw strings). This excludes `ai_inferred`, `not_found`, `not_catalogued`, and `unenriched` from ever seeding specs — severing the guess→spec-hallucination propagation. `manual` (SP4) is not an `enrichment_status` value; manual edits set a trustworthy status, so no change needed here.
   - **F3 freeze fix (paired, in-scope for SP1 because it is the same reader):** at `spec_enrichment_service.py:190`, `c.specs_enriched_at = now` is stamped for **every** processed card even when `wrote_any` is `False`, freezing zero-facet cards forever under the `force=False` filter (`:127`). Change: stamp `specs_enriched_at` **only when `wrote_any` or a summary was written**; otherwise increment `c.spec_attempts` (new column, see Data model) and stamp only once `spec_attempts >= SPEC_MAX_ATTEMPTS` (=3). This bounds retries on genuinely spec-less parts while letting a card whose description later improves get a real pass. (References F3; the `enrichment_attempt_version` / `needs_reeval` machinery is owned by SP3 — SP1 only adds `spec_attempts` and the conditional stamp.)

3. **One-time data-cleanup migration (F4 discipline).** Two backfills in one Alembic migration (raw SQL via `op.get_bind()` + `text()`), with explicit backup and full rollback.
   - **3a. NULL the fabricated descriptions** on the 298-card set:
     ```sql
     UPDATE material_cards
        SET description = NULL
      WHERE deleted_at IS NULL
        AND enrichment_status = 'not_found'
        AND enrichment_source IS NULL
        AND description IS NOT NULL
        AND (description ILIKE '%likely%' OR description ILIKE '%possibly%'
             OR description ILIKE '%may be%' OR description ILIKE '%proprietary%'
             OR description ILIKE '%appears to be%' OR description ILIKE '%could be%');
     ```
     Predicate rationale: `enrichment_status='not_found'` + `enrichment_source IS NULL` is exactly the "clean miss that still carries a guess" signature the authoritative worker leaves behind; the vague tokens are the Haiku hedging vocabulary from the audit sample. Capture affected `id`s into a backup table (see migration outline) before the UPDATE.
   - **3b. Reset `specs_enriched_at`** on the cards whose specs were seeded from a now-untrustworthy description (the ~310-card set), so they re-enter the spec pass once a trustworthy description exists:
     ```sql
     UPDATE material_cards
        SET specs_enriched_at = NULL
      WHERE deleted_at IS NULL
        AND specs_enriched_at IS NOT NULL
        AND enrichment_status NOT IN ('verified','web_sourced','oem_sourced');
     ```
     This aligns the existing data with the new gate (Change 2): any card whose specs were stamped while NOT in a trustworthy status is reset to retryable. Cards in a trustworthy status keep their specs. (Do **not** delete `MaterialSpecFacet` rows here — those are governed by SP2's facet-provenance rework; SP1 only clears the stamp so the gated reader re-evaluates. Note this ordering dependency in Interfaces.)

4. **Sever the manual button from the Haiku path (SP4 boundary).**
   - `app/routers/htmx_views.py:8747` imports `enrich_material_cards`; `:8754` awaits it; `:8758-8761` then force-runs `enrich_card_specs([material_id], db, force=True)`.
   - **Should (SP1 scope only):** remove the `enrich_material_cards` import and call (`:8747`, `:8754`) so the button no longer invokes the ungated Haiku path. SP1 leaves a clear seam for SP4 to insert the authoritative-ladder call (`enrich_cards([material_id], db, refresh=True)`). **Interim behavior between SP1 merge and SP4 merge:** the button must not become a no-op (CLAUDE.md: no half-measures). Two coordinated options — **recommended: land SP1 and SP4's button-repoint in the same PR** so the button is repointed atomically. If they must ship separately, SP1's interim is to call the authoritative ladder directly (the import already exists in that router module per the audit's §3.4) — never leave the button calling Haiku and never leave it inert. The forced spec pass at `:8758-8761` stays, but `force=True` now runs through the gated reader (Change 2) only if the card is in a trustworthy status; otherwise it is a no-op, which is correct (don't seed specs from a guess even on manual force). Adjust `enrich_card_specs` so `force=True` still honors the new status gate (the status gate is a correctness invariant, not a freeze-avoidance gate — `force` only bypasses the `specs_enriched_at IS NULL` filter, not the trustworthy-status filter).

---

**Data model / migration.**

- **New column:** `MaterialCard.spec_attempts INTEGER NOT NULL DEFAULT 0` (`app/models/intelligence.py`, near `specs_enriched_at:54`). Bounds spec retries (Change 2). No index (only read in the per-card loop, never filtered alone).
- **Migration A — schema** (`alembic/versions/091_add_spec_attempts.py`, revision id `091_spec_attempts` ≤32 chars, `down_revision="090_add_condition_mc"` which is the verified single current head):
  - **upgrade:** `op.add_column("material_cards", sa.Column("spec_attempts", sa.Integer(), nullable=False, server_default="0"))`.
  - **downgrade:** `op.drop_column("material_cards", "spec_attempts")`.
  - Run `alembic upgrade head → downgrade -1 → upgrade head`; then `alembic heads` to confirm a single head.
- **Migration B — data cleanup** (`alembic/versions/092_cleanup_vague_descs.py`, revision id `092_cleanup_vague_descs` =22 chars ≤32, `down_revision="091_spec_attempts"`):
  - **Backup (in-migration, reversible):** before any UPDATE, `CREATE TABLE _sp1_desc_backup AS SELECT id, description, specs_enriched_at FROM material_cards WHERE <3a predicate OR 3b predicate>` so rollback is exact. (This is permitted: a DDL-in-migration backup table is not raw prod DDL outside Alembic.)
  - **upgrade:** create backup table → run 3a UPDATE → run 3b UPDATE (via `op.get_bind().execute(text(...))`).
  - **downgrade:** `UPDATE material_cards m SET description = b.description, specs_enriched_at = b.specs_enriched_at FROM _sp1_desc_backup b WHERE m.id = b.id;` then `DROP TABLE _sp1_desc_backup;`.
  - **Operational backup:** the `db-backup` `pg_dump` runs every 6h; confirm a fresh dump exists before applying B on the live DB (per CLAUDE.md Safety). Counts are expectations (~298 / ~310), not hard assertions — the migration logs `result.rowcount` via Loguru for both UPDATEs but does not fail on count drift.

---

**Interfaces & dependencies.**

- **Consumes from foundation:** `MaterialEnrichmentStatus` constants (`app/constants.py:452-465`) for the Change-2 status gate. `SPEC_MAX_ATTEMPTS=3` is a module constant in `spec_enrichment_service.py`. SP1 does **not** consume F1/F2 (tier ladder, facet provenance) — those are SP2/SP4.
- **Exposes:** the gated `enrich_card_specs` / `enrich_pending_specs` queries (trustworthy-status filter) that all later SPs inherit; the `spec_attempts` column (SP3 may later fold this into its `spec_pipeline_version` re-trigger but does not need to). The severed button seam in `htmx_views.py` that SP4 fills.
- **Ordering dependencies:** (i) **SP4 button-repoint must land with or before SP1's button-severance** (Change 4) — do not ship an inert/Haiku button. (ii) Migration B (3b) clears `specs_enriched_at` but leaves `MaterialSpecFacet` rows; **SP2's facet-provenance migration is responsible for re-ranking/purging stale facets** — SP1 and SP2 migrations are independent (no shared table column) and can land in either order, but SP2 should land before any large spec re-run so re-extracted facets carry provenance. Document this in the SP2 spec's "consumes" section.

---

**Tests** (TDD — write first; `TESTING=1 PYTHONPATH=/root/availai pytest`, in-memory SQLite via `conftest.py`).

- **Unit — job removal:** assert `_job_material_enrichment` no longer exists in `app.jobs.tagging_jobs` (e.g. `not hasattr`); assert `register_tagging_jobs` adds no job with `id="material_enrichment"` (mock scheduler, inspect `add_job` calls) even when `settings.material_enrichment_enabled = True` is monkeypatched (proves removal, not flag-gating).
- **Unit — spec gate (Change 2):** for each status, build a card with `category` + non-empty `description`; assert `enrich_card_specs` / `enrich_pending_specs` select it **iff** status ∈ {verified, web_sourced, oem_sourced}. Parametrize all 7 `MaterialEnrichmentStatus` values.
- **Regression — guess→spec leak (the specific bug):** card with `enrichment_status='not_found'`, `enrichment_source=None`, vague `description`, `category='hdd'`; run `enrich_pending_specs` with `claude_structured` mocked to return specs; assert **zero** `MaterialSpecFacet` rows and `record_spec` **not** called (the description never reached the prompt).
- **Regression — spec freeze fix (F3, Change 2):** card in `verified` status with description; mock `claude_structured` to return all-null/low-conf specs (`wrote_any=False`); assert `specs_enriched_at` stays `NULL` and `spec_attempts` increments to 1; repeat to attempt 3; on the 3rd, assert `specs_enriched_at` is stamped and the card is no longer re-selected. Then a positive case: `wrote_any=True` stamps `specs_enriched_at` immediately and does not bump beyond.
- **Migration test (B) — integration:** seed cards matching 3a/3b predicates plus control cards in trustworthy status that must be untouched; run upgrade; assert vague `description` → NULL, `specs_enriched_at` reset on non-trustworthy, trustworthy cards unchanged; run downgrade; assert exact restoration from `_sp1_desc_backup`; assert table dropped. (Run with `--override-ini="addopts="` if xdist interferes with the shared engine.) Note: ILIKE behaves under SQLite but verify the `not_found`+source-NULL predicate against live PG before applying (memory: SQLite masks PG quirks).
- **Migration test (A):** `spec_attempts` column exists, NOT NULL, default 0; upgrade/downgrade clean.
- **Router test (Change 4):** POST `/v2/partials/materials/{id}/enrich` no longer calls `enrich_material_cards` (assert via mock/import-absence); button still returns the detail partial (200, not a 500/no-op).

---

**Done criteria (measurable).**

1. `grep -rn "_job_material_enrichment\|enrich_material_cards" app/jobs app/routers` returns **zero** matches (writer fully retired from job + button).
2. After Migration B on the live DB: `SELECT count(*) FROM material_cards WHERE enrichment_status='not_found' AND enrichment_source IS NULL AND (description ILIKE '%likely%' OR …)` = **0**.
3. After Migration B: every card with `specs_enriched_at IS NOT NULL` is in {verified, web_sourced, oem_sourced} (`SELECT count(*) … WHERE specs_enriched_at IS NOT NULL AND enrichment_status NOT IN (…)` = **0**).
4. No new `MaterialSpecFacet` rows are created for any card not in a trustworthy status (verified by the regression test + a post-deploy spot query 24h after the worker runs).
5. `alembic heads` returns a single head; upgrade→downgrade→upgrade clean on both migrations.
6. Full suite green; `pre-commit run --all-files` clean.

---

**Risks & mitigations.**

- **R1 — orphaning the spec pass when deleting the Haiku job.** Mitigation: confirm the enrichment worker already schedules/calls `enrich_pending_specs`; if not, keep a thin spec-only job (Change 1). Verified-against-code before deletion, not assumed.
- **R2 — interim dead/Haiku button between SP1 and SP4.** Mitigation: land the button-repoint atomically with SP1, or have SP1 interim-call the authoritative ladder. Never inert, never Haiku.
- **R3 — cleanup over/under-matches** (vague-token list misses some, or NULLs a legitimate description). Mitigation: the predicate is tightly scoped to `not_found` + `enrichment_source IS NULL` (a real description always carries a non-NULL source); the `_sp1_desc_backup` table makes the change fully reversible; log rowcounts and eyeball against the audit's ~298 before committing the data migration to prod.
- **R4 — 3b resets specs on cards whose facets stay** (transient inconsistency: stamp cleared but stale facets present until SP2 / next gated run). Mitigation: documented ordering dependency on SP2; gated reader (Change 2) won't re-seed until a trustworthy description exists, so no new pollution; stale facets are addressed by SP2's provenance migration.
- **R5 — SQLite tests pass but PG predicate behaves differently** (`ILIKE`, NULL semantics). Mitigation: explicit "verify against live PG" gate in the migration test notes (memory: feedback_sqlite_masks_postgres).
- **R6 — config-flag removal breaks other readers.** Mitigation: grep `material_enrichment_enabled` / `material_enrichment_batch_size` across `app/` before removing; rename rather than reuse if the spec job needs a batch size.

Grounded file:lines (current, verified): `app/jobs/tagging_jobs.py:49-55,205-238`; `app/routers/htmx_views.py:8747,8754,8758-8761`; `app/services/spec_enrichment_service.py:59,119-128,190,205-216`; `app/services/material_enrichment_service.py:80-85`; `app/config.py:128-129`; `app/models/intelligence.py:54,58,81-87`; `app/constants.py:452-465`. Current single alembic head: `090_add_condition_mc` (`alembic/versions/090_add_condition_to_material_cards.py`).

========== SP2:categorization ==========
I now have all the exact code references I need. Here is the SP2 design spec.

## SP2 — CANONICAL CATEGORIZATION (never store a junk category)

**Goal & root cause addressed.**
A `MaterialCard.category` should only ever hold one of the 48 canonical commodity keys, or `NULL` (uncategorized / pending re-decode). Today three write paths use a `normalize_category(raw) or raw` idiom that persists the raw connector/AI free-text verbatim whenever normalization returns `None`, so the DB now holds 32 distinct dirty strings ("Integrated Circuits (ICs)", "Schottky Diodes & Rectifiers", "Intel", "Microcontroller", …). Root causes, from audit section B:
- `app/services/enrichment.py:181` — `card.category = normalize_category(raw_cat) or raw_cat` — **no guard, LEAK.**
- `app/services/authoritative_enrichment_service.py:380` — `card.category = normalize_category(inf.category) or inf.category` — **no guard, LEAK.**
- `app/services/material_enrichment_service.py:74` — `cat = normalize_category(cat) or cat` then re-bucketed to `"other"` at `:75-76` — **does not leak junk, but mis-files unmapped values as `"other"`**, conflating "AI's deliberate no-fit" with "couldn't parse the string."

A junk category is load-bearing damage: `spec_write_service.record_spec` rejects every spec when category is empty/unschema'd, and `mpn_decoder/writer.py:36-40` refuses to decode when the decoded commodity conflicts with the stored category. A wrong category therefore permanently blocks all real spec data. The fix is a **single chokepoint** (`@validates("category")`) that enforces canonical-or-NULL on every write path present and future, plus removing the leaking fallbacks, plus a backfill of the 32 dirty strings.

**The 48 canonical keys** (from `commodity_registry.COMMODITY_TREE`, flattened by `get_all_commodities()`):
`capacitors, resistors, inductors, transformers, fuses, oscillators, filters, diodes, transistors, mosfets, thyristors, analog_ic, logic_ic, power_ic, dram, flash, microcontrollers, cpu, microprocessors, dsp, fpga, asic, gpu, ssd, hdd, power_supplies, voltage_regulators, batteries, connectors, cables, sockets, relays, switches, motors, leds, displays, optoelectronics, sensors, rf, motherboards, network_cards, raid_controllers, server_chassis, fans_cooling, networking, enclosures, tools_accessories, other`.

(Note: the audit narrative lists 47 because it elided `power_ic`; the registry's three IC children are `analog_ic, logic_ic, power_ic` — 48 total. `other` is reserved for the AI's deliberate "no category fits" signal, never the junk sink.)

---

**Changes** — numbered.

1. **`app/models/intelligence.py` (add after `_validate_enrichment_status`, currently ends :87).** No `@validates("category")` exists today; `category` is a bare `String(255)` (:40). Add the chokepoint validator mirroring the established `_validate_search_count`/`_validate_enrichment_status` pattern:
   - `@validates("category")` runs the incoming value through `category_normalizer.normalize_category`.
   - Canonical key (alias-resolved or already-canonical) → store it.
   - Unmapped / empty / `None` → store `None` (NOT `"other"`).
   - Import `normalize_category` lazily inside the method (same lazy-import pattern as `_validate_enrichment_status` importing `MaterialEnrichmentStatus`) to avoid a model→service import cycle.
   - This is the single chokepoint that closes all current AND future write paths.

2. **`set_category` helper composition with F2.** F2's `set_category(card, value, source, confidence)` applies the provenance + tier ladder (F1) and writes `category_source/category_confidence/category_tier`; the `@validates` hook enforces canonical-or-NULL on the column. They compose as a **two-stage pipeline, no duplicated normalization decision**:
   - `set_category` first calls `normalize_category(value)` itself to obtain the canonical key (or `None`) — it needs the *normalized* value to evaluate the ladder (a lower-tier source must not overwrite a higher-tier category, per F1/F3) and to decide whether to advance `category_tier`.
   - It then assigns `card.category = <normalized-or-None>` through the validated column. The `@validates` hook re-normalizes idempotently (normalizing an already-canonical key returns itself; normalizing `None` returns `None`), so the column write is a guaranteed no-op transform and stays the last line of defense for any caller that bypasses `set_category`.
   - **Tie-break rule for NULL under the ladder:** `set_category` only writes (and only advances `category_tier`/`category_source`/`category_confidence`) when F1 `resolve()` says incoming wins. If incoming normalizes to `None` (unmapped) it is treated as tier-absent and **never overwrites an existing canonical category** — i.e. a junk AI guess can't blank out a real decode. It only writes `None` when there is no existing category (or the existing category is itself `None`).

3. **`app/services/enrichment.py:181` — remove the `or raw_cat` leak.** Now: `card.category = normalize_category(raw_cat) or raw_cat`. Should: route through F2 `set_category(card, raw_cat, source="<connector source_name>", confidence=confidence)` (the `confidence`/`source_name` already in scope at :171-172). The `if enrichment.get("category") and not card.category:` guard at :177 is retained as a cheap fast-path but is now redundant against the ladder. Drop the inline `or raw_cat`.

4. **`app/services/authoritative_enrichment_service.py:380` — remove the `or inf.category` leak.** Now: `card.category = normalize_category(inf.category) or inf.category`. Should: `set_category(card, inf.category, source="claude_opus_inferred", confidence=inf.confidence)` (tier 40, `ai_guess`/`claude_opus_inferred` per F1). A sub-canonical AI guess that doesn't map now stores `None` rather than "Intel"/"Laptop Battery", leaving the card uncategorized → re-decodable rather than poisoned.

5. **`app/services/material_enrichment_service.py:74-76` — drop `or cat` and the `"other"` re-bucket.** Now: `cat = normalize_category(cat) or cat; if cat not in VALID_CATEGORIES: cat = "other"`. Should: route the assignment at :82 through `set_category(card, ai.get("category"), source="claude_haiku", confidence=<from schema or default low>)` (tier 40). Drop both the `or cat` and the `cat = "other"` fallback so an unmapped Haiku value becomes `NULL`-pending, not silently mis-filed as `other`. The `_PART_SCHEMA` enum constraint (this module's :49) stays as a first line of defense. (Audit section E recommends retiring this Haiku path entirely; SP2 only makes its category write safe — retirement is SP5/state-machine scope.)

6. **`app/services/mpn_decoder/writer.py:55` — route through `set_category`.** Now: bare `card.category = result.commodity` (decoder commodity is regex-gated and already canonical, so it is safe, but it bypasses provenance). Should: `set_category(card, result.commodity, source="mpn_decode", confidence=result.confidence)` (tier 85 per F1). Preserve the existing "only when `not card_cat`" gate at :50 and the conflict-skip at :36-40 — those are correct domain logic; `set_category`'s ladder additionally lets a future higher-tier source upgrade the decode-set category.

---

**Data model / migration.**

SP2 owns only the **category-provenance columns** (F2) and the **category backfill**. The shared `enrichment_attempt_version` / `needs_reeval` / `spec_attempts` columns and the `MaterialSpecFacet` provenance columns belong to the SP that owns the state machine and the facet write path; SP2 *consumes* the F1/F2 helpers but does not migrate those tables.

Columns added to `material_cards` (F2):
- `category_source` `String(50)` nullable — e.g. `mpn_decode`, `digikey_api`, `claude_opus_inferred`.
- `category_confidence` `Float` nullable.
- `category_tier` `Integer` nullable, indexed (`ix_material_cards_category_tier`) — lets a future re-rank sweep query "category came from a low tier" cheaply, mirroring the facet-provenance rationale in audit D.

Migration (Alembic, F4 — revision id `091_category_provenance`, ≤32 chars; verify single head with `alembic heads`, merge if needed):
- **upgrade():** `op.add_column` ×3 on `material_cards`; `op.create_index("ix_material_cards_category_tier", "material_cards", ["category_tier"])`.
- **downgrade():** `op.drop_index("ix_material_cards_category_tier", "material_cards")`; `op.drop_column` ×3 (reverse order).
- No DDL in `startup.py` (F4).

Backfill of the 32 dirty strings — **split into two coordinated changes** (F4: data backfill via `op.get_bind()` + `text()` inside a data migration with explicit rollback; a fresh `pg_dump` from the `db-backup` service must exist first):

(a) **Deterministic remaps — extend `CATEGORY_ALIASES` in `app/services/category_normalizer.py`** (these are permanent, not one-off, so they belong in the alias map, and the `@validates` hook will then auto-canonicalize them on any future write):
```
"microcontroller": "microcontrollers", "8-bit microcontrollers - mcu": "microcontrollers",
"diode": "diodes", "schottky diodes & rectifiers": "diodes",
"mosfet": "mosfets",
"voltage regulator": "voltage_regulators",
"interface ic": "analog_ic", "rs-232 interface ic": "analog_ic",
"integrated circuit (timer)": "analog_ic", "timers & support products": "analog_ic",
"analog to digital converters - adc": "analog_ic", "data converter (adc)": "analog_ic",
"logic ic": "logic_ic",
"circuit protection": "fuses",
"terminals": "connectors",
"isolators": "optoelectronics",
"multiprotocol modules": "rf", "rf/wireless module": "rf",
"battery products": "batteries", "laptop battery": "batteries",
"laptop battery (fru / cru replacement part)": "batteries",
"raid controller accessory / battery backup (bbwc battery module)": "raid_controllers",
"raid controller accessory / battery module": "raid_controllers",
"storage controller accessory / raid cache backup power (fbwc capacitor pack)": "raid_controllers",
"storage controller battery": "raid_controllers",
"server maintenance consumable / thermal management accessory": "fans_cooling",
"industrial automation and controls": "other",
"development boards, kits, programmers": "other",
```
("battery products" already exists in the alias map at :25 — leave it.)

(b) **Migration data step** updates existing rows in two passes via `text()`:
- Pass 1 (deterministic): for each `(dirty_string → canonical)` pair above, `UPDATE material_cards SET category = :canon, category_source='backfill', category_tier=85, category_confidence=1.0 WHERE lower(trim(category)) = :dirty AND deleted_at IS NULL`. (tier 85 = `mpn_decode`-equivalent confidence for a curated human-verified remap, so a later AI guess can't undo it.)
- Pass 2 (quarantine): `UPDATE material_cards SET category = NULL, category_source=NULL, category_tier=NULL, category_confidence=NULL WHERE lower(trim(category)) IN (...the genuinely ambiguous/manufacturer strings...) AND deleted_at IS NULL` — namely `'integrated circuits (ics)'`, `'discrete semiconductor products'`, `'intel'`, `'infinite electronics'`. NULLing returns these to the enrichment queue for MPN re-decode (paired with F3's `needs_reeval`/version re-eligibility owned by the state-machine SP — SP2 sets the value to `NULL`, which already makes them re-selectable as effectively-unenriched once that SP's `OR` branches land).
- **downgrade():** non-destructive no-op documented as such (the original dirty strings are not restorable and were junk by definition; the column-drop in the schema migration removes the provenance, and re-running enrichment re-populates `category`). Rollback of the *schema* is the `drop_column` above; rollback of the *data* is "let enrichment re-run." State this explicitly in the migration docstring (F4 requires rollback to be addressed, even when it is a documented no-op).

---

**Interfaces & dependencies.**

Consumes from the shared foundation:
- F1 `resolve(existing, incoming)` and the `SOURCE_TIER` map from `app/services/spec_tiers.py` (`mpn_decode`=85, vendor APIs=90, `claude_opus_inferred`/`ai_guess`=40, `manual`=100).
- F2 `set_category(card, value, source, confidence)` helper — SP2 is the **primary caller** that replaces all five scattered `card.category =` writes. If the foundation SP defines `set_category`, SP2 wires the call sites; if SP2 lands first, SP2 defines `set_category` in `app/services/spec_tiers.py` (the F1 module) and the foundation consumes it. Either way `set_category` lives next to `resolve` so the ladder logic is co-located.

Exposes:
- The `@validates("category")` guarantee: `MaterialCard.category ∈ {48 canonical keys} ∪ {None}`. Every other SP (faceted sidebar, spec schema selection, decode conflict-check) can rely on this invariant and stop defensively re-normalizing.
- Extended `CATEGORY_ALIASES` (importable by the existing `scripts/normalize_categories.py` one-off, per `category_normalizer.py:8`).

Depends on F4 migration discipline and on a fresh DB backup before the data step.

---

**Tests** (always include, per CLAUDE.md — `TESTING=1`, in-memory SQLite; SQLite tolerates the validator since it's pure Python, but verify the migration's `text()` UPDATEs against live PG per the `feedback_sqlite_masks_postgres` rule).

Unit — `tests/test_category_validator.py`:
- canonical key passes through unchanged (`"hdd"` → `"hdd"`).
- known alias canonicalizes (`"Microcontroller"` → `"microcontrollers"`; `"Schottky Diodes & Rectifiers"` → `"diodes"`; case/whitespace-insensitive).
- **regression (the leak bug):** unmapped junk stores `None`, NOT the raw string (`"Integrated Circuits (ICs)"` → `None`; `"Intel"` → `None`).
- **regression (no `"other"` sink):** unmapped value never becomes `"other"` (`"infinite Electronics"` → `None`, asserting `!= "other"`).
- empty/whitespace/`None` → `None`.
- `"other"` itself (deliberate AI no-fit) is preserved as `"other"` (it's a canonical key).

Unit — `tests/test_set_category.py`:
- higher tier overrides lower (existing `claude_opus_inferred`/40 `"cpu"` → incoming `mpn_decode`/85 `"microprocessors"` wins).
- lower tier cannot overwrite higher (existing `mpn_decode`/85 → incoming tier-40 guess loses; category unchanged).
- **regression (junk can't blank a real category):** existing canonical `"dram"` + incoming unmapped junk → category stays `"dram"`, provenance untouched.
- `set_category` → validated column → canonical-or-NULL composes (passing `"Microcontroller"` at tier 85 yields stored `"microcontrollers"` with `category_tier=85`).
- equal tier → higher confidence wins, exact tie → newer `updated_at` (F1).

Integration — `tests/test_category_leak_paths.py` (one per fixed call site, asserting no junk persists end-to-end):
- `enrichment.py` apply with a connector category of `"Discrete Semiconductor Products"` → `card.category is None` (was: stored verbatim).
- `authoritative_enrichment_service.enrich_card` `ai_inferred` branch with `inf.category="Laptop Battery"` → `"batteries"` (alias) and with `inf.category="Intel"` → `None`.
- `material_enrichment_service._apply_enrichment_result` with an off-vocab AI category → `None`, asserting it is NOT `"other"`.
- `mpn_decoder/writer` decode of an un-categorized card sets canonical category with `category_tier=85` and provenance recorded.

Migration — `tests/test_migration_091_category_provenance.py`:
- upgrade adds the 3 columns + index; downgrade removes them; upgrade→downgrade→upgrade round-trips.
- backfill data step: seed rows with each of the 32 dirty strings → after upgrade, deterministic ones hold the expected canonical key with `category_source='backfill'`, ambiguous ones hold `NULL`.
- revision id length ≤32 (the `feedback_alembic_revision_id_length` guard).

---

**Done criteria** (measurable).
- `SELECT count(*) FROM material_cards WHERE category IS NOT NULL AND lower(trim(category)) NOT IN (<48 canonical keys>)` returns **0** on live PG after migration (currently 32 distinct dirty strings across ~50+ rows per audit B).
- The 4 ambiguous/manufacturer strings ("Integrated Circuits (ICs)", "Discrete Semiconductor Products", "Intel", "infinite Electronics") are now `category IS NULL` and re-selectable by the worker (verified by the state-machine SP's re-eligibility once landed).
- No code path can assign a non-canonical non-NULL category: grep shows zero remaining `normalize_category(...) or <raw>` idioms in `app/services/`; all five former `card.category =` writes route through `set_category`.
- Full suite green (`TESTING=1 … pytest tests/ -q`); `pre-commit run --all-files` clean; relevant `docs/APP_MAP_DATABASE.md` updated with the 3 new columns (per `feedback_update_app_map`).

---

**Risks & mitigations.**
- **R1 — `@validates` silently NULLs a legitimately-intended-but-unmapped category, masking a missing alias.** Mitigation: the validator logs at `WARNING` (Loguru) with the rejected raw value when it returns `None` for a non-empty input, so missing aliases surface in logs instead of vanishing; periodic log review feeds new `CATEGORY_ALIASES` entries.
- **R2 — Model↔service import cycle from importing `normalize_category` at module top.** Mitigation: lazy import inside the validator method (same pattern as the existing `_validate_enrichment_status`).
- **R3 — Backfill NULLs a row that had a usable (if dirty) category, temporarily losing a filter facet until re-decode.** Mitigation: only the 4 genuinely ambiguous/manufacturer strings are NULLed; all deterministically-mappable strings are preserved as canonical keys. NULL is the correct "pending re-decode" state and the sidebar already buckets NULL into its bottom group. Fresh `pg_dump` taken before the data step; `scripts/restore.sh` is the recovery path.
- **R4 — `set_category` ownership ambiguity between SP2 and the foundation SP causes a merge collision.** Mitigation: spec pins `set_category` to live in `app/services/spec_tiers.py` (the F1 module) regardless of landing order; whichever SP lands first defines it, the other wires call sites — single definition site, no duplication.
- **R5 — SQLite tests pass while the `text()` backfill UPDATE is PG-invalid.** Mitigation: per `feedback_sqlite_masks_postgres`, the backfill UPDATEs use only portable SQL (`lower(trim(...))`, parameterized `IN`) and are smoke-run against the live PG container before deploy.

Spec file references (all absolute): `/root/availai/.claude/worktrees/materials-filter-rework/app/models/intelligence.py:40,74-87`; `/root/availai/.claude/worktrees/materials-filter-rework/app/services/category_normalizer.py:20-65`; `/root/availai/.claude/worktrees/materials-filter-rework/app/services/commodity_registry.py:20-41,102`; `/root/availai/.claude/worktrees/materials-filter-rework/app/services/enrichment.py:177-181`; `/root/availai/.claude/worktrees/materials-filter-rework/app/services/authoritative_enrichment_service.py:376-393`; `/root/availai/.claude/worktrees/materials-filter-rework/app/services/material_enrichment_service.py:66-85`; `/root/availai/.claude/worktrees/materials-filter-rework/app/services/mpn_decoder/writer.py:35-55`.

========== SP3:provenance-ladder ==========
I now have complete file:line grounding. The current alembic head is `090_add_condition_mc`. I have everything needed to author the SP3 spec with zero ambiguity.

## SP3 — PROVENANCE & TIER LADDER

**Goal & root cause addressed.**
Make good data always beat guesses so that source-execution ordering is no longer load-bearing. Root cause (audit §D): `record_spec` (`app/services/spec_write_service.py:122-170`) is a 2-tier hardcoded scheme — vendor-API is the only protected tier (`:18`, `:131-144`), and for every other cross-source conflict it does **latest-write-wins** (`:146-157`) with confidence *logged but never compared*. So a `spec_extraction` write (0.85) silently overwrites an `mpn_decode` write (0.95) purely because it ran later. Today the *only* thing protecting decode is execution order — the decode writer runs before the AI extractor (`writer.py:3-5`) and the AI extractor only re-runs cards where `specs_enriched_at IS NULL` (`spec_enrichment_service.py:127`). That ordering is the exact band-aid this SP removes. Parallel defect: `category` is a bare `String` (`intelligence.py:40`) written by 5 uncoordinated paths with no provenance, so a low-tier AI guess can permanently block a higher-tier correction, and a wrong category blocks all spec writes (`record_spec` rejects every spec when category is empty/mismatched — `:55-58`).

**Changes** — numbered.

1. **NEW `app/services/spec_tiers.py`** (does not exist today). Implements F1. Exposes:
   - `SOURCE_TIER: dict[str,int]` exactly per F1 — `manual:100`; vendor APIs `digikey_api,mouser_api,nexar_api,element14_api,oemsecrets_api:90`; `mpn_decode:85`; OEM scrape `partsurfer,psref:80`; `web_search:70`; `brokerbin:65`; `spec_extraction:60`; `ai_guess`/`claude_opus_inferred:40`.
   - `tier_for(source: str) -> int` — `SOURCE_TIER.get(source, 0)` (unknown source = tier 0, can never beat a known source; logged at debug).
   - `resolve(existing: dict | None, incoming: dict) -> bool` — returns `True` iff incoming wins. Each arg is a provenance dict with keys `tier:int`, `confidence:float`, `updated_at:str` (ISO-8601, lexicographically sortable). Rule (F1): `None` existing → win; else compare tuple `(tier, confidence, updated_at)` — incoming wins iff its tuple `>` existing's. Higher tier always overrides; equal tier → higher confidence; exact `(tier,confidence)` tie → newer `updated_at`. Pure function, no DB, no side effects (unit-testable in isolation).

2. **Rewrite `record_spec` conflict logic — `app/services/spec_write_service.py:122-170`.**
   - Delete `_VENDOR_API_SOURCES` (`:18`) and the entire 2-tier branch (`:122-170`).
   - Persist `tier` into the entry built at `:110-115`: `new_entry["tier"] = tier_for(source)` (F2 — `specs_structured[key]` gains `tier` alongside value/source/confidence/updated_at).
   - Replace conflict resolution with a single call: build `incoming = {"tier": tier_for(source), "confidence": confidence, "updated_at": now_iso}`; read `existing = specs.get(spec_key)`; if `existing is not None and not resolve(existing, incoming): return False`. For legacy entries missing `tier`, backfill in-memory via `existing.setdefault("tier", tier_for(existing.get("source","")))` before the call so old rows compare correctly. This is uniform for ALL sources (F1) — removing both the vendor special-case and the latest-wins branch.
   - The facet upsert (`:178-199`) is reached only when the write wins, so a losing incoming never mutates the facet — preserving the ladder in the projection.

3. **Facet provenance projection — `app/services/spec_write_service.py:178-199`.** When the write wins and the facet is upserted, also set the three new columns (Change 6) on the facet from the winning entry: `facet.source = source`, `facet.confidence = confidence`, `facet.tier = new_entry["tier"]`. This is done unconditionally inside the existing upsert block (both the `db.add` new-row path `:181-186` and the in-place update path `:188-199`), so the facet row always mirrors the JSONB winner's provenance. No separate query — the facet write already happens here.

4. **`set_category(card, value, source, confidence)` helper — F2, in `app/services/spec_tiers.py`** (co-located with the ladder it enforces; imported by all category writers). Behavior: normalize `value` via `category_normalizer.normalize_category` (returns `None` for off-vocab — do **not** persist a junk string); if it resolves to `None`, return `False` without writing. Build `incoming = {"tier": tier_for(source), "confidence": confidence, "updated_at": now_iso}`; build `existing` from `card.category_tier/category_confidence/category_source` + `card.updated_at` (or `None` if `card.category` is NULL). Call `resolve(existing, incoming)`; if it wins, set `card.category`, `card.category_source`, `card.category_confidence=confidence`, `card.category_tier=incoming["tier"]` and return `True`; else return `False`. A lower-tier source can never overwrite a higher-tier category (F1/F2).

5. **Route category writers through `set_category` (F2 — replaces 5 scattered writes).** This SP's mandated first consumer + the spec/decode paths it owns:
   - `app/services/mpn_decoder/writer.py:55` — replace `card.category = result.commodity` with `set_category(card, result.commodity, DECODE_SOURCE, result.confidence)`. The surrounding `if not card_cat:` guard (`:50`) and the conflict-skip at `:36-40` are removed: the ladder now decides — decode (tier 85, conf 0.95) can correct a lower-tier category but cannot overwrite a vendor/manual one. Keep the per-card `begin_nested` savepoint. Update the `categorized` counter to increment on `set_category(...) == True` instead of `if not card_cat`.
   - The other four sites (`material_enrichment_service.py:82`, `enrichment.py:181`, `authoritative_enrichment_service.py:380`, and the spec path) are owned by sibling SPs (SP-categorization / SP-description). SP3 **defines and exports** `set_category` as the single helper; this spec's Done criteria require those sites to be migrated, but the edits land in their respective SPs to avoid cross-SP file collisions. SP3 lists them so the foundation contract is explicit.

6. **Pass source/tier so the ladder is exercised.** `spec_enrichment_service.py:179` already passes `source="spec_extraction"` — no change needed there; tier is derived inside `record_spec` via `tier_for`. `writer.py` already passes `source=DECODE_SOURCE` (`:64`). The only behavioral change is that `record_spec` now derives and persists `tier` and uses `resolve()`. **Why the ordering band-aid is now unnecessary:** decode writes `tier=85`; spec_extraction writes `tier=60`. `resolve()` rejects any tier-60 write against an existing tier-85 entry regardless of which ran first or what `specs_enriched_at` says. So the comment at `writer.py:3-5` ("runs BEFORE the AI spec extractor so the 0.95 decode is the baseline the 0.85 pass cannot overwrite") and the `_common.py:7-8` note describe a property now guaranteed by the ladder, not by call order. The decode pass may run before, after, or interleaved with AI extraction with identical results. Update both docstrings to state the ladder is now authoritative (remove the ordering rationale).

**Data model / migration** (F4 — Alembic, upgrade + downgrade, revision id ≤32 chars, single head; chains from current head `090_add_condition_mc`).

Migration `091_spec_provenance` (revision id `091_spec_provenance`, 18 chars; `down_revision = "090_add_condition_mc"`).
- `MaterialSpecFacet` (`app/models/faceted_search.py:45-60`) gains: `source = Column(String(50))`; `confidence = Column(Float)`; `tier = Column(Integer)`. All nullable (legacy rows pre-date provenance). No new index required — provenance is read alongside an already-indexed facet row, not filtered standalone.
- `MaterialCard` (`app/models/intelligence.py`) gains: `category_source = Column(String(50))`; `category_confidence = Column(Float)`; `category_tier = Column(Integer)`. All nullable.
- **upgrade**: 6 `op.add_column` calls. Then a data backfill (same migration, F4) using `op.get_bind()` + `text()`:
  (a) Facet provenance: for each `material_spec_facets` row, copy `source`/`confidence` from the matching `material_cards.specs_structured -> spec_key` JSONB entry and compute `tier` from `SOURCE_TIER`. Do this in SQL with a `jsonb` extract joined `material_spec_facets f JOIN material_cards c ON c.id=f.material_card_id`, setting `f.source = c.specs_structured->f.spec_key->>'source'`, `f.confidence = (c.specs_structured->f.spec_key->>'confidence')::float`. For `tier`, emit a `CASE` over the F1 `SOURCE_TIER` literals (the migration carries its own literal copy of the map — migrations must not import app code that may drift).
  (b) Category provenance: rows with non-NULL `category` but NULL `category_source` get `category_source='legacy_backfill'`, `category_confidence=0.5`, `category_tier=50` (a deliberate mid-tier so any real future source — decode 85, vendor 90 — overrides it, but a tier-40 AI guess does not silently flip an existing human-reviewed category). Document this constant in the migration.
- **downgrade**: 6 `op.drop_column` calls (3 on `material_spec_facets`, 3 on `material_cards`), reverse order. No data restore needed (additive columns).
- After creating: run `alembic heads` → single head; `alembic upgrade head` → `downgrade -1` → `upgrade head` round-trip on local PG (SQLite masks PG JSON ops — verify the JSONB extract on real Postgres per [[feedback_sqlite_masks_postgres]]).

**Interfaces & dependencies.**
- **Consumes from foundation:** F1 (`SOURCE_TIER`, `resolve`) — implemented *by* this SP in `spec_tiers.py`; F2 provenance columns — added by this SP's migration; F4 discipline.
- **Exposes to other SPs:** `app/services/spec_tiers.py` — `SOURCE_TIER`, `tier_for(source)`, `resolve(existing, incoming)`, `set_category(card, value, source, confidence)`. SP-categorization, SP-description, SP-mpn-decode, and the authoritative/web tiers all import `set_category` from here and `record_spec`'s tier behavior is automatic. SP-mpn-decode no longer needs its "runs before AI" ordering guarantee (Change 6).
- **Does not own:** the status state machine / `select_batch` re-eligibility / `enrichment_attempt_version` / `needs_reeval` / spec-freeze stamp (F3) — those are sibling SPs. SP3 only touches `tier` persistence and conflict resolution. The `specs_enriched_at` freeze (audit §D) is mentioned only because removing the ordering band-aid (Change 6) makes the freeze-fix SP independent of write-order.

**Tests** (TDD; `tests/test_spec_tiers.py` + extend `tests/test_spec_write_service.py`; `TESTING=1`).
Unit — `resolve()` / ladder:
1. `resolve(None, anything)` → `True`.
2. Higher tier always wins: existing `{tier:60,conf:0.99}` vs incoming `{tier:85,conf:0.50}` → `True` (the headline regression: decode beats higher-confidence extraction).
3. Lower tier always loses: existing `{tier:85,conf:0.95}` vs incoming `{tier:60,conf:0.85}` → `False`.
4. Equal tier, higher confidence wins: `{tier:60,0.80}` vs `{tier:60,0.90}` → `True`; reverse → `False`.
5. Exact `(tier,conf)` tie → newer `updated_at` wins; identical timestamps → `False` (no churn).
6. `tier_for` unknown source → 0; loses to any known source.
Unit/integration — `record_spec`:
7. **Freeze-bug regression:** decode `record_spec(source="mpn_decode", confidence=0.95)` then `record_spec(source="spec_extraction", confidence=0.85)` for the same `spec_key` → second returns `False`, JSONB + facet still hold the decode value/tier=85. (Today this passes incorrectly via latest-wins.)
8. Reverse order (extraction first, then decode) → decode wins → facet/JSONB upgraded to tier 85. Proves order-independence (kills the band-aid).
9. Vendor-API still authoritative via tiers: `digikey_api`(90) not overwritten by `mpn_decode`(85) or `spec_extraction`(60).
10. Legacy entry with no `tier` key → backfilled from its `source` before compare; an incoming higher-tier write still wins.
11. Facet row carries `source`/`confidence`/`tier` after a winning write; a losing write leaves the facet untouched.
Unit/integration — `set_category`:
12. Off-vocab value → `normalize_category` returns `None` → `set_category` returns `False`, `card.category` unchanged (no junk persisted).
13. **Category cannot be downgraded:** card with `category_tier=90` (vendor) → `set_category(card, "...", "spec_extraction", 0.99)` (tier 60) returns `False`, category unchanged.
14. Higher tier corrects lower: card with `category_tier=40` (ai_guess) → `set_category(card, "dram", "mpn_decode", 0.95)` (tier 85) wins; `category`, `category_source`, `category_tier=85` updated.
15. Equal-tier higher-confidence category wins; exact tie → newer wins.
Migration:
16. `tests/test_migrations.py`-style: revision id ≤32 chars; single `alembic heads`; upgrade→downgrade→upgrade round-trip; backfill populates facet provenance from JSONB and category provenance to the legacy mid-tier.

**Done criteria** (measurable).
- `_VENDOR_API_SOURCES` and the latest-wins branch are gone from `spec_write_service.py`; all conflict resolution flows through `resolve()`. (grep: zero references to `_VENDOR_API_SOURCES`.)
- Every `specs_structured` entry written post-deploy has a `tier` key; every `material_spec_facets` row written post-deploy has non-NULL `source/confidence/tier`; backfill leaves zero pre-existing facet rows with NULL `tier` where the JSONB entry had a known source.
- `mpn_decoder/writer.py:55` writes category exclusively through `set_category`; `writer.py` and `_common.py` docstrings no longer claim correctness depends on run-order.
- Regression tests 7 + 8 + 13 pass (the freeze, the order-independence, the category-downgrade block). Full suite green: `TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/materials-filter-rework pytest tests/ -q`.
- APP_MAP_DATABASE.md updated with the 6 new columns; APP_MAP_INTERACTIONS.md updated with the `spec_tiers.resolve`/`set_category` contract (per [[feedback_update_app_map]]).

**Risks & mitigations.**
- *Legacy JSONB entries lack `tier`* → in-memory `setdefault` from `source` before `resolve` (Change 2); covered by test 10.
- *SQLite masks the JSONB backfill* → round-trip the migration on live PG before merge ([[feedback_sqlite_masks_postgres]]); the `->>'confidence'::float` cast is PG-specific.
- *Cross-SP file collision on the 4 non-decode category sites* → SP3 owns only `spec_tiers.py` + `writer.py` + `spec_write_service.py`; the other call-site edits land in their owning SPs importing this helper — prevents two SPs editing `authoritative_enrichment_service.py:380` simultaneously.
- *Legacy mid-tier (50) backfill choice* → a too-high backfill would block real sources; tier 50 is below decode(85)/vendor(90) so real data always upgrades, yet above ai_guess(40) so a stray guess can't silently flip a human/reviewed category. Documented constant in the migration; test 16 asserts it.
- *Removing the decode conflict-skip at `writer.py:36-40`* (shared `spec_key` across hdd/ssd/dram) → the category ladder now governs which commodity wins, and `record_spec` still rejects specs when category/schema mismatch, so a drive's `capacity_gb` cannot land on a non-drive card; covered by test 9 + the existing schema gate (`spec_write_service.py:62-71`).

Key files: `app/services/spec_tiers.py` (new), `app/services/spec_write_service.py:18,110-170,178-199`, `app/services/mpn_decoder/writer.py:3-5,36-40,50-55,64,72`, `app/services/mpn_decoder/_common.py:7-8`, `app/services/spec_enrichment_service.py:179`, `app/models/faceted_search.py:45-60`, `app/models/intelligence.py:40`, `alembic/versions/091_spec_provenance.py` (new, down_revision `090_add_condition_mc`).

========== SP4:state-machine ==========
I now have all verified line numbers and the exact code shape. The status is written in five `apply_*`/inline places inside `enrich_card` (lines 172, 206, 242, 271, 382, 399). The cleanest single chokepoint for the version stamp is at the end of `enrich_card` (return value is the status) and in the worker's `run_one_batch` after the call — both already touch the card. I have everything needed to write a zero-ambiguity spec.

## SP4 — Self-Healing Enrichment State Machine (no permanent freezes; smart re-checks; working manual button)

**Goal & root cause addressed.** Today a card that reaches any terminal status (`verified`, `web_sourced`, `oem_sourced`, `ai_inferred`) is **never re-selected** by `select_batch` (`worker.py:98-120` filters to `unenriched ∨ not_found_eligible ∨ not_catalogued_eligible` only), so a better source appearing later can never upgrade it (audit A§2-A). `not_found` cards are re-checked on a flat 22h wall-clock with **no changed-input signal**, burning the whole `web_daily_cap` for zero resolutions and starving `unenriched` parts on budget (audit A§4). A zero-facet spec pass stamps `specs_enriched_at` unconditionally (`spec_enrichment_service.py:190`), freezing spec-less cards forever (audit A§D). The manual "enrich" button calls the legacy Haiku path that never touches `enrichment_status` and merely extends the `not_found` backoff — a dead button on frozen cards (audit A§F). SP4 makes every terminal state self-heal: a pipeline-version bump re-runs each terminal card exactly once, a per-MPN `needs_reeval` flag re-triggers on new sightings/offers and the manual button, `not_found` gets exponential + changed-input-gated backoff isolated from the unenriched budget, the spec stamp is conditional with a bounded retry, and the manual button re-enters the authoritative ladder via `refresh=True`.

This consumes the **F1 ladder** (via SP-Provenance's `spec_tiers.resolve`), **F2 provenance** (`enrichment_attempt_version` is the SP4 column; category provenance is SP-Categorization's), **F3 self-heal model** (SP4 owns it), and **F4 migration discipline**.

---

**Changes** (file:line → now → should)

1. **`app/services/enrichment_worker/config.py:13-45`** — `EnrichmentWorkerConfig` has no pipeline version or spec-attempt bound. → Add two module-level constants **above** the dataclass (not env-tunable — they are code-version markers, not operator knobs): `ENRICHMENT_PIPELINE_VERSION: int = 1` and `SPEC_MAX_ATTEMPTS: int = 3`. Docstring: "Bump `ENRICHMENT_PIPELINE_VERSION` by 1 whenever a connector, decoder, OEM source, or web-cap change should re-run already-terminal cards exactly once." Add three env-backed backoff knobs to the dataclass + `from_env()`: `not_found_max_retry_days: int = 14` (exponential ceiling), `not_found_base_retry_hours: int = 22` (reuse existing `not_found_retry_hours` as the base; keep the field, repurpose as the exponential base), and `not_found_web_subcap: int = 30` (the slice of `web_daily_cap` reservable by `not_found`/`ai_inferred` re-checks — implements F3 budget isolation, audit A§4). (F3)

2. **`app/services/authoritative_enrichment_service.py` — stamp version on every terminal write.** The status is set in five places (lines 172, 206, 242, 271, 382, and the inline 399-403). Rather than touch all five, stamp at the **single chokepoint**: in `enrich_card`, immediately before each `return` is noisy; instead add `card.enrichment_attempt_version = ENRICHMENT_PIPELINE_VERSION` and `card.needs_reeval = False` as the **first two lines after `conns = ...` is resolved is wrong** (early-return guard at :311-315 must still skip). Correct placement: a tiny local helper `def _stamp(card, status) -> str: card.enrichment_attempt_version = ENRICHMENT_PIPELINE_VERSION; card.needs_reeval = False; return status` and replace every `return MaterialEnrichmentStatus.X` / `return card.enrichment_status` in `enrich_card` (:315, :323, :338, :357, :366, :393, :406) with `return _stamp(card, MaterialEnrichmentStatus.X)`. This guarantees the version is stamped and `needs_reeval` cleared on **every** path that produces a status, including the early-return guard (so a refresh of a still-verified card re-stamps the version) and the terminal not_found/not_catalogued branch. Import the constant from `enrichment_worker.config`. (F3)

3. **`app/services/enrichment_worker/worker.py:233-248` — poison-pill quarantine** sets `card.enrichment_status = NOT_FOUND` directly without stamping version. → Add `card.enrichment_attempt_version = ENRICHMENT_PIPELINE_VERSION` and `card.needs_reeval = False` in that `except Exception` block alongside the existing status/`enriched_at` writes, so a poison-pill card also advances its version (it must not be re-run every batch by the version-gate). (F3)

4. **`app/services/enrichment_worker/worker.py:59-120` — `select_batch` re-eligibility.** Currently three WHERE branches keyed purely on status + wall-clock. → Restructure the WHERE to:
   - **Version sweep (all statuses):** add `version_stale = MaterialCard.enrichment_attempt_version < ENRICHMENT_PIPELINE_VERSION` as a top-level OR alongside the existing branches. This unfreezes every terminal card exactly once after a bump (drained behind unenriched by the existing `order_by` at :114-116, which keys on `status == UNENRICHED` desc). (F3.1)
   - **Targeted re-trigger:** add `MaterialCard.needs_reeval.is_(True)` as a top-level OR. (F3.2)
   - **`not_found` smarter backoff** (replace the flat-22h `not_found_eligible` at :81-87): eligible when `version_stale ∨ needs_reeval ∨ (last_searched_at > enriched_at) ∨ (enriched_at < now − exponential_cutoff)`, where `exponential_cutoff = min(base_retry_hours × 2^spec_attempts… )` — **no**, use a dedicated counter: gate the time-based arm on `enriched_at < now − min(not_found_base_retry_hours × 2^(enrichment_attempt_version-bumps), not_found_max_retry_days×24)`. Since SP4 does not add a separate `not_found_attempts` column (avoids schema sprawl), compute the exponent from a new lightweight counter — **decision: add `not_found_attempts: int` is over-engineering given the changed-input gate already kills daily churn**; instead the time arm uses a **flat `not_found_max_retry_days` (14d)** and the **`last_searched_at > enriched_at` changed-input arm** does the real work (audit A§4 — "only re-select when new demand since last attempt"). So: `not_found_eligible = status==NOT_FOUND ∧ (enriched_at IS NULL ∨ last_searched_at > enriched_at ∨ enriched_at < now−14d)`. This removes the daily 22h churn: a `not_found` card with no new sighting is re-checked only every 14 days, or immediately when a new sighting bumps `last_searched_at`. (F3, audit A§4)
   - **`ai_inferred` re-selectable on long backoff** (mirror `not_catalogued_eligible` at :89-96): `ai_inferred_eligible = status==AI_INFERRED ∧ (enriched_at IS NULL ∨ enriched_at < now − not_catalogued_retry_days)`. Add to the top-level OR. So flagged guesses get re-checked monthly and upgraded when a real source appears (audit E rec 2). (F3)
   - **Budget isolation** (audit A§4, F3) lives in `run_one_batch` not `select_batch`: see change 5.

5. **`app/services/enrichment_worker/worker.py:187-257` — budget-isolate re-checks.** The web gate at :190 is a single global `web_calls_today >= web_daily_cap`. → Before the per-card loop, partition `batch` is unnecessary; instead, per card compute `is_recheck = card.enrichment_status in (NOT_FOUND, AI_INFERRED, NOT_CATALOGUED)`. Replace the single gate with: web tier disabled for **re-check** cards once `web_calls_today >= not_found_web_subcap`, and for **all** cards once `>= web_daily_cap`. Implement by passing a per-card `disabled` superset: if `is_recheck and web_calls_today >= config.not_found_web_subcap`, pass `disabled | {"web_search"}` to `enrich_card` for that card only (don't mutate the shared persistent set). This reserves `web_daily_cap − not_found_web_subcap` (50 of 80) exclusively for `unenriched`, so stale re-checks can never zero the budget (audit A§4). (F3)

6. **`app/services/spec_enrichment_service.py:188-190` — conditional stamp + bounded retry.** Now `c.specs_enriched_at = now` runs for every processed card regardless of `wrote_any` (freeze D). → (a) Bump `c.spec_attempts = (c.spec_attempts or 0) + 1` for every processed card. (b) Stamp `c.specs_enriched_at = now` **only when** `wrote_any or c.specs_summary` was set **or** `c.spec_attempts >= SPEC_MAX_ATTEMPTS`. So a zero-facet card stays `specs_enriched_at IS NULL` (re-selectable) until either a later pass extracts a facet or it has been tried 3 times, then it's stamped and stops. (F3) The `force=False` gate at :126-127 (`specs_enriched_at.is_(None)`) and `enrich_pending_specs` at :208 then naturally re-select these until the bound. Import `SPEC_MAX_ATTEMPTS` from `enrichment_worker.config`.

7. **`app/services/spec_enrichment_service.py:138-140` — off-vocab re-skip loop.** Now `if cat not in COMMODITY_SPECS: skipped; continue` without stamping, so off-vocab cards re-select every run (freeze E). → **No code change here is needed once SP-Categorization lands** (category is canonical-or-NULL), because `enrich_card_specs`/`enrich_pending_specs` already require `category IS NOT NULL` (`:122`, `:209`): a NULL category is simply not eligible, and a non-NULL category is guaranteed canonical, so `cat in COMMODITY_SPECS` is always true and the `continue` is dead. **SP4 hardens the interaction**: add a guard `if cat and cat not in COMMODITY_SPECS:` → stamp `specs_enriched_at` and bump `spec_attempts` on those cards before `continue` (defense-in-depth so that even if a non-canonical category slips through, it cannot monopolize batches). Document the dependency on SP-Categorization explicitly in a comment. (F3, audit A§E)

8. **`app/routers/htmx_views.py:8738-8767` — fix the manual button.** Now calls `material_enrichment_service.enrich_material_cards([material_id], db)` (legacy Haiku, never sets status) then `enrich_card_specs(..., force=True)`. → Replace the body: set `mc.needs_reeval = True`, then call `await authoritative_enrichment_service.enrich_cards([material_id], db, refresh=True)` (refresh=True bypasses the :311-315 verified/oem guard so even a `verified` card re-enters the ladder), then `await enrich_card_specs([material_id], db, force=True)` (force re-runs specs ignoring the `specs_enriched_at` gate), then `db.refresh(mc)`. Drop the `material_enrichment_service` import. The `enrich_cards` call commits internally (`:412`); `needs_reeval` is set before it and cleared inside `_stamp` (change 2) on success — so a successful run leaves `needs_reeval=False`, a failure leaves it `True` so the worker retries. (F3.4, audit A§F)

9. **`app/search_service.py:1987-1988` — set `needs_reeval` on new sighting/offer.** This is the search-path upsert (`_upsert_material_card`) that bumps `card.search_count` and `card.last_searched_at` when a part is searched/sighted. → Add `card.needs_reeval = True` immediately after the `search_count`/`last_searched_at` bump. A new sighting/offer is exactly "a better data source may now exist for this MPN" → the worker re-checks it next batch via the `needs_reeval` OR (change 4). Combined with the `last_searched_at > enriched_at` arm, this is the changed-input self-heal for `not_found` (audit A§4, F3.2). (F3)

---

**Data model / migration.**

New columns on `MaterialCard` (`app/models/intelligence.py`, after `enrichment_provenance` at :61):
```
enrichment_attempt_version = Column(Integer, nullable=False, server_default="0")
needs_reeval               = Column(Boolean, nullable=False, server_default="false", index=True)
spec_attempts              = Column(Integer, nullable=False, server_default="0")
```
- `needs_reeval` is indexed (it is an OR-branch predicate in `select_batch`, low-cardinality but the worker query runs every 30s; a partial index `WHERE needs_reeval` is the right shape).
- No validator needed (ints/bool, server-defaulted).

**Migration** `alembic/versions/091_selfheal_state_columns.py` (revision id `091_selfheal_state` — 17 chars, ≤32; `down_revision = "090_add_condition_mc"`):
- **upgrade:** three `op.add_column(...)` with the server_defaults above; `op.create_index("ix_material_cards_needs_reeval", "material_cards", ["needs_reeval"], postgresql_where=sa.text("needs_reeval"))`.
- **downgrade:** `op.drop_index("ix_material_cards_needs_reeval", "material_cards")`; three `op.drop_column(...)` in reverse order.
- **Backfill:** none required — `server_default` makes existing rows `version=0` (→ all immediately version-stale vs `ENRICHMENT_PIPELINE_VERSION=1`, which is the **intended** one-time re-run of the whole existing population on first deploy after the connector/decoder fixes from sibling SPs land), `needs_reeval=false`, `spec_attempts=0`. This is the desired self-heal-on-ship behavior; no `op.get_bind()`/`text()` data step.
- After creating: run `alembic heads` → confirm single head; `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` round-trip on SQLite **and** verify against live PG per `feedback_sqlite_masks_postgres` / `feedback_alembic_revision_id_length`. (F4)

---

**Interfaces & dependencies.**
- **Consumes from foundation:** SP-Provenance's `spec_tiers.resolve` (used indirectly via `record_spec`; SP4 does not call it but the conditional-stamp logic in change 6 relies on `wrote_any` reflecting tier-gated writes). SP-Categorization's canonical-or-NULL guarantee (change 7 dead-code rationale). F3 constants live in `enrichment_worker/config.py` (SP4 owns them).
- **Exposes:** `ENRICHMENT_PIPELINE_VERSION`, `SPEC_MAX_ATTEMPTS` (importable constants); `MaterialCard.enrichment_attempt_version / needs_reeval / spec_attempts`; the `_stamp` helper convention in `authoritative_enrichment_service`. Other SPs (e.g. a new BrokerBin connector) bump `ENRICHMENT_PIPELINE_VERSION` to trigger a one-time re-sweep — document this in the constant's docstring and in `docs/APP_MAP_INTERACTIONS.md` (F4 / CLAUDE.md update-app-map rule).
- **No cross-SP code coupling beyond imports** — SP4 can land independently of the BrokerBin/PartSurfer source work (those just bump the version constant when they ship).

---

**Tests** (`tests/test_enrichment_self_heal.py`, plus additions to existing worker/spec test files; `TESTING=1`, in-memory SQLite, `--override-ini="addopts="` for single-file runs):

*Unit — `select_batch` (the freeze regressions):*
1. **Version sweep:** card with `status=verified, enrichment_attempt_version=0`, `ENRICHMENT_PIPELINE_VERSION=1` → IS in batch; same card at `version=1` → NOT in batch. (Regression for freeze A.)
2. **One-time drain:** after a simulated re-run stamps `version=1`, re-querying `select_batch` no longer returns it (proves "exactly once").
3. **needs_reeval:** `status=not_found, needs_reeval=True, enriched_at=now` → IS in batch despite fresh `enriched_at`. (F3.2)
4. **not_found changed-input:** `not_found, enriched_at=now, last_searched_at=now−1h` (no new search) → NOT eligible; same card with `last_searched_at=now+…` i.e. `> enriched_at` → eligible. (Regression for audit A§4 daily churn.)
5. **not_found flat-clock no longer 22h:** `not_found, enriched_at=now−23h, last_searched_at < enriched_at` → NOT eligible (proves 22h churn removed); `enriched_at=now−15d` → eligible.
6. **ai_inferred long backoff:** `ai_inferred, enriched_at=now−31d` → eligible; `now−1d` → NOT. (Regression: ai_inferred was previously never re-selected.)
7. **Ordering preserved:** mixed `unenriched`+version-stale-`verified` batch → unenriched first (assert order keys hold with the new OR branches).

*Unit — version stamping:*
8. `enrich_card` over a mocked-miss card asserts `enrichment_attempt_version == ENRICHMENT_PIPELINE_VERSION` and `needs_reeval is False` on the returned not_found card; same for a mocked verified hit.
9. Poison-pill path (`run_one_batch` with an `enrich_card` raising non-Claude) → quarantined card has `version` stamped (won't re-run every batch).

*Unit — spec stamp / bound (freeze D):*
10. **Zero-facet card is retried, not frozen:** `enrich_card_specs` where Claude returns no spec ≥ FACET_MIN_CONF → `specs_enriched_at IS NULL`, `spec_attempts == 1`; second pass → `spec_attempts == 2`, still NULL; third pass → `spec_attempts == 3`, `specs_enriched_at` NOW stamped (bounded stop). (Regression for freeze D.)
11. **Wrote-a-facet card stamps immediately:** one facet written → `specs_enriched_at` set on pass 1.
12. **Off-vocab defense (change 7):** card with a non-canonical category somehow present → stamped + `spec_attempts` bumped, not infinite-re-skipped. (Regression for freeze E.)

*Integration — manual button (freeze F):*
13. POST `/v2/partials/materials/{id}/enrich` on a `verified` card (mock `enrich_cards` + `enrich_card_specs`) → asserts `enrich_cards` called with `refresh=True` and `needs_reeval` was set True before the call; response is the detail partial. (Regression for freeze F dead-button.)
14. Manual button no longer imports/calls `material_enrichment_service.enrich_material_cards` (assert the legacy path is gone).

*Integration — search path:*
15. `_upsert_material_card` with a new sighting on an existing `not_found` card → `needs_reeval is True` and `last_searched_at > enriched_at`, then `select_batch` returns it.

*Migration:*
16. Apply `091` on a fresh SQLite DB, assert columns + index exist, round-trip downgrade/upgrade; assert revision id length ≤ 32 (per `feedback_alembic_revision_id_length` guard test pattern).

---

**Done criteria** (measurable):
- After deploy with `ENRICHMENT_PIPELINE_VERSION` bumped to 1, **100% of the ~1,9xx terminal cards become re-eligible exactly once** then settle (DB: `SELECT count(*) WHERE enrichment_attempt_version < 1` trends to 0 as the worker drains; no card re-runs twice for the same version).
- **Zero `not_found` cards with no new sighting are re-checked inside 14 days** (DB: re-checks correlate with `last_searched_at > prior enriched_at`, not the wall clock). Daily `not_found` web-call spend drops from "up to full 80 cap on stale re-checks" to ≤ `not_found_web_subcap` (30), leaving ≥50/day for `unenriched` (audit A§4 target).
- **No spec-less card is permanently frozen:** every card has `specs_enriched_at IS NULL` (still retrying, `spec_attempts < 3`) or `spec_attempts ≤ 3` — no card with `specs_enriched_at IS NOT NULL` and zero facets and `spec_attempts < 3`.
- **Manual button re-enters the ladder:** clicking enrich on a `verified`/`not_found` card runs `enrich_card(refresh=True)` (verified via log line / status change), not the Haiku no-op.
- Full suite green (`pytest tests/ -q`); the 16 new tests pass; `pre-commit run --all-files` clean; `docs/APP_MAP_DATABASE.md` (3 new columns) and `APP_MAP_INTERACTIONS.md` (version-bump self-heal contract) updated in the same PR.

---

**Risks & mitigations.**
- **Mass re-run storm on first deploy** (every terminal card version-stale at once) → mitigated by the existing `daily_cap=200` + ordering (unenriched first); the sweep drains over days behind real work, never bursts. Stagger further by bumping the version only after the sibling connector/decoder SPs land (so the re-run actually finds new data — otherwise it just re-confirms misses and spends budget for nothing). **Decision: gate the version bump to ship in the same release as ≥1 new resolution source**, documented in the constant's docstring.
- **`needs_reeval` thrash** if a high-search part is sighted repeatedly → bounded: `needs_reeval` is cleared on every `_stamp` (every completed run), and re-checks are web-budget-isolated (change 5), so a hot part costs at most one re-run per worker pass, capped by `not_found_web_subcap`.
- **`last_searched_at > enriched_at` timezone/NULL edge** → both are `UTCDateTime`; `enriched_at` is NULL only for never-run cards (already covered by the `IS NULL` arm); SQLite vs PG comparison verified against live PG per `feedback_sqlite_masks_postgres`.
- **Spec `force=True` from manual button re-charges Claude** on a card that legitimately has no specs → acceptable (manual, single card, user-initiated); the `spec_attempts` bound does not block `force=True` (intended escape hatch).
- **Index churn** on `needs_reeval` (frequent flips) → partial index `WHERE needs_reeval` keeps it tiny (only the handful of currently-flagged rows are indexed).

Spec file references (all absolute): `/root/availai/.claude/worktrees/materials-filter-rework/app/services/enrichment_worker/config.py`, `app/services/enrichment_worker/worker.py`, `app/services/authoritative_enrichment_service.py`, `app/services/spec_enrichment_service.py`, `app/routers/htmx_views.py`, `app/search_service.py`, `app/models/intelligence.py`, `app/constants.py`, new migration `alembic/versions/091_selfheal_state_columns.py` (down_revision `090_add_condition_mc`).

========== SP5:oem-acquisition ==========
I have full grounding now. Here is the SP5 design spec.

## SP5 — OEM/FRU DATA ACQUISITION (give the data-poor cards real data)

**Goal & root cause addressed.**
1,628 cards (88% of the catalogue) are `not_found` and almost all are OEM/FRU spares (HP `######-001`, Lenovo `00xxxxx`/`5Bxxxxxxx`, Acer dotted codes) with a blank manufacturer. The only OEM-resolution mechanisms wired today are two Claude `web_search` tiers (`cross_reference_mpn`, `extract_oem_description` in `oem_extractor.py:67,182`) whose Python gates (`conf>=0.90` + both codes verbatim in the quote) clear so rarely they yield near-zero (12 `oem_sourced` + 73 `not_catalogued` total). The commodity distributor APIs structurally return 0 for FRU codes. The one already-credentialed source with real FRU reach — BrokerBin (`sources.py:592`) — is built and used for *search* but is **not in `SOURCE_ORDER`**, so `enrich_card` never queries it. SP5 wires BrokerBin into the OEM path as a dedicated tier, instruments and tunes the Claude OEM tiers, optionally adds OEM scrapers (flagged as a user decision), fixes two secondary connector bugs (Element14 403, connector_status binding gap), and bumps `ENRICHMENT_PIPELINE_VERSION` so all frozen cards re-run through the new sources.

**Changes** — numbered.

1. **BrokerBin enrichment tier — new module `app/services/enrichment_worker/broker_extractor.py`.**
   - Today: `enrich_card` (`authoritative_enrichment_service.py:317-366`) queries only `SOURCE_ORDER = ["digikey","mouser","element14","oemsecrets","nexar"]`; BrokerBin is never consulted in enrichment.
   - Should do: add a `broker_resolve(card, connectors, …) -> BrokerResolveResult` that calls the existing `BrokerBinConnector.search(display_mpn)`, then **normalizes/trusts noisy broker free-text** before writing. Trust rules (all in Python, never trust a single listing):
     - **MPN match gate:** keep only listings where `normalize_mpn_key(listing["mpn_matched"]) == card.normalized_mpn` (exact, mirroring `merge_authoritative` `:71`). Broker `part` strings are dirty; the exact-normalized match is the load-bearing guard.
     - **Corroboration gate:** require **>= 2 distinct sellers** (distinct `vendor_name`) for the matched MPN before writing description/manufacturer, OR a single listing whose connector `confidence == 5` (qty>0 AND price>0). This is the broker analogue of the OEM tiers' verbatim gate — it converts "one noisy listing" into "corroborated real-world identity."
     - **Manufacturer:** take the modal non-empty `mfg` across matched listings; write only if it appears in >= 2 listings (defends against a single mistyped `mfg`).
     - **Description:** take the **longest** matched-listing `description` (broker descriptions are terse; longest carries the most signal), strip seller boilerplate, cap 500 chars. Never synthesize.
     - **Sourcing lead:** capture the highest-`confidence` matched listing's `vendor_name` + `vendor_phone` + `vendor_email` + `condition` + `country` + `qty_available` + `age_in_days` into a new `card.sourcing_leads` JSONB list (see data model) — this is the unique BrokerBin value: a live seller for the exact spare.
   - **Ladder position (F1):** BrokerBin is `source="brokerbin"`, **tier 65** — below `web_search` (70) and `oem_scrape` (80), above `spec_extraction` (60). Rationale: a corroborated broker listing is weaker than an OEM-official page or an authorized-distributor web page, but stronger than an AI-mined spec. In `enrich_card` it runs **after** the OEM web tiers and **before** the `infer_part` fallback — i.e. inserted at `authoritative_enrichment_service.py:366`, just before line 368. New terminal status: a BrokerBin hit writes status `oem_sourced` (it is a sourced, non-distributor-verified identity) via a new `apply_broker_sourced(card, result)` helper that sets `enrichment_source="brokerbin"`, writes description/manufacturer/category through the F2 provenance path, appends `sourcing_leads`, and stamps `enrichment_attempt_version` (F3).
   - All spec/description writes go through `record_spec(..., source="brokerbin", confidence=<0.70 base, +0.10 per extra corroborating seller, cap 0.85>)` and `set_category(card, value, "brokerbin", confidence)` (F2 helper from SP3) so the F1 ladder governs whether BrokerBin can overwrite an existing value (it cannot overwrite tier-90 vendor data or tier-80 OEM-scrape data; it can fill a `not_found`/`web_sourced`-empty card).

2. **Wire BrokerBin into the connector build + enrich path.**
   - Today: `_connectors_in_order` (`authoritative_enrichment_service.py:95-104`) filters `_build_connectors(db)` down to `SOURCE_ORDER`, dropping BrokerBin.
   - Should do: return `(distributor_conns, broker_conn)` — keep `SOURCE_ORDER` distributor list unchanged for `fetch_authoritative` (BrokerBin must NOT pollute the tier-90 verified merge), and separately surface the BrokerBin connector instance for the new tier 1 above. `enrich_card` gains the broker connector via the existing `connectors`/build path; no change to the verified-merge logic.

3. **OEM scraper connectors (PartSurfer / Lenovo PSREF) — USER DECISION, flagged.**
   - This SP **does not unilaterally build scrapers.** It presents the open question explicitly (see "Risks & open decision"). If approved: new connectors `app/connectors/partsurfer.py` (HPE; `partsurfer.hpe.com`, already on `oem_domains.py:27` allowlist) and `app/connectors/lenovo_psref.py`, both `source="oem_scrape"`, **tier 80** (F1), feeding `apply_oem_sourced`. Mandatory guards: (a) **allowlist gating** — only fetch the two hardcoded hosts; (b) **per-host circuit breaker** mirroring the existing connector cooldown (`authoritative_enrichment_service.py:144-146`) — N consecutive failures disable the scraper for the run; (c) **response caching** keyed on normalized MPN (reuse `intel_cache`, 30-day TTL — OEM spare descriptions are static); (d) classify only when `classify_oem_vendor` returns `hpe`/`lenovo` so the scrape fires only on the matching family. ToS-gray and layout-fragile; default **OFF** behind `OEM_SCRAPE_ENABLED=false`. BrokerBin-only ships regardless of this decision.

4. **Instrument + tune the Claude OEM web tiers (`oem_extractor.py:67,182`).**
   - Today: gate rejections log a one-line `logger.info` per reason but are not counted; yield is near-zero and undiagnosable; the 80/day web sub-cap (`config.py` `web_daily_cap`) is shared globally and gets exhausted by `not_found` re-checks.
   - Should do: (a) **instrument gate-rejection reasons** — each `return _XR_FAILED`/`_OEM_FAILED` increments a labelled counter (`no_trusted_source`, `linkage_missing_code`, `resolved_equals_original`, `confidence_below_threshold`, `mpn_mismatch`, `desc_too_short`) emitted to the existing batch stats dict + Prometheus, so the dominant rejection reason becomes measurable before loosening any gate. (b) **Do NOT loosen the 0.90/verbatim gates in this SP** — gate tuning is gated on the instrumentation data (changing a trust gate without the rejection histogram is a guess; defer the actual loosen to a follow-up once the histogram shows which gate dominates). (c) **Reserve a `web_daily_cap` sub-cap for `unenriched` cards** so `not_found` re-check churn can't zero the budget before new OEM tiers run on fresh parts — add `WEB_DAILY_CAP_UNENRICHED_RESERVE` (default 40 of 80) consumed only by `not_found`/`not_catalogued` re-checks (this is the SP5-scoped budget fix; the per-card backoff is SP4's).

5. **Secondary fix — Element14 HTTP 403.**
   - Today: `fetch_authoritative` catches `ConnectorAuthError` and run-disables Element14 (`:138-142`); in prod it 403s every run.
   - Should do: the 403 is an auth/endpoint problem in `app/connectors/element14.py`, not a pipeline bug. SP5 scope: (a) confirm the failing request (header/base-url/region param) against the Element14 v2 API and correct it; (b) if the key is genuinely invalid, leave the run-disable behavior (correct) and surface it in `connector_status` (fix 6) so it shows disabled honestly rather than silently. Include a regression test that the connector sends the corrected auth header.

6. **Secondary fix — connector_status settings-binding gap (`connector_status.py:13-24`).**
   - Today: `log_connector_status` reads `settings.ebay_client_id` / `settings.sourcengine_api_key` (Pydantic), reporting eBay + Sourcengine "disabled" even though their env vars are present, because the runtime builder (`credential_service.get_credentials_batch`, `:140-165`) resolves **DB-first, env-fallback** while `connector_status` reads only `settings.*`.
   - Should do: route `log_connector_status` through the **same** `credential_is_set(db, source_name, env_var_name)` (`credential_service.py:132-137`) used at runtime, so startup status reflects the actual DB/env credential the builder will use. It must take a `db: Session` (called from the lifespan, which already has one). This closes the divergence at the root rather than mirroring env reads.

7. **Bump `ENRICHMENT_PIPELINE_VERSION` on ship (F3).**
   - SP5 adds a source (BrokerBin), changes a tier ladder, and may add scrapers — exactly the trigger F3 defines. Increment the constant (owned by SP4) by 1 in the same PR. Combined with SP4's `OR enrichment_attempt_version < ENRICHMENT_PIPELINE_VERSION` in all `select_batch` branches, every terminal card — including the 1,628 `not_found`, the 73 `not_catalogued`, and the frozen `verified`/`web_sourced`/`ai_inferred` set — re-runs exactly once through the new BrokerBin tier, drained behind `unenriched` by the existing ordering (`worker.py:114-116`).

**Data model / migration (F4).**
One Alembic migration, revision id `091_sp5_sourcing_leads` (<= 32 chars), single head verified after creation.
- `MaterialCard.sourcing_leads` — `JSONB`, nullable, no server default. Stores a list of `{vendor_name, vendor_phone, vendor_email, condition, country, qty_available, age_in_days, source, fetched_at}` dicts from BrokerBin matched listings (cap 10 most-recent by `age_in_days`).
- No index needed (read with the card; not filtered on). If a future "has a sourcing lead" facet is wanted, add a partial GIN index in a later migration — out of SP5 scope.
- **upgrade:** `op.add_column("material_cards", sa.Column("sourcing_leads", postgresql.JSONB, nullable=True))`.
- **downgrade:** `op.drop_column("material_cards", "sourcing_leads")`.
- **Backfill:** none at migration time (column starts NULL). The F3 version bump re-runs `not_found` cards through BrokerBin, populating `sourcing_leads` organically as the worker drains — no bulk backfill, no prod SQL. (SP5 consumes SP3's `MaterialSpecFacet.source/confidence/tier` and the `category_*` columns and SP4's `enrichment_attempt_version`/`ENRICHMENT_PIPELINE_VERSION`; those migrations are owned by SP3/SP4, not duplicated here.)

**Interfaces & dependencies.**
- **Consumes from foundation:** F1 `spec_tiers.resolve()` + `SOURCE_TIER` (registers `brokerbin:65`, `oem_scrape:80` — these tier values are defined in F1's table, SP5 only references them); F2 `record_spec(source=, confidence=)` provenance write + `set_category(card, value, source, confidence)` helper (SP3); F3 `ENRICHMENT_PIPELINE_VERSION` + `enrichment_attempt_version` stamp + `select_batch` re-eligibility (SP4).
- **Hard dependency:** SP5 must land **after** SP3 (provenance) and SP4 (re-trigger) — without `set_category`/the ladder, BrokerBin's noisy data could overwrite better data (the exact bug F1/F2 exist to prevent); without the version bump the new tier never reaches the 1,628 frozen cards.
- **Exposes:** `broker_extractor.broker_resolve()` + `BrokerResolveResult`; `apply_broker_sourced(card, result)`; `MaterialCard.sourcing_leads` (consumed by the card UI to render a "sourcing lead" chip — UI work is a separate SP, SP5 only populates the column); the OEM gate-rejection counters (consumed by the batch-stats logger + `/metrics`); corrected `element14` auth; `connector_status(db)` honest status.

**Tests** (TDD — write first).
- *Unit, BrokerBin trust/normalization:* (a) two distinct sellers for the exact normalized MPN → description+manufacturer written, status `oem_sourced`, provenance `source="brokerbin"`, `tier=65`; (b) one listing only, `confidence<5` → corroboration gate rejects, no description write, card stays its prior status; (c) a single `confidence==5` listing → accepted; (d) listings whose `part` doesn't normalize-match the card → all skipped (MPN gate); (e) modal manufacturer requires >=2 → a single mistyped `mfg` is dropped; (f) `sourcing_leads` populated with the highest-confidence matched listing's contact, capped at 10.
- *Unit, ladder ordering (regression for the freeze/leak bugs):* (g) BrokerBin (tier 65) **cannot** overwrite an existing tier-90 vendor-API spec via `resolve()`; (h) BrokerBin **cannot** overwrite a tier-80 `oem_scrape` category via `set_category`; (i) BrokerBin **can** fill a field that is empty/`ai_guess`(40); (j) equal-tier tie-break falls to higher confidence then newer `updated_at` (guards against the old "latest write wins" at `spec_write_service.py:146`).
- *Integration, the core SP5 outcome:* (k) a `not_found` card + a stubbed BrokerBin connector returning two corroborating listings → after `enrich_card` the card is `oem_sourced` with a brokerbin-provenanced description and a `sourcing_leads` entry (this is the headline "data-poor card gets real data" test).
- *Integration, version-bump re-queue (regression for the §A/§D freeze):* (l) bump `ENRICHMENT_PIPELINE_VERSION`, assert a previously-`not_found` card with stale `enrichment_attempt_version` is returned by `select_batch`; (m) after a successful BrokerBin re-run its `enrichment_attempt_version == ENRICHMENT_PIPELINE_VERSION` so it is not re-selected a second time.
- *Unit, OEM instrumentation:* (n) each gate-rejection path in `oem_extractor` increments its labelled counter; a passing extraction increments none.
- *Unit, secondary fixes:* (o) `element14` connector sends the corrected auth header (regression for the 403); (p) `connector_status(db)` reports eBay/Sourcengine **enabled** when the credential is present via env even though `settings.*` is unset (regression for the binding gap), using a fake `ApiSource`/monkeypatched `os.getenv`.
- *Scraper tests (only if approved):* allowlist rejects a non-allowlisted host; circuit breaker disables after N failures; cache hit avoids a second fetch.

**Done criteria** (measurable).
- BrokerBin tier live in `enrich_card`; running the suite green (`TESTING=1 … pytest tests/ -q`).
- On a sample of 200 previously-`not_found` OEM/FRU cards re-run through the new pipeline, **>= 15%** transition out of `not_found` (to `oem_sourced` via BrokerBin) — vs. the current ~0%. (Tracked via the `enrichment_status` distribution before/after.)
- Every BrokerBin-sourced card carries provenance `source="brokerbin"`, `tier=65` and a non-empty `sourcing_leads` — assertable in DB.
- OEM gate-rejection histogram is populated at `/metrics` (the dominant rejection reason is now visible).
- `connector_status` reports eBay/Sourcengine consistently with the runtime builder; Element14 either resolves (no 403) or shows honestly disabled.
- After the version bump, the not_found pool drains exactly once through the new sources (no infinite re-run — `enrichment_attempt_version` proves single-pass).

**Risks & mitigations / open decision.**
- **OPEN USER DECISION (must be resolved in the spec, no TBD):** *BrokerBin-only* vs *BrokerBin + OEM scrapers.* **Recommendation: ship BrokerBin-only now; gate scrapers behind `OEM_SCRAPE_ENABLED=false` and a separate go/no-go.** Reasoning: BrokerBin is licensed, already credentialed, low-effort, and the only no-ToS-risk source with FRU reach; PartSurfer/PSREF are higher-accuracy for the largest families but ToS-gray, API-less, and layout-fragile (high maintenance, breakage risk). The scraper code can be written behind the flag so flipping it on later needs no new migration — but the decision to enable it in prod is the user's, not Claude's.
- **Broker free-text is noisy** → mitigated by the exact-normalized-MPN gate + >=2-seller corroboration + tier-65 ceiling (can never overwrite vendor/OEM data via F1).
- **Stale sourcing leads** → `sourcing_leads` capped at 10 and re-populated on each re-run; `age_in_days` stored so the UI can de-emphasize old listings.
- **Version-bump re-run cost** → the one-time re-run of ~1,800 cards is drained behind `unenriched` by existing ordering and bounded by `daily_cap`/`web_daily_cap`; BrokerBin calls are not Claude-billed, so the marginal cost is the BrokerBin API quota, not the web budget.
- **Scrapers (if enabled) break on layout drift** → per-host circuit breaker + 30-day cache + allowlist gating; failures degrade to the existing tiers, never crash the batch.

Key files: `app/services/authoritative_enrichment_service.py` (`:38` SOURCE_ORDER, `:95-104` connector build, `:317-366` enrich tiers, new `apply_broker_sourced`), new `app/services/enrichment_worker/broker_extractor.py`, `app/connectors/sources.py:592` (BrokerBin connector — reused as-is), `app/connector_status.py:13-24` (fix 6), `app/connectors/element14.py` (fix 5), `app/services/enrichment_worker/oem_extractor.py:67,182` (fix 4 instrumentation), `app/services/enrichment_worker/config.py` (web sub-cap reserve), `app/models/intelligence.py` (`sourcing_leads` column), `alembic/versions/091_sp5_sourcing_leads*.py`.
