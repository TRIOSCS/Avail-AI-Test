# Faceted Materials Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat materials search (ILIKE + category pills) with a Newegg/DigiKey-style faceted search featuring a commodity tree, commodity-specific sub-filters, and fast facet counts.

**Architecture:** Two-column layout — left sidebar (commodity tree + dynamic sub-filters) and right results area. Server renders sub-filters from `commodity_spec_schemas` using Jinja macros. Facet counts from `material_spec_facets` table with Redis caching. Alpine.js manages filter state with URL as source of truth. All HTMX partial-based.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2, HTMX 2.x, Alpine.js 3.x, Tailwind CSS, Redis

**Prerequisite:** SP1 (Data Foundation) is 95% done — models, migration, spec_write_service, unit_normalizer all exist and are tested. This plan covers: finishing SP1 (seed data), SP2 core (batch script fix), and SP3 (faceted UI).

---

## File Structure

### New Files
| File | Purpose |
|------|---------|
| `app/services/commodity_registry.py` | Commodity tree config (parent groups → sub-categories) + schema seed function |
| `app/services/faceted_search_service.py` | Faceted query builder, facet count aggregation, sub-filter data |
| `app/templates/htmx/partials/materials/workspace.html` | Two-column layout shell with Alpine.js component |
| `app/templates/htmx/partials/materials/filters/tree.html` | Commodity tree partial (collapsible parent groups) |
| `app/templates/htmx/partials/materials/filters/subfilters.html` | Dynamic sub-filter partial (rendered per commodity) |
| `app/templates/htmx/partials/materials/filters/_macros.html` | Jinja macros: checkbox_group, range_input, boolean_toggle |
| `tests/test_commodity_registry.py` | Tests for commodity registry and seeding |
| `tests/test_faceted_search_service.py` | Tests for faceted query and count logic |

### Modified Files
| File | What Changes |
|------|-------------|
| `app/routers/htmx_views.py` | New routes: `/v2/partials/materials/workspace`, `/v2/partials/materials/filters/tree`, `/v2/partials/materials/filters/sub`, `/v2/partials/materials/faceted` |
| `app/templates/htmx/partials/materials/list.html` | Refactor into results-only partial (remove search bar + category pills — moved to workspace) |
| `app/startup.py` | Add `seed_commodity_schemas()` call |
| `scripts/enrich_specs_batch.py` | Update `apply_spec_results()` to use `record_spec()` |
| `app/static/htmx_app.js` | Add `materialsFilter()` Alpine component |
| `app/static/styles.css` | Faceted search styles (tree, sub-filters, mobile drawer) |
| `app/models/__init__.py` | Already exports faceted search models (no change needed) |

---

## Important Context

### Category Matching (verified from production DB)
- `material_cards.category` stores **lowercase underscore keys**: `capacitors`, `dram`, `logic_ic`, `power_supplies`, `network_cards`, `microcontrollers`, `tools_accessories`, `fans_cooling`, etc.
- `spec_write_service.record_spec()` does `card.category.lower().strip()` before schema lookup
- **Seed data commodity values must exactly match** the actual category values in the DB
- Top categories by count: other(5683), connectors(5281), motherboards(3290), logic_ic(1868), power_supplies(1831), capacitors(1755), hdd(1498), enclosures(1414), server_chassis(1388), dram(976), cpu(889), ssd(781), microcontrollers(614), gpu(211), network_cards(829)

### Existing Code to Reuse
- `app/services/spec_write_service.py` — `record_spec()` with conflict resolution (184 lines, fully tested)
- `app/services/unit_normalizer.py` — `normalize_value()` for unit conversions (70 lines, tested)
- `app/models/faceted_search.py` — `CommoditySpecSchema`, `MaterialSpecFacet`, `MaterialSpecConflict`
- `app/services/material_search_service.py` — `search_materials_local()` stays for text search fallback
- `app/cache/decorators.py` — `@cached_endpoint` for Redis caching
- `app/templates/htmx/partials/materials/detail.html` — detail view unchanged

### Test Setup
- Tests use SQLite in-memory via `tests/conftest.py`
- Import engine: `from tests.conftest import engine`
- Fixture: `db_session` (autouse, auto-rollback)
- Pattern: `db.add(obj)` → `db.flush()` → assert
- Run targeted: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_<file>.py -v`

---

## Task 1: Commodity Registry + Schema Seeding

**Files:**
- Create: `app/services/commodity_registry.py`
- Modify: `app/startup.py`
- Create: `tests/test_commodity_registry.py`

The commodity tree is a Python config dict (not a table) since parent groups rarely change. The seed function inserts `CommoditySpecSchema` rows for the 15 top commodities.

- [ ] **Step 1: Write test for commodity tree structure**

```python
# tests/test_commodity_registry.py
"""tests/test_commodity_registry.py -- Tests for commodity registry.

Covers: app/services/commodity_registry.py
Depends on: conftest.py, faceted search models
"""
from app.services.commodity_registry import COMMODITY_TREE, get_all_commodities, get_parent_group


def test_commodity_tree_has_parent_groups():
    assert len(COMMODITY_TREE) >= 10


def test_get_all_commodities_returns_flat_list():
    commodities = get_all_commodities()
    assert "capacitors" in commodities
    assert "dram" in commodities
    assert len(commodities) >= 40


def test_get_parent_group_returns_group_name():
    assert get_parent_group("capacitors") == "Passives"
    assert get_parent_group("network_cards") == "IT / Server Hardware"
    assert get_parent_group("cpu") == "Processors & Programmable"


def test_get_parent_group_unknown_returns_misc():
    group = get_parent_group("not_a_real_commodity")
    assert group == "Misc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_commodity_registry.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement commodity_registry.py**

