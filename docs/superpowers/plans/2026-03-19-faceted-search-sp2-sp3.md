# Faceted Search SP2+SP3: Data Population & UI Polish

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate the faceted search with structured spec data from AI extraction and vendor APIs, then polish the UI with remaining review fixes.

**Architecture:** SP1 (data foundation) is fully built — models, services, migrations, tests. SP3 (faceted UI) is also built — workspace, tree, subfilters, macros, Alpine component, routes. The `enrich_specs_batch.py` script exists for AI extraction. Remaining work: (1) vendor API enrichment service to parse specs from existing sightings, (2) run AI extraction for 7 priority commodities, (3) UI polish from code review.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Anthropic Batch API, HTMX 2.x, Alpine.js 3.x, Jinja2, Tailwind CSS

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `app/services/vendor_spec_enrichment.py` | Parse structured specs from DigiKey/Nexar/Mouser sighting raw_data |
| `tests/test_vendor_spec_enrichment.py` | Tests for vendor spec parsing |
| `scripts/backfill_vendor_specs.py` | One-time script to backfill specs from existing sightings |

### Modified Files
| File | Changes |
|------|---------|
| `scripts/enrich_specs_batch.py` | Fix unit parameter (use `unit` not `canonical_unit`) |
| `app/templates/htmx/partials/materials/filters/tree.html` | Auto-expand active commodity group |
| `app/templates/htmx/partials/materials/filters/_macros.html` | Range input debounce, defensive URL parsing |
| `app/templates/htmx/partials/materials/workspace.html` | Smart empty state |
| `app/templates/htmx/partials/materials/list.html` | Show spec summary chips in faceted mode |
| `app/static/htmx_app.js` | Defensive syncFromURL, tree auto-expand signal |
| `app/routers/htmx_views.py` | Pass `active_group` to tree template |

---

### Task 1: Vendor API Spec Enrichment Service

**Files:**
- Create: `app/services/vendor_spec_enrichment.py`
- Create: `tests/test_vendor_spec_enrichment.py`

This service parses structured spec data from DigiKey/Nexar/Mouser API responses stored in sighting `raw_data` JSON. Each connector returns different field names — this service normalizes them.

- [ ] **Step 1: Write failing tests for DigiKey spec parsing**

