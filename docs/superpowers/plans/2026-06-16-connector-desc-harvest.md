# Connector-description Harvest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop discarding the structured fields + description the connector pipeline already fetches, and harvest them into the F1 spec ladder (coverage + facet depth).

**Architecture:** In the enrichment path (`enrich_material_card` → `_apply_enrichment_to_card`), after the existing manufacturer/category apply, run a harvest step that (1) feeds the connector `description` through the existing `categorize_and_record` at a new `connector_desc` source (tier 84 — categorizes uncategorized server-commodity cards + fills facets), (2) records the structured fields (`package`/`pin_count`/`rohs`) at the connector's own vendor-API tier (90) for component-commodity cards, and (3) stores `datasheet_url` on the card. All writes go through the ladder; v1 = enrichment path only (search-path harvest is a follow-up). Flag-gated, no migration.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0, pytest (SQLite in tests). Reuses `spec_tiers`, `spec_write_service.record_spec`, `desc_extractor.writer.categorize_and_record`.

**Spec:** `docs/superpowers/specs/2026-06-16-connector-description-harvest-design.md`

**Honest scope note:** `commodity_seeds.json` confirms `package`/`pin_count`/`rohs` are **component-commodity** facets (not in `dram`/`ssd`/`cpu`/…). So the structured-field write helps DigiKey/Mouser **component** cards; the **description** harvest is what serves the dominant uncategorized **server-spare** cohort. `record_spec` schema-gates structured fields, so they safely no-op where the commodity lacks the key.

---

### Task 1: Register `connector_desc` (tier 84 + constants + flag + migration CASE arm)

**Files:**
- Modify: `app/services/spec_tiers.py` (the `SOURCE_TIER` dict, near `"partsurfer_desc": 84`)
- Modify: `app/services/desc_extractor/_common.py` (after the `PARTSURFER_DESC_*` constants)
- Modify: `app/config.py` (near `partsurfer_desc_enabled`, ~line 138)
- Modify: `alembic/versions/096_spec_provenance.py` (the `_SOURCE_TIER_SQL_CASE`, after the `partsurfer_desc` arm ~line 67)
- Test: `tests/test_connector_desc_harvest.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_connector_desc_harvest.py`:
```python
"""tests/test_connector_desc_harvest.py — harvest the structured fields + description the
connector pipeline already fetches (previously discarded), into the F1 ladder.

Depends on: conftest.py (db_session), seed_commodity_schemas, MaterialCard +
MaterialSpecFacet, spec_tiers.SOURCE_TIER (connector_desc=84).
"""

from app.services.spec_tiers import SOURCE_TIER, tier_for


def test_connector_desc_registered_at_tier_84():
    assert SOURCE_TIER["connector_desc"] == 84
    assert tier_for("connector_desc") == 84
    # Above the card's own desc_parse (83), below the deterministic decoders (85).
    assert SOURCE_TIER["connector_desc"] > SOURCE_TIER["desc_parse"]
    assert SOURCE_TIER["connector_desc"] < SOURCE_TIER["mpn_decode"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_connector_desc_harvest.py::test_connector_desc_registered_at_tier_84 -v --override-ini="addopts="`
Expected: FAIL with `KeyError: 'connector_desc'`.

- [ ] **Step 3: Register the source in the ladder**

In `app/services/spec_tiers.py`, add to `SOURCE_TIER` immediately above `"partsurfer_desc": 84,`:
```python
    "connector_desc": 84,  # distributor (DigiKey/Mouser/…) description parsed by the desc grammar:
    # more authoritative than the card's own desc_parse (83), below the deterministic decoders (85).
```

- [ ] **Step 4: Add the constants**

In `app/services/desc_extractor/_common.py`, after the `PARTSURFER_DESC_CONFIDENCE = 0.90` line:
```python
# Distributor-connector description: the same desc grammar run over a DigiKey/Mouser/
# element14/OEMSecrets/Nexar product description we ALREADY fetch (not the card's own).
# Outranks desc_parse (spec_tiers.SOURCE_TIER: connector_desc=84 > desc_parse 83).
CONNECTOR_DESC_SOURCE = "connector_desc"
CONNECTOR_DESC_CONFIDENCE = 0.90
```

- [ ] **Step 5: Add the config flag**

In `app/config.py`, immediately after `partsurfer_desc_enabled: bool = True`:
```python
    connector_desc_harvest_enabled: bool = True
```

- [ ] **Step 6: Sync the migration-096 CASE snapshot**

