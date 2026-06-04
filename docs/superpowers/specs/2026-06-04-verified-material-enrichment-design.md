# Verified Material Enrichment + Part-Number Import — Design Spec

**Date:** 2026-06-04
**Status:** Approved design, pending implementation plan
**Branch:** `feat/verified-material-enrichment`

## 1. Problem & Goal

A buyer-supplied report (`report1780605266325.xls` — actually an HTML-table export) contains **1,827 unique part numbers** ("every part we've ever been asked for"), single column, no manufacturer/description/specs. We need to add all of them to availai's `material_cards` with **core-attribute** enrichment that is **accurate and source-attributed**, so they are usefully filterable.

The hard requirement is **"no unlabeled hallucination."** Every value shown must either be:
- **Verified** — pulled from an authoritative catalog source, attributed to that source per field; or
- **AI-inferred** — a best-effort guess, **structurally and visually flagged as unverified**; or
- **Not found** — MPN only, explicitly marked.

A value must never appear *as if* verified when it was guessed.

### 1.1 Why this is non-trivial (the root problem)
availai's existing material enrichment (`app/services/material_enrichment_service.py`) generates descriptions/specs by asking **Claude Haiku to infer them from the MPN string alone** — no authoritative lookup. That is itself the hallucination vector. availai's authorized-distributor connectors exist only for **supplier search** (sightings), and they currently parse only `manufacturer`, `description`, `datasheet_url` (+ price/stock) — discarding the catalog category/lifecycle/package/parameter data the APIs actually return.

This design fixes the root cause: it introduces a **verified, source-attributed enrichment path** that becomes the canonical accurate enrichment for *any* future list, not just this one.

### 1.2 The input list (characterized)
- 1,827 rows, **0 duplicates, 0 blanks**, single column, header `Material: Material Name`.
- ~1,133 longer MPNs (e.g. `M393A2K43EB3-CWEB/C`, `LTM8053IY#PBF`, `BCN31ABL253G13`) — mostly resolvable in electronic-component catalogs.
- ~676 short alphanumeric codes (e.g. `04M3HJ`, `5B20N02284`, `93Y0050`) — almost all **OEM/FRU service part numbers** (Dell/Lenovo/IBM/HP) that will **not** be in DigiKey/Mouser/Nexar. These are the expected `ai_inferred` / `not_found` population.
- File is a mislabeled `text/html` table (ISO-8859-1), cleanly parseable as a single-column table.

## 2. Approved Decisions

| Decision | Choice |
|---|---|
| Coverage policy | **Max coverage.** Verified where possible; AI-inferred (flagged) for gaps; `not_found` for the rest. Nothing is dropped. |
| Spec depth | **Core attributes + verified descriptions** (no parametric facet sliders). Fields: description, manufacturer, category, lifecycle_status, package_type, rohs_status, pin_count, datasheet_url. Filtering via FTS + category/manufacturer/lifecycle + a verified-only toggle. |
| Build approach | **Reusable capability** (not a throwaway script): import path + authoritative enrichment service + flagged AI fallback. Run once for this list. |
| Source order | Cost-optimized: **DigiKey → Mouser → Element14 → OEMSecrets → Nexar (gaps only)**. All authoritative; Nexar last to limit paid credits while still capturing its unique coverage on hard parts. |
| Match guard | **Exact normalized-MPN match required** before accepting any source's data. Partial/fuzzy hits are rejected, never attached. (Primary defense against verified-but-wrong.) |
| AI fallback model | **Claude Opus 4.8** (`claude-opus-4-8`) — most capable for both knowledge and *calibrated refusal*. Used only for gap parts; produces description + category only; strict "return null if not confident". |
| Run safety | **Dry-run → coverage report → review → commit.** No DB writes until the report is reviewed. |

## 3. Data Model Changes (Alembic migration)

Two new columns on `material_cards` (model: `app/models/intelligence.py:26-69`):

```python
# First-class verification status. Indexed (drives the "Verified only" filter).
enrichment_status = Column(String(20), nullable=False, server_default="unenriched", index=True)
#   one of: "unenriched" | "verified" | "ai_inferred" | "not_found"

# Per-field provenance, auditable to source.
enrichment_provenance = Column(JSONB, nullable=True)
#   { "<field>": {"source": "digikey", "confidence": 1.0, "fetched_at": "2026-06-04T..Z",
#                 "matched_mpn": "..."}, ... }
#   fields tracked: description, manufacturer, category, lifecycle_status,
#                   package_type, rohs_status, pin_count, datasheet_url
```