```python
# tests/test_vendor_spec_enrichment.py
"""Tests for vendor spec enrichment service.

What: Tests parsing of structured specs from vendor API raw_data.
Called by: pytest
Depends on: vendor_spec_enrichment, spec_write_service, conftest fixtures
"""
import pytest
from unittest.mock import MagicMock, patch

from app.services.vendor_spec_enrichment import (
    parse_digikey_specs,
    parse_nexar_specs,
    parse_mouser_specs,
    enrich_card_from_sightings,
)


class TestParseDigikeySpecs:
    def test_extracts_capacitor_specs(self):
        raw_data = {
            "parameters": [
                {"parameter": "Capacitance", "value": "100µF"},
                {"parameter": "Voltage - Rated", "value": "25V"},
                {"parameter": "Temperature Coefficient", "value": "X7R"},
                {"parameter": "Package / Case", "value": "0805"},
                {"parameter": "Tolerance", "value": "±10%"},
            ]
        }
        result = parse_digikey_specs(raw_data, "capacitors")
        assert result["capacitance"] == {"value": "100µF", "confidence": 0.95}
        assert result["voltage_rating"] == {"value": "25V", "confidence": 0.95}
        assert result["dielectric"] == {"value": "X7R", "confidence": 0.95}

    def test_extracts_dram_specs(self):
        raw_data = {
            "parameters": [
                {"parameter": "Memory Type", "value": "DDR5"},
                {"parameter": "Memory Size", "value": "16GB"},
                {"parameter": "Speed", "value": "4800 MT/s"},
                {"parameter": "Module Type", "value": "DIMM"},
            ]
        }
        result = parse_digikey_specs(raw_data, "dram")
        assert result["ddr_type"] == {"value": "DDR5", "confidence": 0.95}
        assert result["capacity_gb"] == {"value": 16, "confidence": 0.95}

    def test_returns_empty_for_unknown_category(self):
        result = parse_digikey_specs({"parameters": []}, "unknown_category")
        assert result == {}

    def test_returns_empty_for_no_parameters(self):
        result = parse_digikey_specs({}, "capacitors")
        assert result == {}


class TestParseNexarSpecs:
    def test_extracts_from_specs_list(self):
        raw_data = {
            "specs": [
                {"attribute": {"name": "Capacitance"}, "displayValue": "100pF"},
                {"attribute": {"name": "Voltage Rating"}, "displayValue": "50V"},
            ]
        }
        result = parse_nexar_specs(raw_data, "capacitors")
        assert "capacitance" in result
        assert "voltage_rating" in result

    def test_returns_empty_for_no_specs(self):
        result = parse_nexar_specs({}, "capacitors")
        assert result == {}


class TestParseMouserSpecs:
    def test_extracts_from_product_attributes(self):
        raw_data = {
            "ProductAttributes": [
                {"AttributeName": "Capacitance", "AttributeValue": "10nF"},
                {"AttributeName": "Voltage Rated", "AttributeValue": "16V"},
            ]
        }
        result = parse_mouser_specs(raw_data, "capacitors")
        assert "capacitance" in result

    def test_returns_empty_for_no_attributes(self):
        result = parse_mouser_specs({}, "capacitors")
        assert result == {}


class TestEnrichCardFromSightings:
    def test_enriches_card_with_vendor_specs(self, db_session):
        """Integration test: creates card + sighting, enriches specs."""
        from app.models.intelligence import MaterialCard

        card = MaterialCard(
            normalized_mpn="test-cap-001",
            display_mpn="TEST-CAP-001",
            manufacturer="TDK",
            category="capacitors",
        )
        db_session.add(card)
        db_session.commit()

        # Mock sighting with raw_data
        mock_sighting = MagicMock()
        mock_sighting.source_type = "digikey"
        mock_sighting.raw_data = {
            "parameters": [
                {"parameter": "Capacitance", "value": "100nF"},
                {"parameter": "Voltage - Rated", "value": "50V"},
            ]
        }

        with patch(
            "app.services.vendor_spec_enrichment._get_sightings_for_card",
            return_value=[mock_sighting],
        ):
            count = enrich_card_from_sightings(db_session, card.id)

        assert count >= 1  # At least one spec recorded
```

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_vendor_spec_enrichment.py -v`
Expected: FAIL (module doesn't exist)

- [ ] **Step 2: Implement vendor spec enrichment service**

```python
# app/services/vendor_spec_enrichment.py
"""Vendor API spec enrichment — parses structured specs from sighting raw_data.

What: Extracts technical specs from DigiKey/Nexar/Mouser API response fields.
Called by: backfill_vendor_specs.py script, future sighting creation hooks.
Depends on: spec_write_service.record_spec(), commodity_registry
"""
import re

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.services.spec_write_service import load_schema_cache, record_spec

# DigiKey parameter name → our spec_key, grouped by commodity
_DIGIKEY_MAP: dict[str, dict[str, str]] = {
    "capacitors": {
        "Capacitance": "capacitance",
        "Voltage - Rated": "voltage_rating",
        "Temperature Coefficient": "dielectric",
        "Tolerance": "tolerance",
        "Package / Case": "package",
    },
    "resistors": {
        "Resistance": "resistance",
        "Power (Watts)": "power_rating",
        "Tolerance": "tolerance",
        "Package / Case": "package",
    },
    "dram": {
        "Memory Type": "ddr_type",
        "Memory Size": "capacity_gb",
        "Speed": "speed_mhz",
        "Error Correction": "ecc",
        "Module Type": "form_factor",
    },
    # ... extend for other commodities as needed
}

# Nexar attribute name → our spec_key (same mapping, different field names)
_NEXAR_MAP: dict[str, dict[str, str]] = {
    "capacitors": {
        "Capacitance": "capacitance",
        "Voltage Rating": "voltage_rating",
        "Dielectric": "dielectric",
        "Tolerance": "tolerance",
        "Package / Case": "package",
    },
    # ... extend as needed
}

# Mouser attribute name → our spec_key
_MOUSER_MAP: dict[str, dict[str, str]] = {
    "capacitors": {
        "Capacitance": "capacitance",
        "Voltage Rated": "voltage_rating",
        "Dielectric": "dielectric",
        "Tolerance": "tolerance",
        "Package/Case": "package",
    },
    # ... extend as needed
}


def _extract_numeric(value_str: str) -> tuple[float | None, str | None]:
    """Extract numeric value and unit from strings like '100µF', '25V', '16GB'.

    Returns (numeric_value, unit) or (None, None) if not parseable.
    """
    if not value_str:
        return None, None

    match = re.match(r"([0-9]*\.?[0-9]+)\s*([a-zA-Zµ°/%]+)?", value_str.strip())
    if not match:
        return None, None

    num = float(match.group(1))
    unit = match.group(2) if match.group(2) else None
    return num, unit


