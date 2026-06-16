# CPU-bucket Pollution Re-classifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-categorize the recognizable-vendor pollution in the `category='cpu'` catch-all bucket to the correct commodity, via a deterministic, precision-first MPN-prefix classifier + a bulk CLI.

**Architecture:** A pure prefix classifier (`classify_polluted_mpn`) maps definitive non-CPU manufacturer prefixes to valid TRIO commodity keys, guarding against real Intel/AMD CPU identifiers. A one-shot CLI scans `category='cpu'` cards and re-categorizes matches via `set_category(source="cpu_pollution_fix")` at a new tier 96 (above the `trio_source` default 95). `cpu`-bucket-only; precision over coverage; no new migration.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0, pytest (SQLite in tests). Reuses `spec_tiers.set_category`, `commodity_registry.CANONICAL_COMMODITY_KEYS`.

**Spec:** `docs/superpowers/specs/2026-06-16-cpu-pollution-reclassify-design.md`

---

### Task 1: Register `cpu_pollution_fix` source (tier 96) + migration CASE sync

**Files:**
- Modify: `app/services/spec_tiers.py` (the `SOURCE_TIER` dict, near `"manual": 100` / `"trio_source": 95`)
- Modify: `alembic/versions/096_spec_provenance.py` (the `_SOURCE_TIER_SQL_CASE`, add an arm above `'manual'`/near the top tiers)
- Test: `tests/test_cpu_pollution_reclassify.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_cpu_pollution_reclassify.py`:
```python
"""tests/test_cpu_pollution_reclassify.py — re-classify the recognizable pollution in the
`cpu` catch-all bucket to the correct commodity via deterministic MPN prefixes.

Depends on: conftest.py (db_session), seed_commodity_schemas, MaterialCard,
spec_tiers.SOURCE_TIER (cpu_pollution_fix=96), commodity_registry.CANONICAL_COMMODITY_KEYS.
"""

from app.services.spec_tiers import SOURCE_TIER, tier_for


def test_cpu_pollution_fix_registered_at_tier_96():
    assert SOURCE_TIER["cpu_pollution_fix"] == 96
    assert tier_for("cpu_pollution_fix") == 96
    # Beats the trio_source 'cpu' default (95), loses to manual (100).
    assert SOURCE_TIER["cpu_pollution_fix"] > SOURCE_TIER["trio_source"]
    assert SOURCE_TIER["cpu_pollution_fix"] < SOURCE_TIER["manual"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_cpu_pollution_reclassify.py::test_cpu_pollution_fix_registered_at_tier_96 -v --override-ini="addopts="`
Expected: FAIL with `KeyError: 'cpu_pollution_fix'`.

- [ ] **Step 3: Register the source**

In `app/services/spec_tiers.py`, add to `SOURCE_TIER` immediately below `"manual": 100,` and above `"trio_source": 95,`:
```python
    "cpu_pollution_fix": 96,  # deterministic re-classification of the polluted `cpu` catch-all:
    # beats the trio_source 'cpu' DEFAULT (95, an un-coded SFDC dump), below manual (100).
    # Only ever written by app/management/fix_cpu_pollution.py on category='cpu' cards.
```

- [ ] **Step 4: Sync the migration-096 CASE snapshot**