```python
# app/services/commodity_registry.py
"""Commodity tree and schema seed data.

What: Defines the 2-level commodity taxonomy and provides schema seed data for
      commodity_spec_schemas table. Parent groups are display-only, sub-categories
      map to material_cards.category (lowercased).
Called by: startup.py (seed), faceted search UI (tree rendering)
Depends on: CommoditySpecSchema model
"""
from loguru import logger
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema

# 2-level tree: parent group (display only) → list of sub-categories
# Sub-category keys MUST match material_cards.category values exactly (verified from production DB)
COMMODITY_TREE: dict[str, list[str]] = {
    "Passives": ["capacitors", "resistors", "inductors", "transformers", "fuses", "oscillators", "filters"],
    "Semiconductors — Discrete": ["diodes", "transistors", "mosfets", "thyristors"],
    "Semiconductors — ICs": ["analog_ic", "logic_ic", "power_ic"],
    "Processors & Programmable": ["microcontrollers", "cpu", "microprocessors", "dsp", "fpga", "asic", "gpu"],
    "Memory & Storage": ["dram", "flash", "ssd", "hdd"],
    "Connectors & Electromechanical": ["connectors", "cables", "relays", "switches", "sockets"],
    "Power & Energy": ["power_supplies", "voltage_regulators", "batteries"],
    "Optoelectronics & Display": ["leds", "displays", "optoelectronics"],
    "Sensors & RF": ["sensors", "rf"],
    "IT / Server Hardware": ["motherboards", "network_cards", "raid_controllers", "server_chassis", "fans_cooling", "networking"],
    "Misc": ["motors", "enclosures", "tools_accessories", "other"],
}

# Display names for sub-categories (for UI rendering)
_DISPLAY_NAMES: dict[str, str] = {
    "capacitors": "Capacitors",
    "resistors": "Resistors",
    "inductors": "Inductors",
    "transformers": "Transformers",
    "fuses": "Fuses",
    "oscillators": "Oscillators",
    "filters": "Filters",
    "diodes": "Diodes",
    "transistors": "Transistors",
    "mosfets": "MOSFETs",
    "thyristors": "Thyristors",
    "analog_ic": "Analog ICs",
    "logic_ic": "Logic ICs",
    "power_ic": "Power Management ICs",
    "microcontrollers": "Microcontrollers",
    "cpu": "CPUs",
    "microprocessors": "Microprocessors",
    "dsp": "DSP",
    "fpga": "FPGAs",
    "asic": "ASIC",
    "gpu": "GPU",
    "dram": "DRAM",
    "flash": "Flash",
    "ssd": "SSD",
    "hdd": "HDD",
    "connectors": "Connectors",
    "cables": "Cables",
    "relays": "Relays",
    "switches": "Switches",
    "sockets": "Sockets",
    "power_supplies": "Power Supplies",
    "voltage_regulators": "Voltage Regulators",
    "batteries": "Batteries",
    "leds": "LEDs",
    "displays": "Displays",
    "optoelectronics": "Optoelectronics",
    "sensors": "Sensors",
    "rf": "RF & Wireless",
    "motherboards": "Motherboards",
    "network_cards": "Network Cards",
    "raid_controllers": "RAID Controllers",
    "server_chassis": "Server Chassis",
    "fans_cooling": "Fans & Cooling",
    "networking": "Networking",
    "motors": "Motors",
    "enclosures": "Enclosures",
    "tools_accessories": "Tools & Accessories",
    "other": "Other",
}

# Reverse lookup: sub-category → parent group
_PARENT_LOOKUP: dict[str, str] = {}
for _group, _subs in COMMODITY_TREE.items():
    for _sub in _subs:
        _PARENT_LOOKUP[_sub] = _group


def get_all_commodities() -> list[str]:
    """Return flat list of all sub-category keys."""
    result = []
    for subs in COMMODITY_TREE.values():
        result.extend(subs)
    return result


def get_parent_group(commodity: str) -> str:
    """Return the parent group name for a commodity, or 'Misc' if unknown."""
    return _PARENT_LOOKUP.get(commodity.lower().strip(), "Misc")


def get_display_name(commodity: str) -> str:
    """Return human-readable display name for a commodity key."""
    return _DISPLAY_NAMES.get(commodity.lower().strip(), commodity.title())


# Schema seed data: specs for the top 15 commodities
# Each entry becomes rows in commodity_spec_schemas
COMMODITY_SPEC_SEEDS: dict[str, list[dict]] = {
    "dram": [
        {"spec_key": "ddr_type", "display_name": "DDR Type", "data_type": "enum",
         "enum_values": ["DDR3", "DDR4", "DDR5", "DDR5X", "LPDDR4", "LPDDR5"], "sort_order": 1, "is_primary": True},
        {"spec_key": "capacity_gb", "display_name": "Capacity (GB)", "data_type": "numeric",
         "unit": "GB", "canonical_unit": "GB", "numeric_range": {"min": 1, "max": 256}, "sort_order": 2, "is_primary": True},
        {"spec_key": "speed_mhz", "display_name": "Speed (MHz)", "data_type": "numeric",
         "unit": "MHz", "canonical_unit": "MHz", "numeric_range": {"min": 800, "max": 8400}, "sort_order": 3},
        {"spec_key": "ecc", "display_name": "ECC", "data_type": "boolean", "sort_order": 4},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum",
         "enum_values": ["DIMM", "SO-DIMM", "UDIMM", "RDIMM", "LRDIMM"], "sort_order": 5},
    ],
    "capacitors": [
        {"spec_key": "capacitance", "display_name": "Capacitance", "data_type": "numeric",
         "unit": "pF", "canonical_unit": "pF", "numeric_range": {"min": 0.1, "max": 1000000000000}, "sort_order": 1, "is_primary": True},
        {"spec_key": "voltage_rating", "display_name": "Voltage Rating (V)", "data_type": "numeric",
         "unit": "V", "canonical_unit": "V", "numeric_range": {"min": 1, "max": 10000}, "sort_order": 2, "is_primary": True},
        {"spec_key": "dielectric", "display_name": "Dielectric", "data_type": "enum",
         "enum_values": ["X7R", "X5R", "C0G", "Y5V", "NP0"], "sort_order": 3},
        {"spec_key": "tolerance", "display_name": "Tolerance", "data_type": "enum",
         "enum_values": ["±1%", "±5%", "±10%", "±20%"], "sort_order": 4},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum",
         "enum_values": ["0402", "0603", "0805", "1206", "1210", "through-hole"], "sort_order": 5},
    ],
    "resistors": [
        {"spec_key": "resistance", "display_name": "Resistance", "data_type": "numeric",
         "unit": "ohms", "canonical_unit": "ohms", "sort_order": 1, "is_primary": True},
        {"spec_key": "power_rating", "display_name": "Power Rating (W)", "data_type": "numeric",
         "unit": "W", "canonical_unit": "W", "sort_order": 2},
        {"spec_key": "tolerance", "display_name": "Tolerance", "data_type": "enum",
         "enum_values": ["0.1%", "1%", "5%"], "sort_order": 3},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum",
         "enum_values": ["0402", "0603", "0805", "1206", "through-hole"], "sort_order": 4},
    ],
    "hdd": [
        {"spec_key": "capacity_gb", "display_name": "Capacity (GB)", "data_type": "numeric",
         "unit": "GB", "canonical_unit": "GB", "sort_order": 1, "is_primary": True},
        {"spec_key": "rpm", "display_name": "RPM", "data_type": "enum",
         "enum_values": ["5400", "7200", "10000", "15000"], "sort_order": 2},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum",
         "enum_values": ["2.5\"", "3.5\""], "sort_order": 3},
        {"spec_key": "interface", "display_name": "Interface", "data_type": "enum",
         "enum_values": ["SATA", "SAS", "NVMe"], "sort_order": 4},
    ],
    "ssd": [
        {"spec_key": "capacity_gb", "display_name": "Capacity (GB)", "data_type": "numeric",
         "unit": "GB", "canonical_unit": "GB", "sort_order": 1, "is_primary": True},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum",
         "enum_values": ["2.5\"", "M.2", "U.2", "mSATA"], "sort_order": 2},
        {"spec_key": "interface", "display_name": "Interface", "data_type": "enum",
         "enum_values": ["SATA", "NVMe", "SAS"], "sort_order": 3},
        {"spec_key": "read_speed_mbps", "display_name": "Read Speed (MB/s)", "data_type": "numeric",
         "unit": "MB/s", "canonical_unit": "MB/s", "sort_order": 4},
    ],
    "connectors": [
        {"spec_key": "pin_count", "display_name": "Pin Count", "data_type": "numeric",
         "unit": "pins", "canonical_unit": "pins", "sort_order": 1, "is_primary": True},
        {"spec_key": "pitch_mm", "display_name": "Pitch (mm)", "data_type": "numeric",
         "unit": "mm", "canonical_unit": "mm", "sort_order": 2},
        {"spec_key": "mounting", "display_name": "Mounting", "data_type": "enum",
         "enum_values": ["through-hole", "SMD", "press-fit"], "sort_order": 3},
        {"spec_key": "gender", "display_name": "Gender", "data_type": "enum",
         "enum_values": ["male", "female", "genderless"], "sort_order": 4},
        {"spec_key": "series", "display_name": "Series", "data_type": "enum", "sort_order": 5},
    ],
    "motherboards": [
        {"spec_key": "socket", "display_name": "CPU Socket", "data_type": "enum",
         "enum_values": ["LGA1700", "AM5", "LGA4677", "LGA1151", "LGA2066", "SP3"], "sort_order": 1, "is_primary": True},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum",
         "enum_values": ["ATX", "mATX", "EATX", "Mini-ITX"], "sort_order": 2},
        {"spec_key": "chipset", "display_name": "Chipset", "data_type": "enum", "sort_order": 3},
        {"spec_key": "ram_slots", "display_name": "RAM Slots", "data_type": "numeric",
         "unit": "slots", "canonical_unit": "slots", "numeric_range": {"min": 1, "max": 16}, "sort_order": 4},
    ],
    "cpu": [
        {"spec_key": "socket", "display_name": "Socket", "data_type": "enum", "sort_order": 1, "is_primary": True},
        {"spec_key": "core_count", "display_name": "Core Count", "data_type": "numeric",
         "unit": "cores", "canonical_unit": "cores", "sort_order": 2, "is_primary": True},
        {"spec_key": "clock_speed_ghz", "display_name": "Clock Speed (GHz)", "data_type": "numeric",
         "unit": "GHz", "canonical_unit": "GHz", "sort_order": 3},
        {"spec_key": "tdp_watts", "display_name": "TDP (W)", "data_type": "numeric",
         "unit": "W", "canonical_unit": "W", "sort_order": 4},
        {"spec_key": "architecture", "display_name": "Architecture", "data_type": "enum", "sort_order": 5},
    ],
    "power_supplies": [
        {"spec_key": "wattage", "display_name": "Wattage (W)", "data_type": "numeric",
         "unit": "W", "canonical_unit": "W", "sort_order": 1, "is_primary": True},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum",
         "enum_values": ["ATX", "SFX", "1U server", "2U server", "redundant"], "sort_order": 2},
        {"spec_key": "efficiency", "display_name": "Efficiency", "data_type": "enum",
         "enum_values": ["80+ Bronze", "80+ Silver", "80+ Gold", "80+ Platinum", "80+ Titanium"], "sort_order": 3},
    ],
    "gpu": [
        {"spec_key": "memory_gb", "display_name": "Memory (GB)", "data_type": "numeric",
         "unit": "GB", "canonical_unit": "GB", "sort_order": 1, "is_primary": True},
        {"spec_key": "memory_type", "display_name": "Memory Type", "data_type": "enum",
         "enum_values": ["GDDR5", "GDDR6", "GDDR6X", "HBM2", "HBM3"], "sort_order": 2},
        {"spec_key": "interface", "display_name": "Interface", "data_type": "enum",
         "enum_values": ["PCIe 3.0", "PCIe 4.0", "PCIe 5.0"], "sort_order": 3},
    ],
    "inductors": [
        {"spec_key": "inductance", "display_name": "Inductance", "data_type": "numeric",
         "unit": "nH", "canonical_unit": "nH", "sort_order": 1, "is_primary": True},
        {"spec_key": "current_rating", "display_name": "Current Rating (A)", "data_type": "numeric",
         "unit": "A", "canonical_unit": "A", "sort_order": 2},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "sort_order": 3},
    ],
    "diodes": [
        {"spec_key": "type", "display_name": "Type", "data_type": "enum",
         "enum_values": ["rectifier", "zener", "Schottky", "TVS"], "sort_order": 1, "is_primary": True},
        {"spec_key": "voltage", "display_name": "Voltage (V)", "data_type": "numeric",
         "unit": "V", "canonical_unit": "V", "sort_order": 2},
        {"spec_key": "current", "display_name": "Current (A)", "data_type": "numeric",
         "unit": "A", "canonical_unit": "A", "sort_order": 3},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "sort_order": 4},
    ],
    "mosfets": [
        {"spec_key": "channel_type", "display_name": "Channel", "data_type": "enum",
         "enum_values": ["N-channel", "P-channel"], "sort_order": 1, "is_primary": True},
        {"spec_key": "vds", "display_name": "Vds (V)", "data_type": "numeric",
         "unit": "V", "canonical_unit": "V", "sort_order": 2},
        {"spec_key": "rds_on", "display_name": "Rds(on) (mΩ)", "data_type": "numeric",
         "unit": "mOhm", "canonical_unit": "mOhm", "sort_order": 3},
        {"spec_key": "id_max", "display_name": "Id max (A)", "data_type": "numeric",
         "unit": "A", "canonical_unit": "A", "sort_order": 4},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "sort_order": 5},
    ],
    "microcontrollers": [
        {"spec_key": "core", "display_name": "Core", "data_type": "enum",
         "enum_values": ["ARM Cortex-M0", "Cortex-M3", "Cortex-M4", "Cortex-M7", "RISC-V", "AVR", "PIC"],
         "sort_order": 1, "is_primary": True},
        {"spec_key": "flash_kb", "display_name": "Flash (KB)", "data_type": "numeric",
         "unit": "KB", "canonical_unit": "KB", "sort_order": 2},
        {"spec_key": "ram_kb", "display_name": "RAM (KB)", "data_type": "numeric",
         "unit": "KB", "canonical_unit": "KB", "sort_order": 3},
        {"spec_key": "clock_mhz", "display_name": "Clock (MHz)", "data_type": "numeric",
         "unit": "MHz", "canonical_unit": "MHz", "sort_order": 4},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "sort_order": 5},
    ],
    "network_cards": [
        {"spec_key": "speed", "display_name": "Speed", "data_type": "enum",
         "enum_values": ["1GbE", "10GbE", "25GbE", "40GbE", "100GbE"], "sort_order": 1, "is_primary": True},
        {"spec_key": "ports", "display_name": "Ports", "data_type": "numeric",
         "unit": "ports", "canonical_unit": "ports", "numeric_range": {"min": 1, "max": 8}, "sort_order": 2},
        {"spec_key": "interface", "display_name": "Interface", "data_type": "enum",
         "enum_values": ["PCIe", "OCP", "LOM"], "sort_order": 3},
        {"spec_key": "controller", "display_name": "Controller", "data_type": "enum",
         "enum_values": ["Intel", "Broadcom", "Mellanox"], "sort_order": 4},
    ],
}


def seed_commodity_schemas(db: Session) -> int:
    """Seed commodity_spec_schemas table. Idempotent — skips existing rows.

    Returns number of rows inserted.
    """
    inserted = 0
    for commodity, specs in COMMODITY_SPEC_SEEDS.items():
        for spec in specs:
            existing = db.query(CommoditySpecSchema).filter_by(
                commodity=commodity, spec_key=spec["spec_key"]
            ).first()
            if existing:
                continue

            row = CommoditySpecSchema(
                commodity=commodity,
                spec_key=spec["spec_key"],
                display_name=spec["display_name"],
                data_type=spec["data_type"],
                unit=spec.get("unit"),
                canonical_unit=spec.get("canonical_unit"),
                enum_values=spec.get("enum_values"),
                numeric_range=spec.get("numeric_range"),
                sort_order=spec.get("sort_order", 0),
                is_filterable=spec.get("is_filterable", True),
                is_primary=spec.get("is_primary", False),
            )
            db.add(row)
            inserted += 1

    if inserted:
        db.commit()
        logger.info("Seeded {} commodity_spec_schemas rows", inserted)
    return inserted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_commodity_registry.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Write test for seed function**

Add to `tests/test_commodity_registry.py`:

```python
from sqlalchemy.orm import Session
from app.models import CommoditySpecSchema
from app.services.commodity_registry import seed_commodity_schemas, COMMODITY_SPEC_SEEDS
from tests.conftest import engine  # noqa: F401


