# `cpu`-bucket pollution re-classifier — design

**Date:** 2026-06-16
**Status:** approved (brainstorm) — pending written-spec review
**Author:** Claude (with mkhoury)

## Problem

`category='cpu'` is TRIO's SFDC catch-all: **37,389 cards = 69.7% of all `trio_source` categorizations**, but a live audit shows **~67% (~25k) are not CPUs at all** — TE Connectivity connectors, EPCOS/TDK/Murata/AVX passives, Samtec connectors, TI/NXP logic ICs, capacitors. **96.6% have NULL manufacturer.** The `cpu` label here is an un-coded default, not a real classification — yet it sits at `trio_source` tier 95, blocking every downstream enricher from correcting it.

## Goal & scope

A **deterministic MPN-prefix classifier** + a **one-shot bulk CLI** that re-categorizes the *recognizable-vendor* pollution in the `cpu` bucket to the correct commodity. Operates **only** on `category='cpu'` cards. **Precision over coverage** — only re-classify on a prefix that *definitively* names a non-CPU commodity.

- **In scope:** the recognizable-vendor pollution (~6–8k cards) → re-homed to valid TRIO commodities that already exist in the vocab (`connectors`, `capacitors`, `resistors`, `inductors`, `logic_ic`, `power_ic`, `MOSFETs`, `relays`, `switches`, `cables`, `optoelectronics`, …).
- **Out of scope (stated):** the ~19k bare/unidentifiable PNs (no description, no vendor signal) stay in `cpu`; **no vendor-lookup** reach (Approach 2/3, deferred); **only the `cpu` bucket** is touched — no other category.

## Resolved decisions

1. **Action = re-categorize** (not un-categorize / flag).
2. **Override = a dedicated `cpu_pollution_fix` source at SOURCE_TIER 96** (just above `trio_source` 95, below `manual` 100), written through the normal `set_category` ladder. The CLI only ever invokes it on `category='cpu'` cards with a definitive prefix match, so it never overrides a genuine `trio_source` category anywhere else. Auditable (`category_source='cpu_pollution_fix'`), ladder-compatible, reversible.

## Design

### A. `MANUFACTURER_PREFIX_MAP` (`app/services/cpu_pollution/prefix_map.py`)
High-precision MPN prefix/regex → **valid commodity key**. Seeded from the grounding's telltale polluters (TE Connectivity → `connectors`; EPCOS/TDK `B`-series → `capacitors`/`inductors`; Murata `GRM` → `capacitors`; Samtec → `connectors`; AVX → `capacitors`; TI/NXP logic prefixes → `logic_ic`; …).

**Map-construction method (build-time, in the plan — not guessed):**
1. Query the live `cpu` bucket for the highest-volume MPN prefixes (e.g. group by first 2–4 chars / the manufacturer-prefix regex families), get counts.
2. For each high-volume prefix, VERIFY it definitively maps to one non-CPU commodity (spot-check sample MPNs against the real part; reject any prefix that also matches real Intel/AMD CPUs or is ambiguous).
3. Map each verified prefix to a commodity key that **exists in `commodity_registry`** (normalized via `category_normalizer`); drop any whose correct commodity has no valid TRIO home.
Result: a curated, comment-justified map (every entry cites its evidence), precision-first.

### B. `classify_polluted_mpn(mpn) -> str | None` (`app/services/cpu_pollution/classifier.py`)
Pure function. Returns the canonical commodity key on a definitive prefix match (validated against `commodity_registry` + normalized), else `None`. MUST return `None` for any real Intel/AMD CPU identifier (sSpec `S[RL]…`, ordering codes `CM8/BX8/CD8/AT8`, model strings `E5-/Gold/i7-`, AMD OPN) — guarded explicitly.

### C. Ladder registration
- `spec_tiers.SOURCE_TIER` += `"cpu_pollution_fix": 96`.
- `alembic/versions/096_spec_provenance.py` `_SOURCE_TIER_SQL_CASE` += the `cpu_pollution_fix → 96` arm (keeps the migration-096 key-for-key sync test green; **no live-DB effect / no new migration** — runtime tier is Python `tier_for()`, same precedent as `partsurfer_desc`/`connector_desc`).

### D. Bulk CLI `app/management/fix_cpu_pollution.py`
`python -m app.management.fix_cpu_pollution [--apply] [--limit N]`.
- Scans `material_cards WHERE category='cpu' AND deleted_at IS NULL`, batched.
- For each: `commodity = classify_polluted_mpn(card.display_mpn)`; if not None → `set_category(card, commodity, source="cpu_pollution_fix", confidence=0.97)` inside a per-card SAVEPOINT.
- **Dry-run by default** (reports `{scanned, would_reclassify, by_commodity}`); `--apply` commits. Idempotent (re-running re-classifies nothing new — already-moved cards are no longer `category='cpu'`).
- Loguru summary line.

## Error handling
- Per-card `begin_nested()` SAVEPOINT (mirrors the desc/connector writers); a bad card rolls back only itself and is logged, never aborts the run.
- `set_category` ladder arbitrates: `cpu_pollution_fix` (96) beats the `trio_source` (95) `cpu` default but still loses to `manual` (100); off-vocab commodity → `None` (rejected) — so a mis-mapped key can't persist junk.
- Classifier returns `None` on any ambiguity → no change.

## Testing (`tests/test_cpu_pollution_reclassify.py`)
- **Classifier:** each seeded prefix → its commodity; a real Intel sSpec / ordering code / model string / AMD OPN → `None`; an unrecognized bare PN → `None`; output keys are valid `commodity_registry` vocab.
- **Override:** a `category='cpu', category_source='trio_source'` card with a TE prefix → re-categorized to `connectors` at `category_source='cpu_pollution_fix'`, `category_tier=96`; a real CPU card in the bucket → untouched; a `category='dram'` (non-cpu) card → untouched even if its MPN matches a prefix (CLI scopes to `cpu`).
- **CLI:** dry-run changes nothing + reports counts; `--apply` re-categorizes; idempotent on re-run.
- **Migration-096 sync** test passes with the new arm.

## Rollback
The CLI is reversible: `category_source='cpu_pollution_fix'` is a distinct, queryable provenance, so a one-line revert (`UPDATE … SET category='cpu', category_source='trio_source' WHERE category_source='cpu_pollution_fix'`) or a `--revert` CLI flag can undo it. No schema migration to downgrade.

## Files
- `app/services/cpu_pollution/__init__.py`, `prefix_map.py`, `classifier.py` (new)
- `app/services/spec_tiers.py` (+ `cpu_pollution_fix: 96`)
- `alembic/versions/096_spec_provenance.py` (+ CASE arm)
- `app/management/fix_cpu_pollution.py` (new CLI)
- `tests/test_cpu_pollution_reclassify.py` (new)
- `docs/APP_MAP_INTERACTIONS.md` (+ the re-classifier + `cpu_pollution_fix` tier)

## Follow-ups (not this sub-project)
- The ~19k bare/unidentifiable `cpu` cards (need vendor-lookup or stay).
- Whether other (non-`cpu`) `trio_source` categories carry pollution (the audit says `cpu` is the catch-all; others looked sane — revisit only if evidence emerges).
- A broader `category_normalizer` pass for casing variants (`Capacitors` vs `capacitors`) seen in the live vocab.
