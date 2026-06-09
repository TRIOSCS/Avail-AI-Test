# MPN→Spec Enrichment — Phase 1: Deterministic MPN Decoders

**Date:** 2026-06-08
**Branch:** `feat/mpn-decode-enrichment` (off main)
**Status:** Awaiting user review

The first, highest-ROI phase of the MPN→datasheet enrichment track (research: the 2026-06-08
workflow). Closes the "input poverty" gap for **Storage + Memory** — likely the majority of
real inventory by line count — by **deterministically decoding genuine manufacturer part
numbers** into the deep filter facets. Zero LLM, zero network, zero hallucination.

## 1. Goal
For a card whose MPN is a standard manufacturer drive/DIMM part number, read its specs
straight out of the part-number string and write them through the existing `record_spec`
path, populating the already-seeded facet keys:
- **HDD** → `capacity_gb`, `interface`, `form_factor`, `rpm`, `usage_class`
- **SSD** → `capacity_gb`, `form_factor`, `interface`, `nand_type`
- **DRAM** → `ddr_type`, `capacity_gb`, `speed_mhz`, `ecc`, `form_factor`

## 2. Non-goals (later phases, NOT this spec)
- OEM/FRU resolution (HP PartSurfer FRU→canonical MPN) = **Phase 2**.
- Datasheet → Claude-PDF extraction (SSD endurance, component specs) = **Phase 3**.
- Broker/VAR catalogs (Dell/Lenovo loose FRUs) = **Phase 4**.
- Any spec NOT encoded in the MPN, and any MPN not matching a known vendor scheme — left
  untouched (never guessed).

## 3. Architecture

### New pure module `app/services/mpn_decoder/` (no I/O, no LLM, no network)
- `_common.py` — `DecodeResult` dataclass `{commodity: str, specs: dict[str, str|int|float], vendor: str, confidence: float}`; shared helpers (capacity-token → GB, etc.).
- `storage.py` — per-vendor HDD/SSD decoders: **Seagate** (`ST…`), **Western Digital** (`WD…`), **Toshiba** (`MG/MQ/DT/MD/KXG/…`), **HGST/Hitachi** (`HUS/HUH/HTS/HDS/HDN…`). Each: a strict regex GATE that matches only that vendor's documented scheme, then positional/lookup decode.
- `memory.py` — per-vendor DRAM-module decoders: **Samsung** (`M3…/M4…`), **SK Hynix** (`HMA/HMT/HMC…`), **Micron** (`MT…`), **Kingston** (`KVR/KSM/KCP/KTH/KTD…`), **Crucial** (`CT…`).
- `__init__.py` — `decode_mpn(mpn: str, manufacturer: str | None = None) -> DecodeResult | None`: normalizes the MPN, tries each vendor gate (manufacturer hint narrows first), returns the first match or `None`.

**Decoders emit only values that map to the seeded `enum_values` / numeric keys** (e.g. `interface ∈ {SATA, SAS, SCSI, NVMe, …}`, `form_factor ∈ {3.5", 2.5", …}`, `ddr_type ∈ {DDR3, DDR4, …}`). `record_spec` independently enum-validates, so an out-of-vocabulary decode is rejected (defense-in-depth).

### Integration — worker second pass
- New producer `decode_and_record(db, card, schema_cache=None) -> int` in a thin adapter (e.g. `mpn_decoder/writer.py` or in `spec_enrichment_service`). Calls `decode_mpn(card.display_mpn, card.manufacturer)`; for each decoded spec, `record_spec(db, card.id, spec_key, value, source="mpn_decode", confidence=0.95)`. Returns count written.
- Invoked in the **worker's second pass** (`app/services/enrichment_worker/worker.py`, over `enriched_ids`, alongside `enrich_card_specs`, on the shared post-await session) — NOT inside `enrich_card` (respects the documented no-query-after-await concurrency invariant). Run BEFORE `enrich_card_specs` so the deterministic value is the higher-confidence baseline.
- **Config flag** `settings.MPN_DECODE_ENABLED` (default **True**) — lets ops disable without a deploy.

### Confidence / conflict
- `source="mpn_decode"`, `confidence=0.95`. Per `record_spec`'s existing rules this **overwrites** description-mined `spec_extraction` (0.85) but **never** a protected vendor-API value (`digikey_api`/`mouser_api`). Deterministic decode is strictly better than an AI description guess.

## 4. Safety / accuracy (a wrong facet value is worse than a missing one)
1. **Per-vendor regex gates** — only recognized schemes decode; anything else → `None`, untouched.
2. **`record_spec` enum/numeric validation** — a decode that isn't an exact seeded enum member is dropped.
3. **`scripts/decode_mpn_dryrun.py`** — runs the decoders read-only over `material_cards`, prints per-vendor match counts + sample decoded specs, writes NOTHING. Ops spot-checks accuracy before trusting (or after enabling).
4. **Unit tests are the accuracy guard** — each vendor: a table of real known MPN → expected specs; plus a non-matching MPN → `None` (no false write).

## 5. Data model
- **None.** Reuses `record_spec`, the seeded `commodity_spec_schemas`, and `material_spec_facets`. No migration. (Optional: a `mpn_decode_today` worker counter, deferred.)

## 6. Reality check
The DB is currently thin (SFDC import has no near-term date — see project memory), so this mostly lands value at scale. It is infrastructure: every standard drive/DIMM MPN that arrives gets its facets for free, forever, with no per-lookup cost. OEM spare-part numbers (HP/Lenovo/Dell FRUs) do NOT match the gates and stay unresolved until Phases 2–4.

## 7. Build sequence (units)
1. `mpn_decoder` module + `DecodeResult` + **storage** decoders (Seagate/WD/Toshiba/HGST) + per-vendor known-MPN tests.
2. **Memory** decoders (Samsung/Hynix/Micron/Kingston/Crucial) + tests.
3. Worker second-pass integration + `MPN_DECODE_ENABLED` flag + integration test (decode → facet written via record_spec; non-match → nothing).
4. `scripts/decode_mpn_dryrun.py`.
5. Docs (`APP_MAP_DATABASE.md` / `APP_MAP_INTERACTIONS.md`) + `pre-commit --all-files`.

## 8. Open items to confirm
1. **Flag default** — ON (regex-gated + enum-validated + tested; dry-run for spot-check) vs OFF-until-validated. (Rec: **ON**.)
2. **Vendor scope** — the 9 vendors above. Add/drop any? (Rec: ship these; they dominate the drive/DIMM market.)
3. **Confidence 0.95 + overwrite `spec_extraction`** — OK? (Rec: yes; deterministic > description guess; vendor-API stays protected.)