In `alembic/versions/096_spec_provenance.py`, in `_SOURCE_TIER_SQL_CASE`, add immediately after the `"WHEN 'partsurfer_desc' THEN 84 "` line:
```python
    "WHEN 'connector_desc' THEN 84 "
```
(This keeps `test_migration_096_spec_provenance.py`'s key-for-key assertion green. No live-DB effect — runtime tier is Python `tier_for()`; the migration already ran. Same as the `partsurfer_desc` precedent. No new migration.)

- [ ] **Step 7: Run the tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_connector_desc_harvest.py::test_connector_desc_registered_at_tier_84 tests/test_migration_096_spec_provenance.py -v --override-ini="addopts="`
Expected: PASS (both — the new tier test + the migration sync test).

- [ ] **Step 8: Commit**

```bash
git add app/services/spec_tiers.py app/services/desc_extractor/_common.py app/config.py alembic/versions/096_spec_provenance.py tests/test_connector_desc_harvest.py
git commit -m "feat(enrich): register connector_desc source at tier 84"
```

---

### Task 2: Widen `_try_connector_config` to carry the harvest fields

**Files:**
- Modify: `app/services/enrichment.py` (`_try_connector_config`, the `return {...}` ~line 107)
- Test: `tests/test_connector_desc_harvest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_connector_desc_harvest.py`:
```python
import asyncio
from unittest.mock import AsyncMock, patch

from app.services import enrichment


def test_try_connector_config_carries_harvest_fields():
    # A connector result shaped like DigiKey's: rich fields beyond manufacturer/category.
    fake_result = {
        "manufacturer": "Samsung",
        "category": "Memory",
        "description": "16GB DDR4-2666 ECC RDIMM 288-pin",
        "package_type": "DIMM-288",
        "pin_count": 288,
        "rohs_status": "compliant",
        "datasheet_url": "https://example.com/ds.pdf",
    }
    config = {"name": "digikey", "module": "x", "class": "y", "creds": [], "confidence": 0.95}

    class _Conn:
        def __init__(self, *a):
            pass

        async def search(self, mpn):
            return [fake_result]

    with patch.object(enrichment, "get_credential_cached", return_value="cred"), patch(
        "importlib.import_module"
    ) as imp:
        imp.return_value = type("M", (), {"y": _Conn})
        out = asyncio.run(enrichment._try_connector_config(config, "MEM123"))

    assert out["manufacturer"] == "Samsung"
    assert out["description"] == "16GB DDR4-2666 ECC RDIMM 288-pin"
    assert out["package_type"] == "DIMM-288"
    assert out["pin_count"] == 288
    assert out["rohs_status"] == "compliant"
    assert out["datasheet_url"] == "https://example.com/ds.pdf"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_connector_desc_harvest.py::test_try_connector_config_carries_harvest_fields -v --override-ini="addopts="`
Expected: FAIL with `KeyError: 'description'`.

- [ ] **Step 3: Widen the return dict**

In `app/services/enrichment.py::_try_connector_config`, replace the `return {...}` block (the one inside `for r in results:` that returns on a non-ignored manufacturer) with:
```python
                return {
                    "manufacturer": mfr,
                    "category": (r.get("category") or r.get("description") or "").strip()[:200] or None,
                    "source": config["name"],
                    "confidence": config["confidence"],
                    # Harvest fields — previously discarded (see connector-desc-harvest spec).
                    "description": (r.get("description") or "").strip() or None,
                    "package_type": (r.get("package_type") or "").strip() or None,
                    "pin_count": r.get("pin_count"),
                    "rohs_status": (r.get("rohs_status") or "").strip() or None,
                    "datasheet_url": (r.get("datasheet_url") or "").strip() or None,
                }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_connector_desc_harvest.py::test_try_connector_config_carries_harvest_fields -v --override-ini="addopts="`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/enrichment.py tests/test_connector_desc_harvest.py
git commit -m "feat(enrich): carry description + structured fields through _try_connector_config"
```

---

### Task 3: Harvest step in `_apply_enrichment_to_card`

**Files:**
- Modify: `app/services/enrichment.py` (new `_harvest_connector_enrichment` helper + a gated call inside `_apply_enrichment_to_card`)
- Test: `tests/test_connector_desc_harvest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_connector_desc_harvest.py`:
```python
from sqlalchemy.orm import Session

from app.config import settings
from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.spec_write_service import record_spec


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: r for r in rows}


def _component_commodity_with(*keys: str) -> str:
    """A seeded commodity whose schema defines all of *keys* (e.g. package/pin_count/rohs)."""
    import json

    seeds = json.load(open("app/data/commodity_seeds.json"))
    for commodity, specs in seeds.items():
        skeys = {s["spec_key"] for s in specs}
        if set(keys).issubset(skeys):
            return commodity
    raise AssertionError(f"no seeded commodity defines all of {keys}")


def test_description_categorizes_uncategorized_card_at_connector_desc(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mem123", display_mpn="MEM123", category=None)
    db_session.add(card)
    db_session.flush()
    enrichment_data = {
        "manufacturer": "Samsung",
        "confidence": 0.95,
        "source": "digikey",
        "category": None,
        "description": "16GB (1x16GB) DUAL RANK X4 DDR4-2666 REGISTERED ECC MEMORY",
        "package_type": None,
        "pin_count": None,
        "rohs_status": None,
        "datasheet_url": "https://example.com/ds.pdf",
    }
    enrichment._apply_enrichment_to_card(card, enrichment_data, db_session)
    db_session.commit()
    assert card.category == "dram"
    assert card.category_source == "connector_desc"
    assert card.category_tier == 84
    assert card.datasheet_url == "https://example.com/ds.pdf"


def test_structured_fields_recorded_at_vendor_tier_for_component_card(db_session: Session):
    seed_commodity_schemas(db_session)
    commodity = _component_commodity_with("package", "pin_count", "rohs")
    card = MaterialCard(normalized_mpn="cmp1", display_mpn="CMP1", category=commodity)
    db_session.add(card)
    db_session.flush()
    enrichment_data = {
        "manufacturer": "TE", "confidence": 0.95, "source": "digikey", "category": None,
        "description": None, "package_type": "SMD", "pin_count": 4, "rohs_status": "compliant",
        "datasheet_url": None,
    }
    enrichment._apply_enrichment_to_card(card, enrichment_data, db_session)
    db_session.commit()
    facets = _facets(db_session, card.id)
    assert "pin_count" in facets and facets["pin_count"].source == "digikey_api"
    assert facets["pin_count"].tier == 90


def test_flag_off_skips_harvest(db_session: Session, monkeypatch):
    seed_commodity_schemas(db_session)
    monkeypatch.setattr(settings, "connector_desc_harvest_enabled", False)
    card = MaterialCard(normalized_mpn="mem9", display_mpn="MEM9", category=None)
    db_session.add(card)
    db_session.flush()
    enrichment_data = {
        "manufacturer": "Samsung", "confidence": 0.95, "source": "digikey", "category": None,
        "description": "16GB DDR4-2666 REGISTERED ECC MEMORY", "package_type": None,
        "pin_count": None, "rohs_status": None, "datasheet_url": "https://example.com/x.pdf",
    }
    enrichment._apply_enrichment_to_card(card, enrichment_data, db_session)
    db_session.commit()
    assert card.category is None
    assert card.datasheet_url is None


def test_connector_desc_loses_to_mpn_decode(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mem5", display_mpn="MEM5", category="dram")
    db_session.add(card)
    db_session.flush()
    cache = None
    # A higher-tier decoder value lands first.
    record_spec(db_session, int(card.id), "ddr_type", "DDR4", source="mpn_decode", confidence=0.95, schema_cache=cache)
    db_session.commit()
    enrichment_data = {
        "manufacturer": "Samsung", "confidence": 0.95, "source": "digikey", "category": None,
        "description": "8GB DDR3-1600 REGISTERED ECC MEMORY", "package_type": None,
        "pin_count": None, "rohs_status": None, "datasheet_url": None,
    }
    enrichment._apply_enrichment_to_card(card, enrichment_data, db_session)
    db_session.commit()
    facets = _facets(db_session, card.id)
    # mpn_decode (85) is not clobbered by connector_desc (84).
    assert facets["ddr_type"].value_text == "DDR4"
    assert facets["ddr_type"].source == "mpn_decode"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_connector_desc_harvest.py -k "harvest or categoriz or structured or flag or mpn_decode" -v --override-ini="addopts="`
Expected: FAIL (no harvest step yet — `card.category` stays None, no facets, `datasheet_url` unset).

- [ ] **Step 3: Add the harvest helper + gated call**

In `app/services/enrichment.py`, add this helper (place it directly after `_apply_enrichment_to_card`):
```python
# Connector structured fields → seeded facet keys. lifecycle_status is intentionally
# omitted: no commodity schema defines a `lifecycle` facet (confirmed against
# commodity_seeds.json), so it would only ever no-op.
_CONNECTOR_FIELD_TO_FACET = {"package_type": "package", "pin_count": "pin_count", "rohs_status": "rohs"}


def _harvest_connector_enrichment(card: MaterialCard, enrichment: dict, ladder_source: str, db: Session) -> None:
    """Record the description + structured fields the connector returned (previously discarded).

    Description → categorize_and_record (categorizes an uncategorized card + fills facets) at
    connector_desc / tier 84. Structured fields → record_spec at the connector's vendor-API tier
    (90) — only stick where the commodity schema defines the key. datasheet_url → card column.
    All writes arbitrated by the F1 ladder; each in a per-card SAVEPOINT.
    """
    from app.services.desc_extractor._common import CONNECTOR_DESC_CONFIDENCE, CONNECTOR_DESC_SOURCE
    from app.services.desc_extractor.writer import categorize_and_record
    from app.services.spec_write_service import load_schema_cache, record_spec

    datasheet_url = (enrichment.get("datasheet_url") or "").strip()
    if datasheet_url and not card.datasheet_url:
        card.datasheet_url = datasheet_url[:1000]

    description = (enrichment.get("description") or "").strip()
    if description:
        # Fill-only: categorizes only if still uncategorized; always fills facets via the ladder.
        categorize_and_record(
            db, card, description=description, source=CONNECTOR_DESC_SOURCE, confidence=CONNECTOR_DESC_CONFIDENCE
        )

    category = (card.category or "").lower().strip()
    if not category:
        return  # no schema without a category — structured facets can't be validated
    schema_cache = load_schema_cache(db, category)
    with db.begin_nested():
        for field, facet_key in _CONNECTOR_FIELD_TO_FACET.items():
            value = enrichment.get(field)
            if value is None or value == "":
                continue
            record_spec(
                db, int(card.id), facet_key, value, source=ladder_source, confidence=0.95, schema_cache=schema_cache
            )
```

Then in `_apply_enrichment_to_card`, add this at the END of the function (after the `tag_material_card(...)` block), importing `settings` at the top of the call:
```python
    from app.config import settings

    if settings.connector_desc_harvest_enabled:
        _harvest_connector_enrichment(card, enrichment, ladder_source, db)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_connector_desc_harvest.py -v --override-ini="addopts="`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Run the existing enrichment regression**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_enrichment*.py tests/test_partsurfer_enrich.py -q --override-ini="addopts="`
Expected: PASS (no regression in the existing enrichment apply path).

- [ ] **Step 6: Commit**

```bash
git add app/services/enrichment.py tests/test_connector_desc_harvest.py
git commit -m "feat(enrich): harvest connector description + structured fields into the ladder"
```

---

### Task 4: APP_MAP doc + final verification

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md` (enrichment-writers / evidence-source-tier section)

- [ ] **Step 1: Update the APP_MAP**

In `docs/APP_MAP_INTERACTIONS.md`, in the enrichment-writers / evidence-source-tier table, add a row for `connector_desc` (tier 84 — DigiKey/Mouser/element14/OEMSecrets/Nexar description parsed by the desc grammar) and note the new harvest step in `_apply_enrichment_to_card` (structured fields at the vendor-API tier 90; `datasheet_url` stored on the card).

- [ ] **Step 2: Lint + type + format**

Run: `ruff check app/services/enrichment.py && pre-commit run --files app/services/enrichment.py app/services/spec_tiers.py app/services/desc_extractor/_common.py app/config.py alembic/versions/096_spec_provenance.py tests/test_connector_desc_harvest.py docs/APP_MAP_INTERACTIONS.md`
Run pre-commit a SECOND time (docformatter mutates then verifies).
Expected: all hooks pass on the second run.

- [ ] **Step 3: Full targeted suite**

Run: `TESTING=1 PYTHONPATH=$PWD pytest tests/test_connector_desc_harvest.py tests/test_migration_096_spec_provenance.py tests/test_spec_tiers*.py tests/test_enrichment*.py tests/test_partsurfer_enrich.py -q --override-ini="addopts="`
Expected: PASS, zero failures.

- [ ] **Step 4: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md
git commit -m "docs(app-map): connector_desc harvest writer + tier 84"
```

---

## Follow-ups (out of scope, noted)
- Search-path harvest (`search_service`) — opportunistic enrichment during pricing search.
- `datasheet_url` consumption — the datasheet PDF→facet sub-project.
- Remaining Approach-A sub-projects: eBay-title mining, Nexar-deep fields, Intel/AMD ARK CPU decoder, Lenovo PSREF.