def test_seed_commodity_schemas_inserts_rows(db_session: Session):
    count = seed_commodity_schemas(db_session)
    total_expected = sum(len(specs) for specs in COMMODITY_SPEC_SEEDS.values())
    assert count == total_expected

    rows = db_session.query(CommoditySpecSchema).all()
    assert len(rows) == total_expected


def test_seed_commodity_schemas_is_idempotent(db_session: Session):
    seed_commodity_schemas(db_session)
    count2 = seed_commodity_schemas(db_session)
    assert count2 == 0
```

- [ ] **Step 6: Run all commodity registry tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_commodity_registry.py -v`
Expected: 6 PASSED

- [ ] **Step 7: Wire seed into startup.py**

In `app/startup.py`, add to the startup function (alongside existing seed calls):

```python
from app.services.commodity_registry import seed_commodity_schemas
# Inside the startup function, after other seed calls:
seed_commodity_schemas(db)
```

- [ ] **Step 8: Commit**

```bash
git add app/services/commodity_registry.py tests/test_commodity_registry.py app/startup.py
git commit -m "feat: add commodity registry with tree taxonomy and schema seeding"
```

---

## Task 2: Faceted Search Service

**Files:**
- Create: `app/services/faceted_search_service.py`
- Create: `tests/test_faceted_search_service.py`

Core query logic: commodity-filtered materials, facet counts, sub-filter data.

- [ ] **Step 1: Write tests for faceted search service**