def parse_digikey_specs(raw_data: dict, category: str) -> dict[str, dict]:
    """Parse DigiKey parameters into normalized spec dict.

    Returns: {spec_key: {"value": ..., "confidence": 0.95}, ...}
    """
    mapping = _DIGIKEY_MAP.get(category, {})
    if not mapping:
        return {}

    parameters = raw_data.get("parameters", [])
    if not parameters:
        return {}

    result = {}
    for param in parameters:
        param_name = param.get("parameter", "")
        param_value = param.get("value", "")
        if not param_name or not param_value:
            continue

        spec_key = mapping.get(param_name)
        if spec_key:
            # Try to extract numeric for numeric-type specs
            numeric, unit = _extract_numeric(param_value)
            if numeric is not None and spec_key in (
                "capacitance", "voltage_rating", "resistance", "power_rating",
                "capacity_gb", "speed_mhz",
            ):
                result[spec_key] = {"value": numeric, "unit": unit, "confidence": 0.95}
            elif spec_key == "ecc":
                result[spec_key] = {"value": "ecc" in param_value.lower(), "confidence": 0.95}
            else:
                result[spec_key] = {"value": param_value, "confidence": 0.95}

    return result


def parse_nexar_specs(raw_data: dict, category: str) -> dict[str, dict]:
    """Parse Nexar specs into normalized spec dict."""
    mapping = _NEXAR_MAP.get(category, {})
    if not mapping:
        return {}

    specs = raw_data.get("specs", [])
    if not specs:
        return {}

    result = {}
    for spec in specs:
        attr_name = spec.get("attribute", {}).get("name", "")
        display_value = spec.get("displayValue", "")
        if not attr_name or not display_value:
            continue

        spec_key = mapping.get(attr_name)
        if spec_key:
            numeric, unit = _extract_numeric(display_value)
            if numeric is not None and spec_key in (
                "capacitance", "voltage_rating", "resistance", "power_rating",
            ):
                result[spec_key] = {"value": numeric, "unit": unit, "confidence": 0.95}
            else:
                result[spec_key] = {"value": display_value, "confidence": 0.95}

    return result


def parse_mouser_specs(raw_data: dict, category: str) -> dict[str, dict]:
    """Parse Mouser ProductAttributes into normalized spec dict."""
    mapping = _MOUSER_MAP.get(category, {})
    if not mapping:
        return {}

    attrs = raw_data.get("ProductAttributes", [])
    if not attrs:
        return {}

    result = {}
    for attr in attrs:
        attr_name = attr.get("AttributeName", "")
        attr_value = attr.get("AttributeValue", "")
        if not attr_name or not attr_value:
            continue

        spec_key = mapping.get(attr_name)
        if spec_key:
            numeric, unit = _extract_numeric(attr_value)
            if numeric is not None and spec_key in (
                "capacitance", "voltage_rating", "resistance", "power_rating",
            ):
                result[spec_key] = {"value": numeric, "unit": unit, "confidence": 0.95}
            else:
                result[spec_key] = {"value": attr_value, "confidence": 0.95}

    return result


_SOURCE_MAP = {
    "digikey": ("digikey_api", parse_digikey_specs),
    "nexar": ("nexar_api", parse_nexar_specs),
    "mouser": ("mouser_api", parse_mouser_specs),
}


def _get_sightings_for_card(db: Session, card_id: int) -> list:
    """Get sightings with raw_data for a card."""
    from app.models.intelligence import Sighting

    return (
        db.query(Sighting)
        .filter(
            Sighting.material_card_id == card_id,
            Sighting.raw_data.isnot(None),
            Sighting.source_type.in_(["digikey", "nexar", "mouser"]),
        )
        .order_by(Sighting.created_at.desc())
        .limit(10)
        .all()
    )