Reused as-is: `normalized_mpn` (unique dedup key), `display_mpn`, `description`, `manufacturer`, `category`, `lifecycle_status`, `package_type`, `rohs_status`, `pin_count`, `datasheet_url`, `enrichment_source`, `enriched_at`, `search_vector`, `deleted_at`.

**`enrichment_source` semantics:** set to the highest-priority source that contributed any field (e.g. `"digikey"`), or `"claude_opus_inferred"` for AI-inferred rows. Detailed per-field attribution lives in `enrichment_provenance`.

**Migration:** Alembic autogenerate, reviewed, `upgrade head` → `downgrade -1` → `upgrade head` tested. `enrichment_status` backfills to `"unenriched"` for existing rows via `server_default`. Add index on `enrichment_status` (mirrors the `deleted_at` index pattern in `079_add_index_material_cards_deleted_at.py`).

## 4. Components

### 4.1 Connector extensions — capture core attributes
Today connectors return only `manufacturer`/`description`/`datasheet_url`. Extend the *parse* step of each to also surface the core attributes the API already returns. Scope is limited to core attributes — **not** full parametric specs.

- **`app/connectors/digikey.py`** (DigiKey Product Search v4): add `category` (from `Category`/`CategoryName`), `lifecycle_status` (from `ProductStatus` → mapped to availai's `active|nrfnd|eol|obsolete|ltb`), `package_type` (from `Parameters` "Package / Case"), `pin_count` (from `Parameters` "Number of Terminations"/"Number of Pins"), `rohs_status` (from `Classifications`/`RohsStatus`). DigiKey is the richest free source for these.
- **`app/connectors/sources.py` `NexarConnector`**: add `category` (`Part.category.name`), `lifecycle_status` (from specs/attributes if present), `package_type`, `pin_count` from `Part.specs`, `bestDatasheet.url`. Used only on gap parts.
- **`app/connectors/mouser.py`**, **`element14.py`**, **`oemsecrets.py`**: add `category` and `datasheet_url` where present; these mainly contribute description/manufacturer/datasheet for coverage.

Each connector's per-result dict gains optional keys: `category`, `lifecycle_status`, `package_type`, `pin_count`, `rohs_status` (in addition to existing `manufacturer`, `mpn_matched`/`mpn`, `description`, `datasheet_url`). All existing search behavior (sightings) is unchanged — these are additive keys.

A unit-level **lifecycle mapping table** lives next to the DigiKey connector (e.g. `{"Active":"active","Obsolete":"obsolete","Not For New Designs":"nrfnd","Last Time Buy":"ltb","End of Life":"eol"}`) and is the single source of truth for status normalization. Unknown statuses → leave `lifecycle_status` null (never guess).

### 4.2 Authoritative enrichment service (new)
**`app/services/authoritative_enrichment_service.py`**

```python
SOURCE_ORDER = ["digikey", "mouser", "element14", "oemsecrets", "nexar"]
CORE_FIELDS = ["description", "manufacturer", "category",
               "lifecycle_status", "package_type", "rohs_status",
               "pin_count", "datasheet_url"]

async def enrich_card_authoritative(card, db, *, refresh=False) -> EnrichmentResult
async def enrich_cards_authoritative(card_ids, db, *, concurrency=5, refresh=False) -> dict
```

Per card:
1. Skip if `enrichment_status == "verified"` and not `refresh`.
2. For each source in `SOURCE_ORDER`: call `connector.search(display_mpn)` (existing `BaseConnector.search`, which already provides retry / circuit-breaker / per-connector semaphore).
3. **Exact-match guard:** keep only results where `normalize_mpn_key(result["mpn_matched"]) == card.normalized_mpn`. Drop everything else.
4. **Merge — first non-null per field by source priority.** For each `CORE_FIELDS` field, take the first source (in order) that supplied a non-null value; record `{source, confidence, fetched_at, matched_mpn}` in `enrichment_provenance[field]`. Confidence = `1.0` for an exact-match authoritative hit.
5. Stop early once all fields are filled OR once the next source is `nexar` and the part already has description+manufacturer+category (avoids spending Nexar credits when the part is already adequately resolved).
6. If ≥1 field was filled from an authoritative source → `enrichment_status = "verified"`, `enrichment_source = <highest-priority contributor>`, `enriched_at = now`.
7. If **no** authoritative source returned an exact match → hand to the AI fallback (§4.3).

Concurrency is bounded (`concurrency=5` default) on top of the connectors' own semaphores; rate-limit/quota errors (`ConnectorRateLimitError`, `ConnectorQuotaError` from `app/connectors/errors.py`) are caught per-source and treated as "source unavailable for this MPN" (logged), not fatal.

### 4.3 AI-inference fallback (flagged) — new
**`app/services/ai_inference_fallback.py`** (a dedicated module, imported by the authoritative enrichment service).

- Model: **`claude-opus-4-8`**.
- Input: the MPN string only.
- Output (structured/tool-forced): `{ "description": str|null, "category": str|null, "confidence": float }`.
- **Strict prompt rule:** "If you are not confident this is a real, specific part, return null for description and category. Do NOT invent a plausible description. It is correct and expected to return null for unknown OEM/service part numbers."
- It produces **description + category only**. It must **never** populate lifecycle/package/pin/RoHS (guessing structured fields is the dangerous hallucination) — those stay null.
- Apply:
  - Non-null description → `enrichment_status = "ai_inferred"`, `enrichment_source = "claude_opus_inferred"`, provenance `{source:"claude_opus_inferred", confidence:<model conf>}`, `enriched_at = now`.
  - Null (model declined) → `enrichment_status = "not_found"`, MPN only, no description.

### 4.4 Import path — new
- **Parser:** extend `app/file_utils.py` to detect and parse the **HTML-table-as-`.xls`** format (sniff leading `<head>`/`<table>` / `text/html`), plus existing CSV/XLSX/TSV. Auto-detect a single-column MPN file (or a column literally named `Material: Material Name` / `mpn` / `part number`). Normalize each value via `normalize_mpn_key()`; drop blanks; dedup.
- **Endpoint:** `POST /api/materials/import-part-numbers` in `app/routers/materials.py` (buyer-gated via `require_buyer`, matching the existing `import-stock-list` endpoint at `app/routers/materials.py:389`). Body: uploaded file. Action: UPSERT bare `MaterialCard` (set `display_mpn`, `normalized_mpn`, `enrichment_status="unenriched"`) deduped on `normalized_mpn`; do **not** create `MaterialVendorHistory` (no vendor in this file — that's the key difference from the stock-list importer). Returns a summary (created / already-existing / skipped-blank). Enrichment is enqueued, not run inline.
- **Script:** `scripts/import_part_numbers.py` wrapping the same service for this one-time load and ops use. Flags: `--file PATH`, `--dry-run` (default), `--commit`, `--refresh`, `--report PATH`.
- **Idempotency:** re-import upserts by `normalized_mpn`; re-enrichment skips `verified` rows unless `--refresh`.

### 4.5 Filtering & UI
- **Service:** add an optional `verified_only: bool` (and/or `enrichment_status` filter) to `search_materials_faceted()` (`app/services/faceted_search_service.py:155`). When set, filter `MaterialCard.enrichment_status == "verified"`.
- **Materials list + detail templates** (`app/templates/partials/materials/…`, e.g. `workspace.html`): status badge per card — **"Verified · {source}"** (green), **"AI-inferred"** (amber, with tooltip "unverified — best-effort guess"), **"Not found"** (gray). Follow existing status-pill styling.
- **Filter bar:** a **"Verified only"** toggle (HTMX, posts to the existing filter endpoint). Default off (show everything); on → verified rows only. This is what lets a guessed value never silently pollute a filtered result set.
- Datasheet URL rendered as a link when present.

## 5. Data Flow

```
report1780605266325.xls
  └─ parse_part_number_file()  → [normalized MPNs]
       └─ UPSERT MaterialCard (enrichment_status="unenriched")
            └─ enrich_cards_authoritative()
                 ├─ for source in [digikey, mouser, element14, oemsecrets, nexar(gap)]:
                 │     connector.search(mpn) → exact-MPN-match filter → merge fields + provenance
                 ├─ any authoritative field?  → status="verified"
                 └─ none?                     → Opus fallback
                                                  ├─ description?  → status="ai_inferred"
                                                  └─ declined      → status="not_found"
  └─ (dry-run) coverage report CSV   ──review──▶  (commit) write to DB
                                                      └─ faceted search + UI badges + "Verified only"
```

## 6. Coverage Report (dry-run artifact)

`--dry-run` writes a CSV (default `reports/part_import_report_<UTC-timestamp>.csv`, gitignored; override with `--report PATH`) with one row per input MPN:

`input_mpn, normalized_mpn, status, source, manufacturer, category, lifecycle_status, package_type, pin_count, rohs_status, description, datasheet_url, notes`

Plus a summary footer: counts of `verified` / `ai_inferred` / `not_found`, and per-source contribution counts. **No DB writes occur in dry-run.** The user reviews this before `--commit`.

## 7. Error Handling

- Connector failures (network, 5xx) per source → logged, treated as "no result from this source", continue to next source. Never abort the run.
- `ConnectorRateLimitError` → respected via the connector's existing backoff; if persistent, that source is skipped for that MPN and noted in `notes`.
- `ConnectorQuotaError` (e.g. Nexar credits exhausted) → that source is disabled for the remainder of the run, logged loudly, surfaced in the report summary (so coverage isn't silently capped).
- Opus fallback error/timeout → row left `not_found` with a `notes` flag; never blocks other rows.
- Partial/ambiguous connector match (MPN mismatch) → discarded; counts toward "no authoritative hit". Logged at debug.
- All DB writes for `--commit` run in batched transactions; a failure rolls back the batch, not the whole run.

## 8. Testing (pytest, mocked external APIs)

- `parse_part_number_file`: HTML-`.xls` format, plain CSV, single-column, the `Material: Material Name` header, blanks/dupes.
- Exact-MPN-match guard: a connector returning a near-but-different MPN is rejected; an exact match is accepted.
- Merge/provenance: first-non-null-by-priority; provenance records correct source per field; `enrichment_status` transitions (`verified` / `ai_inferred` / `not_found`).
- Source-order short-circuit: Nexar is **not** called when cheaper sources already resolved description+manufacturer+category.
- DigiKey `ProductStatus` → lifecycle mapping (incl. unknown → null).
- Opus fallback: confident → `ai_inferred`; declined (null) → `not_found`; structured fields never populated by fallback.
- `verified_only` filter returns only `verified` rows.
- Quota/rate-limit handling: a quota error disables the source and is reported, run completes.
- Representative-MPN fixtures: a passive (`BCN31ABL253G13`), a memory module (`M393A2K43EB3-CWEB/C`), an IC/module (`LTM8053IY#PBF`), and an OEM FRU (`5B20N02284`).

## 9. Rollout

1. Branch `feat/verified-material-enrichment` (done).
2. Implement; run `pre-commit run --all-files`; full pytest suite green.
3. `python scripts/import_part_numbers.py --file "/root/Material Items/report1780605266325.xls" --dry-run` → review coverage report with the user.
4. On approval: `--commit` to the staging DB.
5. Deploy (`deploy.sh` with `--no-cache`) for the UI/filter changes; verify badges + "Verified only" filter render (check Tailwind classes built).
6. Update `docs/APP_MAP_DATABASE.md` (new columns), `docs/APP_MAP_ARCHITECTURE.md` (new service + connector extensions), `docs/APP_MAP_INTERACTIONS.md` (import endpoint + verified-only filter).
7. PR → review (PR-review agents) → merge.

## 10. Out of Scope (YAGNI)

- Full parametric spec facets / range sliders (capacitance, voltage, etc.).
- OEM-specific lookup integrations (Dell/Lenovo/HP parts portals) for the FRU codes — those remain `ai_inferred`/`not_found` for now.
- Scheduled/automatic re-enrichment cron (re-enrichment is on-demand via `--refresh` / the existing job can adopt the new path later).
- Replacing the existing Haiku enrichment everywhere — this adds the verified path and routes the import through it; broader migration of the legacy path is a follow-up.

## 11. Cost Note

DigiKey/Mouser/Element14/OEMSecrets are effectively free per-call (rate-limited). Nexar/Octopart consumes paid credits — bounded here because Nexar runs **only** on parts unresolved by the four free sources (expected to be a minority of the ~1,133 catalog MPNs plus a portion of the ~676 OEM codes, most of which Nexar also won't have). The dry-run report surfaces actual Nexar call count before any credits are spent on a committed run.