```python
# tests/test_faceted_search_service.py
"""tests/test_faceted_search_service.py -- Tests for faceted search queries.

Covers: app/services/faceted_search_service.py
Depends on: conftest.py, faceted search models, commodity_registry
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.faceted_search_service import (
    get_commodity_counts,
    get_facet_counts,
    get_subfilter_options,
    search_materials_faceted,
)
from tests.conftest import engine  # noqa: F401


def _seed_dram_schema(db: Session) -> None:
    """Insert DRAM spec schemas for testing."""
    for spec in [
        {"spec_key": "ddr_type", "display_name": "DDR Type", "data_type": "enum",
         "enum_values": ["DDR3", "DDR4", "DDR5"]},
        {"spec_key": "capacity_gb", "display_name": "Capacity (GB)", "data_type": "numeric",
         "canonical_unit": "GB"},
        {"spec_key": "ecc", "display_name": "ECC", "data_type": "boolean"},
    ]:
        db.add(CommoditySpecSchema(commodity="dram", sort_order=0, is_filterable=True, is_primary=False, **spec))
    db.flush()


def _make_dram_card(db: Session, mpn: str, ddr: str, capacity: float, ecc: bool = False) -> MaterialCard:
    """Create a DRAM card with facet rows."""
    card = MaterialCard(
        normalized_mpn=mpn.lower(), display_mpn=mpn, manufacturer="TestCo",
        category="DRAM", created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    for spec_key, val_text, val_num in [
        ("ddr_type", ddr, None),
        ("capacity_gb", None, capacity),
        ("ecc", "true" if ecc else "false", None),
    ]:
        db.add(MaterialSpecFacet(
            material_card_id=card.id, category="dram", spec_key=spec_key,
            value_text=val_text, value_numeric=val_num,
        ))
    db.flush()
    return card


# --- Commodity counts ---

def test_get_commodity_counts(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR5", 32)

    # Add a capacitor card (no facets, just category)
    cap = MaterialCard(
        normalized_mpn="cap-001", display_mpn="CAP-001", manufacturer="TestCo",
        category="Capacitors", created_at=datetime.now(timezone.utc),
    )
    db_session.add(cap)
    db_session.flush()

    counts = get_commodity_counts(db_session)
    assert counts["dram"] == 2
    assert counts["capacitors"] == 1


# --- Facet counts ---

def test_get_facet_counts_for_dram(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR4", 32)
    _make_dram_card(db_session, "MEM-003", "DDR5", 16)

    counts = get_facet_counts(db_session, "dram")
    assert counts["ddr_type"]["DDR4"] == 2
    assert counts["ddr_type"]["DDR5"] == 1


# --- Faceted search ---

def test_search_materials_faceted_by_commodity(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)

    cap = MaterialCard(
        normalized_mpn="cap-001", display_mpn="CAP-001", manufacturer="TestCo",
        category="Capacitors", created_at=datetime.now(timezone.utc),
    )
    db_session.add(cap)
    db_session.flush()

    results, total = search_materials_faceted(db_session, commodity="dram")
    assert total == 1
    assert results[0].normalized_mpn == "mem-001"


def test_search_materials_faceted_with_subfilters(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR5", 32)
    _make_dram_card(db_session, "MEM-003", "DDR4", 32)

    results, total = search_materials_faceted(
        db_session, commodity="dram", sub_filters={"ddr_type": ["DDR4"]},
    )
    assert total == 2
    mpns = {r.normalized_mpn for r in results}
    assert mpns == {"mem-001", "mem-003"}


def test_search_materials_faceted_numeric_range(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR4", 32)

    results, total = search_materials_faceted(
        db_session, commodity="dram", sub_filters={"capacity_gb_min": 20},
    )
    assert total == 1
    assert results[0].normalized_mpn == "mem-002"


# --- Sub-filter options ---

def test_get_subfilter_options(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR5", 32)

    options = get_subfilter_options(db_session, "dram")
    assert len(options) == 3  # ddr_type, capacity_gb, ecc
    ddr_opt = next(o for o in options if o["spec_key"] == "ddr_type")
    assert "DDR4" in ddr_opt["values"]
    assert "DDR5" in ddr_opt["values"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_search_service.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement faceted_search_service.py**

```python
# app/services/faceted_search_service.py
"""Faceted search query service.

What: Builds faceted queries on material_cards + material_spec_facets.
      Provides commodity counts, facet counts, sub-filter options.
Called by: htmx_views.py faceted search routes
Depends on: MaterialCard, MaterialSpecFacet, CommoditySpecSchema
"""
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet


def get_commodity_counts(db: Session) -> dict[str, int]:
    """Return {commodity_key: count} for all non-deleted material cards."""
    rows = (
        db.query(
            func.lower(func.trim(MaterialCard.category)),
            func.count(MaterialCard.id),
        )
        .filter(MaterialCard.deleted_at.is_(None), MaterialCard.category.isnot(None))
        .group_by(func.lower(func.trim(MaterialCard.category)))
        .all()
    )
    return {cat: count for cat, count in rows if cat}


def get_facet_counts(
    db: Session,
    commodity: str,
    active_filters: dict | None = None,
) -> dict[str, dict[str, int]]:
    """Return facet value counts for a commodity.

    Returns: {spec_key: {value: count, ...}, ...}
    Only includes text-based facets (enums, booleans).
    """
    commodity = commodity.lower().strip()

    base_q = db.query(MaterialSpecFacet.material_card_id).filter(
        MaterialSpecFacet.category == commodity,
    )

    # Apply active filters to narrow the base set
    if active_filters:
        for key, values in active_filters.items():
            if key.endswith("_min") or key.endswith("_max"):
                continue  # Range filters handled separately
            if isinstance(values, list) and values:
                base_q = base_q.filter(
                    MaterialSpecFacet.material_card_id.in_(
                        db.query(MaterialSpecFacet.material_card_id).filter(
                            MaterialSpecFacet.category == commodity,
                            MaterialSpecFacet.spec_key == key,
                            MaterialSpecFacet.value_text.in_(values),
                        )
                    )
                )

    card_ids_subq = base_q.distinct().subquery()

    rows = (
        db.query(
            MaterialSpecFacet.spec_key,
            MaterialSpecFacet.value_text,
            func.count(MaterialSpecFacet.material_card_id.distinct()),
        )
        .filter(
            MaterialSpecFacet.category == commodity,
            MaterialSpecFacet.value_text.isnot(None),
            MaterialSpecFacet.material_card_id.in_(db.query(card_ids_subq)),
        )
        .group_by(MaterialSpecFacet.spec_key, MaterialSpecFacet.value_text)
        .all()
    )

    result: dict[str, dict[str, int]] = {}
    for spec_key, value, count in rows:
        result.setdefault(spec_key, {})[value] = count
    return result