def enrich_card_from_sightings(db: Session, card_id: int) -> int:
    """Parse specs from a card's vendor sightings and record them.

    Returns number of specs recorded.
    """
    card = db.get(MaterialCard, card_id)
    if not card or not card.category:
        return 0

    category = card.category.lower().strip()
    schema_cache = load_schema_cache(db, category)
    if not schema_cache:
        return 0

    sightings = _get_sightings_for_card(db, card_id)
    recorded = 0

    for sighting in sightings:
        source_info = _SOURCE_MAP.get(sighting.source_type)
        if not source_info:
            continue

        source_name, parser = source_info
        raw = sighting.raw_data if isinstance(sighting.raw_data, dict) else {}
        specs = parser(raw, category)

        for spec_key, spec_data in specs.items():
            record_spec(
                db,
                card_id,
                spec_key,
                spec_data["value"],
                source=source_name,
                confidence=spec_data["confidence"],
                unit=spec_data.get("unit"),
                schema_cache=schema_cache,
            )
            recorded += 1

    return recorded
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_vendor_spec_enrichment.py -v`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add app/services/vendor_spec_enrichment.py tests/test_vendor_spec_enrichment.py
git commit -m "feat: vendor API spec enrichment service for DigiKey/Nexar/Mouser"
```

---

### Task 2: Vendor Spec Backfill Script

**Files:**
- Create: `scripts/backfill_vendor_specs.py`

One-time script to backfill specs from existing sightings in production.

- [ ] **Step 1: Write the backfill script**

```python
# scripts/backfill_vendor_specs.py
"""Backfill structured specs from existing vendor API sightings.

What: Iterates material cards with vendor sightings, parses raw_data for specs.
Called by: manual one-time script
Depends on: vendor_spec_enrichment.enrich_card_from_sightings
"""
import argparse
import os
import sys

from loguru import logger

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))
from app.database import SessionLocal
from app.models.intelligence import MaterialCard, Sighting
from sqlalchemy import func


def backfill(category: str | None = None, limit: int = 0, dry_run: bool = True):
    """Backfill specs from vendor sightings."""
    from app.services.vendor_spec_enrichment import enrich_card_from_sightings

    db = SessionLocal()
    try:
        # Find cards that have vendor sightings with raw_data
        query = (
            db.query(MaterialCard.id)
            .join(Sighting, Sighting.material_card_id == MaterialCard.id)
            .filter(
                MaterialCard.deleted_at.is_(None),
                Sighting.raw_data.isnot(None),
                Sighting.source_type.in_(["digikey", "nexar", "mouser"]),
            )
            .group_by(MaterialCard.id)
        )

        if category:
            query = query.filter(
                func.lower(func.trim(MaterialCard.category)) == category.lower()
            )

        if limit:
            query = query.limit(limit)

        card_ids = [r[0] for r in query.all()]
        logger.info(f"Found {len(card_ids)} cards with vendor sightings")

        stats = {"total": len(card_ids), "enriched": 0, "specs_added": 0, "skipped": 0}

        for i, card_id in enumerate(card_ids):
            if dry_run:
                stats["skipped"] += 1
                continue

            count = enrich_card_from_sightings(db, card_id)
            if count > 0:
                stats["enriched"] += 1
                stats["specs_added"] += count
            else:
                stats["skipped"] += 1

            if (i + 1) % 500 == 0:
                db.commit()
                logger.info(f"Progress: {i + 1}/{len(card_ids)} — {stats}")

        if not dry_run:
            db.commit()

        mode = "DRY RUN" if dry_run else "APPLIED"
        logger.info(f"[{mode}] Backfill complete: {stats}")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill specs from vendor sightings")
    parser.add_argument("--category", help="Specific category (e.g., capacitors)")
    parser.add_argument("--limit", type=int, default=0, help="Max cards (0 = all)")
    parser.add_argument("--apply", action="store_true", help="Actually write (default: dry run)")
    args = parser.parse_args()

    backfill(category=args.category, limit=args.limit, dry_run=not args.apply)
```

- [ ] **Step 2: Commit**

```bash
git add scripts/backfill_vendor_specs.py
git commit -m "feat: vendor spec backfill script"
```

---

### Task 3: Fix enrich_specs_batch.py Unit Parameter

**Files:**
- Modify: `scripts/enrich_specs_batch.py:259`

The batch extraction script passes `unit=spec.get("canonical_unit")` to `record_spec()`, but `record_spec` expects `unit` to be the source unit (e.g., "µF") so it can normalize TO the canonical unit. When the source unit equals the canonical unit, no conversion happens — which is correct for AI extraction where Haiku is instructed to use canonical units. But verify this is intentional.

- [ ] **Step 1: Read the unit_normalizer to understand the contract**

Read `app/services/unit_normalizer.py` and `app/services/spec_write_service.py:82-85`.