In `alembic/versions/096_spec_provenance.py`, in `_SOURCE_TIER_SQL_CASE`, add immediately after the `"WHEN 'manual' THEN 100 "` line:
```python
    "WHEN 'cpu_pollution_fix' THEN 96 "
```
(Keeps `test_migration_096_spec_provenance.py`'s key-for-key assertion green. No live-DB effect / no new migration — runtime tier is Python `tier_for()`, same precedent as `partsurfer_desc`/`connector_desc`.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_cpu_pollution_reclassify.py::test_cpu_pollution_fix_registered_at_tier_96 tests/test_migration_096_spec_provenance.py -v --override-ini="addopts="`
Expected: PASS (both).

- [ ] **Step 6: Commit**

```bash
git add app/services/spec_tiers.py alembic/versions/096_spec_provenance.py tests/test_cpu_pollution_reclassify.py
git commit -m "feat(cpu-pollution): register cpu_pollution_fix source at tier 96"
```

---

### Task 2: Prefix map + classifier

**Files:**
- Create: `app/services/cpu_pollution/__init__.py` (empty package marker)
- Create: `app/services/cpu_pollution/prefix_map.py`
- Create: `app/services/cpu_pollution/classifier.py`
- Test: `tests/test_cpu_pollution_reclassify.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cpu_pollution_reclassify.py`:
```python
import pytest

from app.services.commodity_registry import CANONICAL_COMMODITY_KEYS
from app.services.cpu_pollution.classifier import classify_polluted_mpn
from app.services.cpu_pollution.prefix_map import PREFIX_RULES


def test_every_prefix_rule_targets_valid_vocab():
    for _pattern, commodity in PREFIX_RULES:
        assert commodity in CANONICAL_COMMODITY_KEYS, f"{commodity} not a canonical commodity"


@pytest.mark.parametrize(
    "mpn,expected",
    [
        ("5-1437720-3", "connectors"),       # TE Connectivity
        ("1437259-6", "connectors"),         # TE Connectivity
        ("SSW-114-22-S-S-VS-P-TR", "connectors"),  # Samtec
        ("CLT-110-02-G-D-BE-A-K-TR", "connectors"),# Samtec
        ("NRWA330M63V6.3X11TBF", "capacitors"),    # Nichicon
        ("TAJD475K050RNJ", "capacitors"),    # AVX tantalum
        ("B32520C474K189", "capacitors"),    # EPCOS film cap
        ("BLM21AJ601SN1D", "inductors"),     # Murata ferrite bead
        ("CRCW12102K21FKEA", "resistors"),   # Vishay
        ("CD74HC123EE4", "logic_ic"),        # TI CD74 logic
        ("74AUP2G14DW-7", "logic_ic"),       # 74-series logic
        ("BCM5488SA7IPBG", "logic_ic"),      # Broadcom
    ],
)
def test_known_pollution_classifies(mpn, expected):
    assert classify_polluted_mpn(mpn) == expected


@pytest.mark.parametrize(
    "cpu_mpn",
    [
        "SR3QS", "SL5CH",            # Intel sSpec
        "CM8068403654318",          # Intel ordering code
        "BX8070110700K",            # Intel boxed
        "CD8069504194701",          # Intel ordering — must NOT collide with CD74 logic
        "E5-2680V4",                # Intel model string
        "100-000000053",           # AMD OPN
        "EPYC 7742",                # AMD model word
    ],
)
def test_real_cpu_is_never_reclassified(cpu_mpn):
    assert classify_polluted_mpn(cpu_mpn) is None


def test_unrecognized_and_empty_return_none():
    assert classify_polluted_mpn("ZZQW9981XYZ") is None
    assert classify_polluted_mpn("") is None
    assert classify_polluted_mpn(None) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_cpu_pollution_reclassify.py -k "prefix_rule or classifies or never_reclassified or unrecognized" -v --override-ini="addopts="`
Expected: FAIL with `ModuleNotFoundError: app.services.cpu_pollution`.

- [ ] **Step 3: Create the package + prefix map**

Create `app/services/cpu_pollution/__init__.py`:
```python
"""Deterministic re-classification of the polluted `cpu` catch-all bucket.

What: prefix-based classifier (classifier.py) + rule table (prefix_map.py) that map a
    definitively-non-CPU manufacturer MPN to its correct commodity, guarding real Intel/AMD
    CPUs. Applied by app/management/fix_cpu_pollution.py at source="cpu_pollution_fix".
Depends on: app.services.commodity_registry (vocab), stdlib re.
"""
```

Create `app/services/cpu_pollution/prefix_map.py`:
```python
"""Verified MPN-prefix → commodity rules + the real-CPU guard for the cpu-bucket cleanup.

Each PREFIX_RULES entry was verified against live `category='cpu'` sample MPNs; the commodity
is a canonical key (asserted by test_every_prefix_rule_targets_valid_vocab). PRECISION FIRST:
anchored sub-prefixes only (never a bare ambiguous letter), and CPU_GUARD is checked BEFORE
the rules so a real Intel/AMD identifier is never re-homed (e.g. Intel CD80… must not hit the
TI CD74 logic rule). Called by: classifier.py.
"""

from __future__ import annotations

import re

# (anchored regex on the UPPERCASED MPN, canonical commodity key). First match wins.
PREFIX_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^[0-9]-?[0-9]{6}-[0-9]"), "connectors"),               # TE Connectivity
    (re.compile(r"^(SSW|CLT|CLP|SMM|SSM|SLW|TSW|HLE|FLE|BSW)-"), "connectors"),  # Samtec series
    (re.compile(r"^NRWA"), "capacitors"),                               # Nichicon Al electrolytic
    (re.compile(r"^TAJ"), "capacitors"),                                # AVX/Kyocera tantalum
    (re.compile(r"^B32"), "capacitors"),                                # EPCOS/TDK film cap
    (re.compile(r"^BLM"), "inductors"),                                 # Murata ferrite bead
    (re.compile(r"^CRCW"), "resistors"),                                # Vishay thick-film resistor
    (re.compile(r"^CD74"), "logic_ic"),                                 # TI CD74HC logic
    (re.compile(r"^(SN74|74[A-Z])"), "logic_ic"),                       # 74-series logic
    (re.compile(r"^BCM[0-9]"), "logic_ic"),                             # Broadcom
]

# MPNs that ARE real CPUs — never reclassify (defense-in-depth, checked before PREFIX_RULES).
CPU_GUARD: list[re.Pattern[str]] = [
    re.compile(r"^S[RL][0-9A-Z]{2,4}$"),                                # Intel sSpec (SR3QS, SL5CH)
    re.compile(r"^(BX80|CM80|CD80|AT80|FC80|FH80|HH80|CW80)"),          # Intel ordering codes
    re.compile(
        r"(XEON|CORE\s?I[3579]|PENTIUM|CELERON|"
        r"GOLD\s?[0-9]|SILVER\s?[0-9]|PLATINUM\s?[0-9]|BRONZE\s?[0-9]|^E[357]-[0-9])"
    ),                                                                  # Intel model strings
    re.compile(r"(EPYC|RYZEN|OPTERON|ATHLON|THREADRIPPER)"),            # AMD model words
    re.compile(r"^10[0-9]-[0-9]{9}$"),                                  # AMD OPN (100-000000053)
]
```

- [ ] **Step 4: Create the classifier**

Create `app/services/cpu_pollution/classifier.py`:
```python
"""Pure prefix classifier for the cpu-bucket cleanup.

What: classify_polluted_mpn(mpn) -> canonical commodity key | None. Precision-first — returns
    None for any real Intel/AMD CPU identifier and for any MPN without a definitive non-CPU
    manufacturer prefix. Called by: app/management/fix_cpu_pollution.py.
Depends on: prefix_map.PREFIX_RULES + CPU_GUARD.
"""

from __future__ import annotations

from app.services.cpu_pollution.prefix_map import CPU_GUARD, PREFIX_RULES


def classify_polluted_mpn(mpn: str | None) -> str | None:
    """Return the correct commodity for a definitively-non-CPU `cpu`-bucket MPN, else None."""
    if not mpn:
        return None
    s = mpn.strip().upper()
    if not s:
        return None
    for guard in CPU_GUARD:
        if guard.search(s):
            return None
    for pattern, commodity in PREFIX_RULES:
        if pattern.search(s):
            return commodity
    return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_cpu_pollution_reclassify.py -k "prefix_rule or classifies or never_reclassified or unrecognized" -v --override-ini="addopts="`
Expected: PASS (all parametrized cases — note `CD8069…` → None via guard while `CD74HC…` → logic_ic).

- [ ] **Step 6: Commit**

```bash
git add app/services/cpu_pollution/ tests/test_cpu_pollution_reclassify.py
git commit -m "feat(cpu-pollution): prefix map + precision-first classifier"
```

---

### Task 3: Bulk re-classification CLI

**Files:**
- Create: `app/management/fix_cpu_pollution.py`
- Test: `tests/test_cpu_pollution_reclassify.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cpu_pollution_reclassify.py`:
```python
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas


def _cpu_card(db: Session, mpn: str) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category="cpu")
    card.category_source = "trio_source"
    card.category_tier = 95
    db.add(card)
    db.flush()
    return card


def test_cli_dry_run_changes_nothing_but_reports(db_session: Session):
    from app.management.fix_cpu_pollution import reclassify_cpu_pollution

    seed_commodity_schemas(db_session)
    te = _cpu_card(db_session, "5-1437720-3")
    db_session.commit()
    stats = reclassify_cpu_pollution(db_session, apply=False)
    db_session.refresh(te)
    assert stats["reclassified"] == 1
    assert stats["by_commodity"] == {"connectors": 1}
    assert te.category == "cpu"  # dry-run: unchanged


def test_cli_apply_reclassifies_pollution_only(db_session: Session):
    from app.management.fix_cpu_pollution import reclassify_cpu_pollution

    seed_commodity_schemas(db_session)
    te = _cpu_card(db_session, "5-1437720-3")     # TE connector
    cpu = _cpu_card(db_session, "SR3QS")          # real Intel CPU
    dram = MaterialCard(normalized_mpn="d1", display_mpn="5-9999999-9", category="dram")
    dram.category_source = "trio_source"; dram.category_tier = 95
    db_session.add(dram); db_session.flush()
    db_session.commit()

    stats = reclassify_cpu_pollution(db_session, apply=True)
    for c in (te, cpu, dram):
        db_session.refresh(c)
    assert te.category == "connectors"
    assert te.category_source == "cpu_pollution_fix"
    assert te.category_tier == 96
    assert cpu.category == "cpu"     # real CPU untouched
    assert dram.category == "dram"   # non-cpu bucket untouched (CLI scopes to category='cpu')
    assert stats["reclassified"] == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_cpu_pollution_reclassify.py -k "cli_" -v --override-ini="addopts="`
Expected: FAIL with `ModuleNotFoundError: app.management.fix_cpu_pollution`.

- [ ] **Step 3: Create the CLI**

Create `app/management/fix_cpu_pollution.py`:
```python
"""One-shot bulk re-classifier for the polluted `cpu` catch-all bucket.

What: scans material_cards WHERE category='cpu', and for each MPN that
    classify_polluted_mpn definitively maps to a non-CPU commodity, re-categorizes it via
    set_category(source="cpu_pollution_fix") (tier 96 — beats the trio_source 'cpu' default).
    Dry-run by default; --apply commits. Reversible (the cpu_pollution_fix provenance is
    queryable). Scopes ONLY to category='cpu' — no other bucket is touched.
Called by: operators (python -m app.management.fix_cpu_pollution [--apply] [--limit N]).
Depends on: cpu_pollution.classifier, spec_tiers.set_category.
"""

from __future__ import annotations

import argparse

from loguru import logger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import MaterialCard
from app.services.cpu_pollution.classifier import classify_polluted_mpn
from app.services.spec_tiers import set_category

_SOURCE = "cpu_pollution_fix"
_CONFIDENCE = 0.97


def reclassify_cpu_pollution(db: Session, *, apply: bool, limit: int | None = None) -> dict:
    """Re-classify definitively-non-CPU cards out of the `cpu` bucket. Returns summary."""
    q = db.query(MaterialCard).filter(
        MaterialCard.category == "cpu", MaterialCard.deleted_at.is_(None)
    )
    if limit:
        q = q.limit(limit)
    scanned = reclassified = 0
    by_commodity: dict[str, int] = {}
    for card in q.yield_per(500):
        scanned += 1
        commodity = classify_polluted_mpn(card.display_mpn)
        if not commodity:
            continue
        if apply:
            try:
                with db.begin_nested():
                    set_category(card, commodity, _SOURCE, _CONFIDENCE)
            except Exception:
                logger.exception("cpu-pollution: failed on card_id={}", card.id)
                continue
        reclassified += 1
        by_commodity[commodity] = by_commodity.get(commodity, 0) + 1
    if apply:
        db.commit()
    logger.info(
        "cpu-pollution: scanned={} reclassified={} by_commodity={} (apply={})",
        scanned, reclassified, by_commodity, apply,
    )
    return {"scanned": scanned, "reclassified": reclassified, "by_commodity": by_commodity}


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-classify pollution out of the cpu bucket.")
    ap.add_argument("--apply", action="store_true", help="commit changes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap cards scanned")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        reclassify_cpu_pollution(db, apply=args.apply, limit=args.limit)
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_cpu_pollution_reclassify.py -v --override-ini="addopts="`
Expected: PASS (the whole file).

- [ ] **Step 5: Commit**

```bash
git add app/management/fix_cpu_pollution.py tests/test_cpu_pollution_reclassify.py
git commit -m "feat(cpu-pollution): bulk re-classification CLI (dry-run default)"
```

---

### Task 4: APP_MAP + final verification

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md`

- [ ] **Step 1: Update the APP_MAP**

In `docs/APP_MAP_INTERACTIONS.md`, in the enrichment-writers / evidence-source-tier section, add `cpu_pollution_fix` (tier 96 — deterministic re-classification of the polluted `cpu` catch-all, beats `trio_source` 95) and note the `app/management/fix_cpu_pollution.py` CLI + the `app/services/cpu_pollution/` classifier.

- [ ] **Step 2: Lint + type + format**

Run: `ruff check app/services/cpu_pollution/ app/management/fix_cpu_pollution.py && pre-commit run --files app/services/spec_tiers.py app/services/cpu_pollution/__init__.py app/services/cpu_pollution/prefix_map.py app/services/cpu_pollution/classifier.py app/management/fix_cpu_pollution.py alembic/versions/096_spec_provenance.py tests/test_cpu_pollution_reclassify.py docs/APP_MAP_INTERACTIONS.md`
Run pre-commit a SECOND time (docformatter mutates then verifies).
Expected: all hooks pass on the second run.

- [ ] **Step 3: Full targeted suite**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_cpu_pollution_reclassify.py tests/test_migration_096_spec_provenance.py tests/test_spec_tiers.py -q --override-ini="addopts="`
Expected: PASS, zero failures.

- [ ] **Step 4: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md
git commit -m "docs(app-map): cpu_pollution_fix re-classifier + tier 96"
```

---

## Follow-ups (out of scope, noted)
- Extend `PREFIX_RULES` by mining more high-volume `cpu`-bucket prefixes (verify each; guard CPUs) — precision-first growth.
- The ~19k bare/unidentifiable `cpu` cards (need vendor-lookup or stay).
- A `--revert` flag / one-line `UPDATE` to undo (the `cpu_pollution_fix` provenance is queryable).
- A live dry-run before `--apply` on production to confirm the by-commodity breakdown.