def search_materials_faceted(
    db: Session,
    *,
    commodity: str | None = None,
    q: str | None = None,
    sub_filters: dict | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MaterialCard], int]:
    """Search materials with faceted filters.

    Args:
        commodity: Filter by commodity category (lowercased)
        q: Text search on MPN/manufacturer/description
        sub_filters: {spec_key: [values]} for enums, {spec_key_min: val} for ranges
        limit: Max results
        offset: Pagination offset

    Returns: (materials, total_count)
    """
    query = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))

    if commodity:
        query = query.filter(func.lower(func.trim(MaterialCard.category)) == commodity.lower().strip())

    if q:
        pattern = f"%{q}%"
        query = query.filter(
            (MaterialCard.normalized_mpn.ilike(pattern))
            | (MaterialCard.display_mpn.ilike(pattern))
            | (MaterialCard.manufacturer.ilike(pattern))
            | (MaterialCard.description.ilike(pattern))
        )

    if sub_filters and commodity:
        commodity_lower = commodity.lower().strip()
        for key, values in sub_filters.items():
            if key.endswith("_min"):
                spec_key = key[:-4]  # Remove _min suffix
                query = query.filter(
                    MaterialCard.id.in_(
                        db.query(MaterialSpecFacet.material_card_id).filter(
                            MaterialSpecFacet.category == commodity_lower,
                            MaterialSpecFacet.spec_key == spec_key,
                            MaterialSpecFacet.value_numeric >= values,
                        )
                    )
                )
            elif key.endswith("_max"):
                spec_key = key[:-4]
                query = query.filter(
                    MaterialCard.id.in_(
                        db.query(MaterialSpecFacet.material_card_id).filter(
                            MaterialSpecFacet.category == commodity_lower,
                            MaterialSpecFacet.spec_key == spec_key,
                            MaterialSpecFacet.value_numeric <= values,
                        )
                    )
                )
            elif isinstance(values, list) and values:
                query = query.filter(
                    MaterialCard.id.in_(
                        db.query(MaterialSpecFacet.material_card_id).filter(
                            MaterialSpecFacet.category == commodity_lower,
                            MaterialSpecFacet.spec_key == key,
                            MaterialSpecFacet.value_text.in_(values),
                        )
                    )
                )

    total = query.count()
    materials = (
        query.order_by(MaterialCard.search_count.desc(), MaterialCard.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return materials, total


def get_subfilter_options(db: Session, commodity: str) -> list[dict]:
    """Get sub-filter options for a commodity from schema + actual data.

    Returns list of dicts: {spec_key, display_name, data_type, values|range, unit, is_primary}
    """
    commodity = commodity.lower().strip()
    schemas = (
        db.query(CommoditySpecSchema)
        .filter_by(commodity=commodity, is_filterable=True)
        .order_by(CommoditySpecSchema.sort_order)
        .all()
    )

    result = []
    for schema in schemas:
        option = {
            "spec_key": schema.spec_key,
            "display_name": schema.display_name,
            "data_type": schema.data_type,
            "unit": schema.unit,
            "is_primary": schema.is_primary,
        }

        if schema.data_type == "enum":
            # Get actual values from data (not just enum_values from schema)
            actual = (
                db.query(MaterialSpecFacet.value_text)
                .filter(
                    MaterialSpecFacet.category == commodity,
                    MaterialSpecFacet.spec_key == schema.spec_key,
                    MaterialSpecFacet.value_text.isnot(None),
                )
                .distinct()
                .all()
            )
            option["values"] = sorted([r[0] for r in actual])

        elif schema.data_type == "numeric":
            # Get min/max from actual data
            agg = (
                db.query(
                    func.min(MaterialSpecFacet.value_numeric),
                    func.max(MaterialSpecFacet.value_numeric),
                )
                .filter(
                    MaterialSpecFacet.category == commodity,
                    MaterialSpecFacet.spec_key == schema.spec_key,
                    MaterialSpecFacet.value_numeric.isnot(None),
                )
                .first()
            )
            option["range"] = {"min": agg[0], "max": agg[1]} if agg and agg[0] is not None else None

        elif schema.data_type == "boolean":
            option["values"] = ["true", "false"]

        result.append(option)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_search_service.py -v`
Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add app/services/faceted_search_service.py tests/test_faceted_search_service.py
git commit -m "feat: add faceted search query service with commodity counts and sub-filters"
```

---

## Task 3: Filter Macros + Commodity Tree Partial

**Files:**
- Create: `app/templates/htmx/partials/materials/filters/_macros.html`
- Create: `app/templates/htmx/partials/materials/filters/tree.html`
- Create: `app/templates/htmx/partials/materials/filters/subfilters.html`

Jinja macros for reusable filter controls, commodity tree, and dynamic sub-filters.

- [ ] **Step 1: Create filter macros**

Create `app/templates/htmx/partials/materials/filters/_macros.html`:

```html
{# Filter macros for faceted search sidebar.
   Called by: subfilters.html
   Depends on: Alpine.js materialsFilter() component on parent
#}

{% macro checkbox_group(spec_key, display_name, values, counts, active_values) %}
<div class="mb-4">
  <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">{{ display_name }}</h4>
  <div class="space-y-1 max-h-48 overflow-y-auto">
    {% for val in values %}
    <label class="flex items-center gap-2 px-2 py-1 rounded hover:bg-gray-50 cursor-pointer text-sm">
      <input type="checkbox" value="{{ val }}"
             :checked="(subFilters['{{ spec_key }}'] || []).includes('{{ val }}')"
             @change="toggleFilter('{{ spec_key }}', '{{ val }}')"
             class="rounded border-gray-300 text-brand-600 focus:ring-brand-500 h-3.5 w-3.5">
      <span class="text-gray-700 truncate">{{ val }}</span>
      {% if counts and val in counts %}
      <span class="ml-auto text-xs text-gray-400">({{ counts[val] }})</span>
      {% endif %}
    </label>
    {% endfor %}
  </div>
</div>
{% endmacro %}

{% macro range_input(spec_key, display_name, range_data, unit, active_min, active_max) %}
<div class="mb-4">
  <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
    {{ display_name }}{% if unit %} <span class="text-gray-400 font-normal">({{ unit }})</span>{% endif %}
  </h4>
  {% if range_data and range_data.min is not none %}
  <div class="flex items-center gap-2">
    <input type="number" placeholder="Min"
           :value="subFilters['{{ spec_key }}_min'] || ''"
           @change="setRange('{{ spec_key }}', 'min', $event.target.value)"
           class="w-full px-2 py-1.5 text-sm border border-gray-200 rounded focus:ring-1 focus:ring-brand-500 focus:border-brand-500"
           min="{{ range_data.min }}" max="{{ range_data.max }}" step="any">
    <span class="text-gray-400 text-xs">–</span>
    <input type="number" placeholder="Max"
           :value="subFilters['{{ spec_key }}_max'] || ''"
           @change="setRange('{{ spec_key }}', 'max', $event.target.value)"
           class="w-full px-2 py-1.5 text-sm border border-gray-200 rounded focus:ring-1 focus:ring-brand-500 focus:border-brand-500"
           min="{{ range_data.min }}" max="{{ range_data.max }}" step="any">
  </div>
  {% else %}
  <p class="text-xs text-gray-400 italic">No data available</p>
  {% endif %}
</div>
{% endmacro %}

{% macro boolean_toggle(spec_key, display_name, counts, active_value) %}
<div class="mb-4">
  <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">{{ display_name }}</h4>
  <div class="flex gap-2">
    <button @click="toggleFilter('{{ spec_key }}', 'true')"
            :class="(subFilters['{{ spec_key }}'] || []).includes('true') ? 'bg-brand-100 text-brand-700 border-brand-300' : 'bg-white text-gray-600 border-gray-200'"
            class="px-3 py-1 text-sm border rounded-full transition-colors">
      Yes {% if counts and 'true' in counts %}<span class="text-xs opacity-60">({{ counts['true'] }})</span>{% endif %}
    </button>
    <button @click="toggleFilter('{{ spec_key }}', 'false')"
            :class="(subFilters['{{ spec_key }}'] || []).includes('false') ? 'bg-brand-100 text-brand-700 border-brand-300' : 'bg-white text-gray-600 border-gray-200'"
            class="px-3 py-1 text-sm border rounded-full transition-colors">
      No {% if counts and 'false' in counts %}<span class="text-xs opacity-60">({{ counts['false'] }})</span>{% endif %}
    </button>
  </div>
</div>
{% endmacro %}
```

- [ ] **Step 2: Create commodity tree partial**

Create `app/templates/htmx/partials/materials/filters/tree.html`:

```html
{# Commodity tree — 2-level collapsible list.
   Called by: workspace.html (loaded once, cached 1hr)
   Depends on: commodity_tree, commodity_counts from route context
#}
<div class="space-y-1">
  {% for group_name, sub_categories in commodity_tree.items() %}
  <div x-data="{ open: false }">
    <button @click="open = !open"
            class="w-full flex items-center justify-between px-3 py-2 text-xs font-semibold text-gray-500 uppercase tracking-wider hover:bg-gray-50 rounded transition-colors">
      <span>{{ group_name }}</span>
      <svg :class="open && 'rotate-90'" class="w-3.5 h-3.5 text-gray-400 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/>
      </svg>
    </button>
    <div x-show="open" x-collapse class="ml-2">
      {% for sub in sub_categories %}
      {% set count = commodity_counts.get(sub, 0) %}
      {% if count > 0 %}
      <button @click="selectCommodity('{{ sub }}')"
              :class="commodity === '{{ sub }}' ? 'bg-brand-50 text-brand-700 border-l-2 border-brand-500' : 'text-gray-600 hover:bg-gray-50 border-l-2 border-transparent'"
              class="w-full text-left px-3 py-1.5 text-sm flex items-center justify-between transition-colors">
        <span>{{ display_names.get(sub, sub) }}</span>
        <span class="text-xs text-gray-400">({{ count }})</span>
      </button>
      {% endif %}
      {% endfor %}
    </div>
  </div>
  {% endfor %}
</div>
```

- [ ] **Step 3: Create sub-filters partial**

Create `app/templates/htmx/partials/materials/filters/subfilters.html`:

```html
{# Dynamic sub-filters for a specific commodity.
   Called by: HTMX request when commodity is selected
   Depends on: subfilter_options, facet_counts from route context
#}
{% from "htmx/partials/materials/filters/_macros.html" import checkbox_group, range_input, boolean_toggle %}

{% if subfilter_options %}
<div class="border-t border-gray-100 pt-3 mt-3">
  <div class="flex items-center justify-between mb-3">
    <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider">Filters</h3>
    <button @click="clearSubFilters()" x-show="Object.keys(subFilters).length > 0"
            class="text-xs text-brand-600 hover:text-brand-700">Clear all</button>
  </div>

  {% for opt in subfilter_options %}
    {% if opt.data_type == 'enum' and opt.values %}
      {{ checkbox_group(opt.spec_key, opt.display_name, opt.values, facet_counts.get(opt.spec_key, {}), []) }}
    {% elif opt.data_type == 'numeric' %}
      {{ range_input(opt.spec_key, opt.display_name, opt.range, opt.unit, None, None) }}
    {% elif opt.data_type == 'boolean' %}
      {{ boolean_toggle(opt.spec_key, opt.display_name, facet_counts.get(opt.spec_key, {}), None) }}
    {% endif %}
  {% endfor %}
</div>
{% else %}
<div class="border-t border-gray-100 pt-3 mt-3">
  <p class="text-xs text-gray-400 italic">Select a category to see filters</p>
</div>
{% endif %}
```

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/materials/filters/
git commit -m "feat: add Jinja macros and partials for faceted search filters"
```

---

## Task 4: Workspace Layout + Alpine.js Component

**Files:**
- Create: `app/templates/htmx/partials/materials/workspace.html`
- Modify: `app/static/htmx_app.js`

Two-column layout with Alpine.js state management and URL sync.

- [ ] **Step 1: Create workspace.html**

Create `app/templates/htmx/partials/materials/workspace.html`:

```html
{# Materials faceted search workspace — two-column layout.
   Called by: htmx_views.py materials_workspace route
   Depends on: Alpine.js materialsFilter() in htmx_app.js
#}
<div x-data="materialsFilter()" x-init="init()" class="flex gap-0 h-full">

  {# -- Mobile filter button -- #}
  <button @click="panelOpen = true" x-show="!panelOpen"
          class="lg:hidden fixed bottom-20 left-4 z-30 bg-brand-600 text-white rounded-full px-4 py-2 shadow-lg flex items-center gap-2 text-sm">
    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2.586a1 1 0 01-.293.707l-6.414 6.414a1 1 0 00-.293.707V17l-4 4v-6.586a1 1 0 00-.293-.707L3.293 7.293A1 1 0 013 6.586V4z"/>
    </svg>
    Filters
    <template x-if="activeFilterCount > 0">
      <span class="bg-white text-brand-600 rounded-full w-5 h-5 text-xs flex items-center justify-center font-bold" x-text="activeFilterCount"></span>
    </template>
  </button>

  {# -- Left sidebar -- #}
  <div :class="panelOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'"
       class="fixed lg:static inset-y-0 left-0 z-40 w-72 bg-white border-r border-gray-100 overflow-y-auto transition-transform lg:transition-none flex-shrink-0"
       style="top: 0; bottom: 0;">

    {# Mobile close button #}
    <div class="lg:hidden flex items-center justify-between p-3 border-b border-gray-100">
      <span class="font-semibold text-sm text-gray-700">Filters</span>
      <button @click="panelOpen = false" class="text-gray-400 hover:text-gray-600">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
        </svg>
      </button>
    </div>

    <div class="p-3">
      {# Commodity tree — loaded once #}
      <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Categories</h3>
      <button @click="selectCommodity(null)"
              :class="!commodity ? 'bg-brand-50 text-brand-700 font-medium' : 'text-gray-600 hover:bg-gray-50'"
              class="w-full text-left px-3 py-1.5 text-sm rounded transition-colors mb-1">
        All Materials <span class="text-xs text-gray-400">({{ total_materials }})</span>
      </button>

      <div hx-get="/v2/partials/materials/filters/tree"
           hx-trigger="load"
           hx-swap="innerHTML">
        <div class="animate-pulse space-y-2 mt-2">
          {% for _ in range(5) %}
          <div class="h-6 bg-gray-100 rounded"></div>
          {% endfor %}
        </div>
      </div>

      {# Dynamic sub-filters — loaded when commodity changes #}
      <div id="subfilters-container"
           hx-get="/v2/partials/materials/filters/sub"
           hx-trigger="commodity-changed from:body"
           hx-vals='js:{"commodity": Alpine.evaluate($el.closest("[x-data]"), "commodity")}'
           hx-swap="innerHTML">
      </div>
    </div>

    {# Mobile apply button #}
    <div class="lg:hidden sticky bottom-0 p-3 bg-white border-t border-gray-100">
      <button @click="panelOpen = false; applyFilters()"
              class="w-full bg-brand-600 text-white py-2 rounded-lg text-sm font-medium">
        Show Results
      </button>
    </div>
  </div>

  {# Mobile backdrop #}
  <div x-show="panelOpen" @click="panelOpen = false"
       class="lg:hidden fixed inset-0 bg-black/30 z-30" x-transition.opacity></div>

  {# -- Right results area -- #}
  <div class="flex-1 min-w-0">
    {# Search bar #}
    <div class="p-4 border-b border-gray-100">
      <div class="relative">
        <input type="text" x-model="q"
               @input.debounce.400ms="applyFilters()"
               placeholder="Search by MPN, manufacturer, or description..."
               class="w-full pl-10 pr-4 py-2 border border-gray-200 rounded-lg text-sm focus:ring-1 focus:ring-brand-500 focus:border-brand-500">
        <svg class="absolute left-3 top-2.5 w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
        </svg>
      </div>

      {# Active filter chips #}
      <template x-if="commodity">
        <div class="flex flex-wrap gap-1.5 mt-2">
          <span class="inline-flex items-center gap-1 px-2 py-0.5 bg-brand-50 text-brand-700 rounded-full text-xs">
            <span x-text="commodityDisplayName"></span>
            <button @click="selectCommodity(null)" class="hover:text-brand-900">&times;</button>
          </span>
          <template x-for="[key, vals] in Object.entries(subFilters)" :key="key">
            <template x-for="val in (Array.isArray(vals) ? vals : [vals])" :key="key + val">
              <span class="inline-flex items-center gap-1 px-2 py-0.5 bg-gray-100 text-gray-600 rounded-full text-xs">
                <span x-text="key.replace(/_/g, ' ') + ': ' + val"></span>
                <button @click="removeFilter(key, val)" class="hover:text-gray-900">&times;</button>
              </span>
            </template>
          </template>
        </div>
      </template>
    </div>

    {# Results container — loaded via HTMX #}
    <div id="materials-results"
         hx-get="/v2/partials/materials/faceted"
         hx-trigger="load, filters-changed from:body"
         hx-vals='js:{
           "commodity": Alpine.evaluate(document.querySelector("[x-data]"), "commodity") || "",
           "q": Alpine.evaluate(document.querySelector("[x-data]"), "q") || "",
           "sub_filters": JSON.stringify(Alpine.evaluate(document.querySelector("[x-data]"), "subFilters") || {}),
           "limit": 50,
           "offset": Alpine.evaluate(document.querySelector("[x-data]"), "page") * 50 || 0
         }'
         hx-swap="innerHTML"
         hx-indicator="#results-loading">
      {# Skeleton loading #}
      <div class="p-4 space-y-3">
        {% for _ in range(8) %}
        <div class="animate-pulse flex gap-4">
          <div class="h-4 bg-gray-100 rounded w-32"></div>
          <div class="h-4 bg-gray-100 rounded w-24"></div>
          <div class="h-4 bg-gray-100 rounded w-20"></div>
          <div class="h-4 bg-gray-100 rounded flex-1"></div>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Add materialsFilter() Alpine component to htmx_app.js**

Append to `app/static/htmx_app.js`:

```javascript
/* Faceted materials search — Alpine.js component.
 * Manages commodity, sub-filters, search query, pagination.
 * URL is the canonical source of truth (back button, deep links work).
 */
function materialsFilter() {
  return {
    commodity: '',
    commodityDisplayName: '',
    subFilters: {},
    q: '',
    page: 0,
    panelOpen: false,

    get activeFilterCount() {
      let count = 0;
      for (const [key, val] of Object.entries(this.subFilters)) {
        if (Array.isArray(val)) count += val.length;
        else if (val !== '' && val !== null) count += 1;
      }
      return count;
    },

    init() {
      this.syncFromURL();
      window.addEventListener('popstate', () => this.syncFromURL());
    },

    syncFromURL() {
      const params = new URLSearchParams(window.location.search);
      this.commodity = params.get('commodity') || '';
      this.q = params.get('q') || '';
      this.page = parseInt(params.get('page') || '0', 10);
      this.subFilters = {};
      for (const [key, val] of params.entries()) {
        if (key.startsWith('sf_')) {
          const specKey = key.slice(3);
          if (specKey.endsWith('_min') || specKey.endsWith('_max')) {
            this.subFilters[specKey] = parseFloat(val);
          } else {
            this.subFilters[specKey] = val.split(',');
          }
        }
      }
    },

    pushURL() {
      const params = new URLSearchParams();
      if (this.commodity) params.set('commodity', this.commodity);
      if (this.q) params.set('q', this.q);
      if (this.page > 0) params.set('page', this.page);
      for (const [key, val] of Object.entries(this.subFilters)) {
        if (Array.isArray(val) && val.length > 0) {
          params.set('sf_' + key, val.join(','));
        } else if (typeof val === 'number' && !isNaN(val)) {
          params.set('sf_' + key, val);
        }
      }
      const search = params.toString();
      const url = window.location.pathname + (search ? '?' + search : '');
      history.pushState({}, '', url);
    },

    selectCommodity(commodity) {
      this.commodity = commodity || '';
      this.commodityDisplayName = commodity ? commodity.replace(/(^|\s)\S/g, l => l.toUpperCase()) : '';
      this.subFilters = {};
      this.page = 0;
      this.pushURL();
      document.body.dispatchEvent(new CustomEvent('commodity-changed'));
      this.applyFilters();
    },

    toggleFilter(specKey, value) {
      if (!this.subFilters[specKey]) {
        this.subFilters[specKey] = [value];
      } else {
        const idx = this.subFilters[specKey].indexOf(value);
        if (idx >= 0) {
          this.subFilters[specKey].splice(idx, 1);
          if (this.subFilters[specKey].length === 0) {
            delete this.subFilters[specKey];
          }
        } else {
          this.subFilters[specKey].push(value);
        }
      }
      this.page = 0;
      // Desktop: instant apply
      if (window.innerWidth >= 1024) {
        this.pushURL();
        this.applyFilters();
      }
    },

    setRange(specKey, bound, value) {
      const key = specKey + '_' + bound;
      if (value === '' || value === null) {
        delete this.subFilters[key];
      } else {
        this.subFilters[key] = parseFloat(value);
      }
      this.page = 0;
      if (window.innerWidth >= 1024) {
        this.pushURL();
        this.applyFilters();
      }
    },

    removeFilter(key, val) {
      if (Array.isArray(this.subFilters[key])) {
        this.subFilters[key] = this.subFilters[key].filter(v => v !== val);
        if (this.subFilters[key].length === 0) delete this.subFilters[key];
      } else {
        delete this.subFilters[key];
      }
      this.page = 0;
      this.pushURL();
      this.applyFilters();
    },

    clearSubFilters() {
      this.subFilters = {};
      this.page = 0;
      this.pushURL();
      this.applyFilters();
    },

    applyFilters() {
      this.pushURL();
      document.body.dispatchEvent(new CustomEvent('filters-changed'));
    },

    goToPage(newPage) {
      this.page = newPage;
      this.pushURL();
      this.applyFilters();
    },
  };
}
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/materials/workspace.html app/static/htmx_app.js
git commit -m "feat: add materials workspace layout and Alpine.js filter component"
```

---

## Task 5: HTMX Routes for Faceted Search

**Files:**
- Modify: `app/routers/htmx_views.py`

Add routes for workspace, commodity tree, sub-filters, and faceted results.

- [ ] **Step 1: Write tests for new routes**

Add to `tests/test_faceted_search_service.py` (or create `tests/test_faceted_routes.py`):

```python
# tests/test_faceted_routes.py
"""tests/test_faceted_routes.py -- Tests for faceted search HTMX routes.

Covers: Faceted search routes in app/routers/htmx_views.py
Depends on: conftest.py, faceted search models, commodity_registry
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from tests.conftest import engine  # noqa: F401


def test_materials_workspace_renders(client):
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    assert "materialsFilter" in resp.text


def test_commodity_tree_renders(client):
    resp = client.get("/v2/partials/materials/filters/tree")
    assert resp.status_code == 200
    assert "Passives" in resp.text


def test_subfilters_renders_for_commodity(client, db_session: Session):
    # Seed schema
    db_session.add(CommoditySpecSchema(
        commodity="dram", spec_key="ddr_type", display_name="DDR Type",
        data_type="enum", enum_values=["DDR4", "DDR5"],
        sort_order=1, is_filterable=True, is_primary=False,
    ))
    db_session.commit()

    resp = client.get("/v2/partials/materials/filters/sub?commodity=dram")
    assert resp.status_code == 200
    assert "DDR Type" in resp.text


def test_faceted_results_returns_materials(client, db_session: Session):
    card = MaterialCard(
        normalized_mpn="test-001", display_mpn="TEST-001", manufacturer="TestCo",
        category="DRAM", created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?commodity=dram")
    assert resp.status_code == 200
    assert "TEST-001" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_routes.py -v`
Expected: FAIL — 404 route not found

- [ ] **Step 3: Add routes to htmx_views.py**

Add to `app/routers/htmx_views.py` (near the existing materials routes):

```python
import json as _json

from app.services.commodity_registry import COMMODITY_TREE, get_display_name
from app.services.faceted_search_service import (
    get_commodity_counts,
    get_facet_counts,
    get_subfilter_options,
    search_materials_faceted,
)
# NOTE: htmx_views.py already imports `from sqlalchemy import func` — use the existing import.
# If it's aliased (e.g., `sqlfunc`), use that alias in the route code below.


@router.get("/v2/partials/materials/workspace", response_class=HTMLResponse)
async def materials_workspace(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the two-column faceted search workspace."""
    total = db.query(func.count(MaterialCard.id)).filter(MaterialCard.deleted_at.is_(None)).scalar()
    return templates.TemplateResponse(
        "htmx/partials/materials/workspace.html",
        {"request": request, "total_materials": total},
    )


@router.get("/v2/partials/materials/filters/tree", response_class=HTMLResponse)
async def materials_filter_tree(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render commodity tree with counts. Cached 1hr."""
    counts = get_commodity_counts(db)
    display_names = {sub: get_display_name(sub) for subs in COMMODITY_TREE.values() for sub in subs}
    return templates.TemplateResponse(
        "htmx/partials/materials/filters/tree.html",
        {"request": request, "commodity_tree": COMMODITY_TREE, "commodity_counts": counts, "display_names": display_names},
    )


@router.get("/v2/partials/materials/filters/sub", response_class=HTMLResponse)
async def materials_subfilters(
    request: Request,
    commodity: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render dynamic sub-filters for a specific commodity."""
    if not commodity:
        return HTMLResponse("")

    options = get_subfilter_options(db, commodity)
    counts = get_facet_counts(db, commodity)
    return templates.TemplateResponse(
        "htmx/partials/materials/filters/subfilters.html",
        {"request": request, "subfilter_options": options, "facet_counts": counts},
    )


@router.get("/v2/partials/materials/faceted", response_class=HTMLResponse)
async def materials_faceted_results(
    request: Request,
    commodity: str = "",
    q: str = "",
    sub_filters: str = "{}",
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render faceted search results."""
    parsed_filters = {}
    try:
        raw = _json.loads(sub_filters)
        for key, val in raw.items():
            if key.endswith("_min") or key.endswith("_max"):
                parsed_filters[key] = float(val)
            elif isinstance(val, list):
                parsed_filters[key] = val
    except (ValueError, TypeError):
        pass

    materials, total = search_materials_faceted(
        db,
        commodity=commodity or None,
        q=q or None,
        sub_filters=parsed_filters or None,
        limit=min(limit, 200),
        offset=offset,
    )

    # Attach vendor count + best price to each material object
    # (matching existing pattern in materials_list_partial — list.html reads m._vendor_count, m._best_price)
    from app.models import MaterialVendorHistory
    card_ids = [m.id for m in materials]
    vendor_stats = {}
    if card_ids:
        stats = (
            db.query(
                MaterialVendorHistory.material_card_id,
                func.count(MaterialVendorHistory.id),
                func.min(MaterialVendorHistory.price),
            )
            .filter(MaterialVendorHistory.material_card_id.in_(card_ids))
            .group_by(MaterialVendorHistory.material_card_id)
            .all()
        )
        vendor_stats = {s[0]: (s[1], s[2]) for s in stats}

    for m in materials:
        vc, bp = vendor_stats.get(m.id, (0, None))
        m._vendor_count = vc
        m._best_price = bp

    return templates.TemplateResponse(
        "htmx/partials/materials/list.html",
        {
            "request": request,
            "materials": materials,
            "total": total,
            "limit": limit,
            "offset": offset,
            "commodity": commodity,
            "q": q,
            "faceted": True,
        },
    )
```

- [ ] **Step 4: Update materials/list.html to work in faceted mode**

The existing `list.html` needs minor updates to work as a results-only partial when `faceted=True`:
- Skip rendering the search bar and category pills when `faceted` is set
- Use `vendor_stats` dict instead of inline computation
- Add `hx-vals` to pagination links for filter state

Key changes at the top of `list.html`:

```html
{% if not faceted %}
{# Original search bar + category pills — only shown in non-faceted mode #}
...
{% endif %}
```

And in the table, use `vendor_stats.get(m.id, {})` for counts/prices.

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_routes.py -v`
Expected: 4 PASSED

- [ ] **Step 6: Commit**

```bash
git add app/routers/htmx_views.py tests/test_faceted_routes.py app/templates/htmx/partials/materials/list.html
git commit -m "feat: add HTMX routes for faceted search workspace, tree, sub-filters, results"
```

---

## Task 6: Wire Materials Tab to Faceted Workspace

**Files:**
- Modify: `app/routers/htmx_views.py` (existing materials tab route)
- Modify: `app/static/styles.css`

Switch the Materials tab from loading `list.html` directly to loading `workspace.html`.

- [ ] **Step 1: Update the materials tab HTMX target**

The existing materials tab likely loads `/v2/partials/materials` into the content area. Change this to load `/v2/partials/materials/workspace` instead.

Find the tab navigation in the main layout or the tab switching logic. The materials tab button should trigger:

```html
hx-get="/v2/partials/materials/workspace"
```

instead of the current:

```html
hx-get="/v2/partials/materials"
```

Keep the old `/v2/partials/materials` route working for backward compatibility.

- [ ] **Step 2: Add faceted search styles to styles.css**

Append to `app/static/styles.css`:

```css
/* Faceted search sidebar */
@media (min-width: 1024px) {
  .faceted-sidebar {
    height: calc(100vh - 120px);
    position: sticky;
    top: 60px;
  }
}
```

- [ ] **Step 3: Test in browser**

Navigate to the Materials tab. Verify:
- Two-column layout renders
- Commodity tree loads and shows categories with counts
- Clicking a commodity loads sub-filters
- Clicking a sub-filter value updates results
- Search bar filters within commodity
- URL updates with filter state
- Back button restores previous state
- Mobile: filter button shows, drawer slides in

- [ ] **Step 4: Commit**

```bash
git add app/routers/htmx_views.py app/static/styles.css
git commit -m "feat: wire materials tab to faceted search workspace"
```

---

## Task 7: Update Batch Script to Use record_spec()

**Files:**
- Modify: `scripts/enrich_specs_batch.py`

The existing `apply_spec_results()` writes to `specs_summary` (text). Update it to also call `record_spec()` for `specs_structured` + facets.

- [ ] **Step 1: Write test for batch result application**

```python
# tests/test_enrich_specs_batch.py
"""tests/test_enrich_specs_batch.py -- Tests for spec batch application.

Covers: scripts/enrich_specs_batch.py (apply_spec_results path)
Depends on: conftest.py, faceted search models
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from tests.conftest import engine  # noqa: F401


@pytest.mark.asyncio
async def test_apply_results_calls_record_spec(db_session: Session, tmp_path):
    """Applying batch results should write to specs_structured via record_spec."""
    seed_commodity_schemas(db_session)

    card = MaterialCard(
        normalized_mpn="mem-test", display_mpn="MEM-TEST", manufacturer="TestCo",
        category="DRAM", created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()

    import json
    meta = {
        "batch_id": "test-batch",
        "category": "dram",
        "request_map": {
            "specs_dram_0": [{"id": card.id, "mpn": "MEM-TEST"}],
        },
    }
    meta_path = str(tmp_path / "meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    mock_results = {
        "specs_dram_0": {
            "parts": [{
                "mpn": "MEM-TEST",
                "ddr_type": "DDR4",
                "ddr_type_confidence": 0.95,
                "capacity_gb": 16,
                "capacity_gb_confidence": 0.90,
            }],
        },
    }

    with patch("app.utils.claude_client.claude_batch_results", new_callable=AsyncMock, return_value=mock_results):
        from scripts.enrich_specs_batch import apply_spec_results
        stats = await apply_spec_results(meta_path, db_session, dry_run=False)

    assert stats["updated"] > 0

    db_session.refresh(card)
    assert card.specs_structured is not None
    assert card.specs_structured.get("ddr_type", {}).get("value") == "DDR4"

    facet = db_session.query(MaterialSpecFacet).filter_by(
        material_card_id=card.id, spec_key="ddr_type"
    ).first()
    assert facet is not None
    assert facet.value_text == "DDR4"
```

- [ ] **Step 2: Update apply_spec_results() in enrich_specs_batch.py**

In `scripts/enrich_specs_batch.py`, modify `apply_spec_results()` to call `record_spec()`:

```python
# Add import at top:
from app.services.spec_write_service import record_spec

# In apply_spec_results(), replace the db.query(MaterialCard).filter(...).update(...) block with:
for card_info, ai_part in zip(card_meta_list, parts):
    card_id = card_info["id"]
    stats["processed"] += 1

    summary = _specs_to_summary(category, ai_part)
    if not summary:
        stats["skipped"] += 1
        continue

    if not dry_run:
        # Write specs_summary (legacy)
        db.query(MaterialCard).filter(MaterialCard.id == card_id).update(
            {"specs_summary": summary},
            synchronize_session=False,
        )

        # Write to specs_structured + facets via record_spec
        schema = COMMODITY_SPECS.get(category, {})
        for spec in schema.get("specs", []):
            value = ai_part.get(spec["key"])
            conf = ai_part.get(f"{spec['key']}_confidence", 0.0)
            if value is not None and conf >= 0.70:
                record_spec(
                    db, card_id, spec["key"], value,
                    source="haiku_extraction",
                    confidence=conf,
                    unit=spec.get("unit", "").split("/")[0] if spec.get("unit") else None,
                )

    stats["updated"] += 1
```

- [ ] **Step 3: Run test**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enrich_specs_batch.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add scripts/enrich_specs_batch.py tests/test_enrich_specs_batch.py
git commit -m "feat: update batch spec extraction to write specs_structured via record_spec"
```

---

## Task 8: Full Suite + Integration Verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

Expected: All tests pass, no regressions.

- [ ] **Step 2: Check for import issues**

```bash
TESTING=1 PYTHONPATH=/root/availai python -c "
from app.services.commodity_registry import COMMODITY_TREE, seed_commodity_schemas
from app.services.faceted_search_service import search_materials_faceted, get_commodity_counts
print('All imports OK')
"
```

- [ ] **Step 3: Commit any fixes and push**

```bash
git push origin main
```

- [ ] **Step 4: Deploy and test**

```bash
cd /root/availai && git pull origin main && docker compose up -d --build && echo "Done — hard refresh browser"
```

Test in browser:
1. Navigate to Materials tab — should show two-column workspace
2. Commodity tree loads with counts
3. Click "DRAM" — sub-filters appear (DDR Type, Capacity, ECC, Form Factor, Speed)
4. Click "DDR4" checkbox — results filter
5. URL updates with `?commodity=dram&sf_ddr_type=DDR4`
6. Back button restores previous state
7. Mobile: filter drawer works

---

## Summary

| Task | Description | Files | Est. Steps |
|------|-------------|-------|-----------|
| 1 | Commodity Registry + Schema Seeding | commodity_registry.py, startup.py, tests | 8 |
| 2 | Faceted Search Service | faceted_search_service.py, tests | 5 |
| 3 | Filter Macros + Tree + Sub-filter Partials | 3 template files | 4 |
| 4 | Workspace Layout + Alpine.js | workspace.html, htmx_app.js | 3 |
| 5 | HTMX Routes | htmx_views.py, test_faceted_routes.py, list.html | 6 |
| 6 | Wire Materials Tab | htmx_views.py, styles.css | 4 |
| 7 | Update Batch Script | enrich_specs_batch.py, tests | 4 |
| 8 | Full Suite + Deploy | verification only | 4 |

**Total:** 8 tasks, ~38 steps

**Not covered (separate plan):** SP4 (AI Search Integration — Haiku query → commodity + filter pre-selection)