If `unit == canonical_unit`, `normalize_value()` returns the value unchanged (no conversion needed). So passing `canonical_unit` is correct — it means "value is already in canonical form." No code change needed.

- [ ] **Step 2: Add a comment clarifying this**

In `enrich_specs_batch.py:259`, add a comment:
```python
unit=spec.get("canonical_unit"),  # AI output is in canonical units; no conversion needed
```

If the comment already exists or is equivalent, skip this task.

- [ ] **Step 3: Commit if changed**

```bash
git add scripts/enrich_specs_batch.py
git commit -m "docs: clarify unit parameter in batch spec extraction"
```

---

### Task 4: Tree Auto-Expand for Active Commodity

**Files:**
- Modify: `app/templates/htmx/partials/materials/filters/tree.html`
- Modify: `app/routers/htmx_views.py` (tree route)

When a commodity is selected, the tree should auto-expand the group containing it.

- [ ] **Step 1: Pass active_commodity to tree template**

In `htmx_views.py`, find the `materials_filters_tree_partial` route. Add parsing of `commodity` query param and pass it to the template context.

- [ ] **Step 2: Update tree.html to auto-expand active group**

Change `x-data="{ open: false }"` to check if the group contains the active commodity:

```html
<div x-data="{ open: {{ 'true' if active_commodity and active_commodity in sub_categories else 'false' }} }">
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_routes.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/materials/filters/tree.html app/routers/htmx_views.py
git commit -m "fix: auto-expand commodity tree group for active selection"
```

---

### Task 5: Range Input Debounce Fix

**Files:**
- Modify: `app/templates/htmx/partials/materials/filters/_macros.html`

Range inputs currently use `@change` which only fires on blur. Change to `@input.debounce.600ms` for live-as-you-type filtering.

- [ ] **Step 1: Update range_input macro**

In `_macros.html`, find the two `<input type="number">` elements inside `range_input` macro. Change:
```
@change="setRange('{{ spec_key }}', 'min', $event.target.value)"
```
to:
```
@input.debounce.600ms="setRange('{{ spec_key }}', 'min', $event.target.value)"
```

Do the same for the max input.

- [ ] **Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_routes.py -v`

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/materials/filters/_macros.html
git commit -m "fix: range input debounce for live filtering"
```

---

### Task 6: Defensive URL Sync + Smart Empty State

**Files:**
- Modify: `app/static/htmx_app.js` — `syncFromURL()` method
- Modify: `app/templates/htmx/partials/materials/list.html` — empty state

- [ ] **Step 1: Add defensive parsing to syncFromURL**

In `htmx_app.js`, find `syncFromURL()`. Add validation:
- If `commodity` param exists but is not in the known commodities list, ignore it
- If `sf_*` params have values that can't be parsed, ignore them silently
- Wrap the whole function in try/catch to prevent broken URLs from crashing the component

```javascript
syncFromURL() {
    try {
        const params = new URLSearchParams(window.location.search);
        this.commodity = params.get('commodity') || '';
        this.q = params.get('q') || '';
        this.page = parseInt(params.get('page') || '0', 10) || 0;
        // Parse sf_ params defensively
        this.subFilters = {};
        for (const [key, value] of params.entries()) {
            if (key.startsWith('sf_')) {
                const specKey = key.slice(3);
                if (key.endsWith('_min') || key.endsWith('_max')) {
                    const num = parseFloat(value);
                    if (!isNaN(num)) this.subFilters[specKey] = num;
                } else {
                    this.subFilters[specKey] = value.split(',').filter(v => v);
                }
            }
        }
    } catch (e) {
        console.warn('Failed to sync from URL:', e);
    }
},
```

- [ ] **Step 2: Improve empty state in list.html**

In `list.html`, find the "No material cards found" empty state. When in faceted mode, show a smarter message:

```html
{% if faceted %}
<div class="text-center py-12 text-gray-400">
  <svg class="mx-auto h-10 w-10 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2.586a1 1 0 01-.293.707l-6.414 6.414a1 1 0 00-.293.707V17l-4 4v-6.586a1 1 0 00-.293-.707L3.293 7.293A1 1 0 013 6.586V4z"/>
  </svg>
  <p class="text-sm font-medium text-gray-500">No results match your filters</p>
  <p class="text-xs text-gray-400 mt-1">Try removing a filter or broadening your search</p>
</div>
{% else %}
<!-- existing empty state -->
{% endif %}
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_routes.py -v`

- [ ] **Step 4: Commit**

```bash
git add app/static/htmx_app.js app/templates/htmx/partials/materials/list.html
git commit -m "fix: defensive URL sync + smart empty state for faceted search"
```

---

### Task 7: Spec Summary Chips in Results

**Files:**
- Modify: `app/templates/htmx/partials/materials/list.html`
- Modify: `app/routers/htmx_views.py` (faceted route)

When in faceted mode, show key specs as small chips below the MPN in the results table, so users can see spec values without clicking into each card.

- [ ] **Step 1: Pass primary specs to template**

First, add the import at the top of `htmx_views.py` (or in the existing imports section):
```python
from app.models import CommoditySpecSchema
```

Then, in the `materials_faceted_partial` route, after fetching materials, for each material that has `specs_structured`, extract primary specs (from `commodity_spec_schemas` where `is_primary=True`) and attach as `m._primary_specs`.

```python
# After materials are fetched, attach primary specs for display
primary_keys = {
    s.spec_key: s.display_name
    for s in db.query(CommoditySpecSchema)
    .filter_by(commodity=commodity, is_primary=True)
    .all()
} if commodity else {}

for m in materials:
    specs = m.specs_structured or {}
    m._primary_specs = [
        {"label": primary_keys[k], "value": specs[k].get("value", "")}
        for k in primary_keys
        if k in specs
    ]
```

**Important:** Every material must have `_primary_specs` set (even if empty list) so the template can safely check it.

- [ ] **Step 2: Show spec chips in list.html**

In `list.html`, after the MPN cell content, add:

```html
{% if m._primary_specs %}
<div class="flex flex-wrap gap-1 mt-0.5">
  {% for spec in m._primary_specs[:3] %}
  <span class="inline-block px-1.5 py-0.5 bg-gray-50 text-[10px] text-gray-500 rounded font-data">
    {{ spec.value }}
  </span>
  {% endfor %}
</div>
{% endif %}
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_routes.py -v`

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/materials/list.html app/routers/htmx_views.py
git commit -m "feat: show primary spec chips in faceted search results"
```

---

### Task 8: Run AI Batch Extraction (Operational)

**Files:** None (operational task using existing scripts)

Run the existing `enrich_specs_batch.py` script for the 7 priority commodities inside the Docker container.

- [ ] **Step 1: Submit batches for 7 commodities**

```bash
docker compose exec app python scripts/enrich_specs_batch.py submit dram --limit 500
docker compose exec app python scripts/enrich_specs_batch.py submit capacitors --limit 500
docker compose exec app python scripts/enrich_specs_batch.py submit resistors --limit 500
docker compose exec app python scripts/enrich_specs_batch.py submit ssd --limit 500
docker compose exec app python scripts/enrich_specs_batch.py submit hdd --limit 500
docker compose exec app python scripts/enrich_specs_batch.py submit motherboards --limit 500
docker compose exec app python scripts/enrich_specs_batch.py submit power_supplies --limit 500
```

Start with `--limit 500` per category to validate quality before running full extraction.

- [ ] **Step 2: Check batch status**

Wait for batches to complete (check via API or logs), then apply:

```bash
docker compose exec app python scripts/enrich_specs_batch.py apply /tmp/specs_batch_<category>_<batch_id>.json
```

Review dry-run output first, then re-run with `--apply`.

- [ ] **Step 3: Run vendor spec backfill**

```bash
docker compose exec app python scripts/backfill_vendor_specs.py --limit 100
# Review dry-run output
docker compose exec app python scripts/backfill_vendor_specs.py --apply
```

- [ ] **Step 4: Verify data quality**

```bash
docker compose exec db psql -U availai -d availai -c "
  SELECT category, COUNT(DISTINCT material_card_id) as cards_with_specs
  FROM material_spec_facets
  GROUP BY category
  ORDER BY cards_with_specs DESC;
"
```

Verify facet counts appear in the UI for each commodity.

---

### Task 9: Integration Test & Full Suite

**Files:**
- Modify: existing test files if needed

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

All tests must pass.

- [ ] **Step 2: Manual UI verification**

Navigate to the Materials tab, click "Faceted Search" view:
1. Verify commodity tree shows counts
2. Click a commodity — verify sub-filters appear
3. Check/uncheck filter — verify results update
4. Type in search — verify results filter
5. Verify URL updates with filters
6. Verify "Clear all" resets everything

- [ ] **Step 3: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration test fixes for faceted search"
```
