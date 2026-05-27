# Materials Filter Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand materials filtering with deeper per-commodity specs, a global manufacturer dropdown, and automated lifecycle detection.

**Architecture:** Seed-driven expansion — add new specs to `commodity_seeds.json`, add manufacturer filter to sidebar + search service, add lifecycle sweep job. No new models; uses existing `CommoditySpecSchema` + `MaterialSpecFacet` architecture.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2/HTMX, Alpine.js, APScheduler

**Spec:** `docs/superpowers/specs/2026-03-20-materials-filter-expansion-design.md`

---

### Task 1: Expand Commodity Seeds

**Files:**
- Modify: `app/data/commodity_seeds.json`
- Test: `tests/test_commodity_registry.py`

- [ ] **Step 1: Write test for expanded seed count**

Add to `tests/test_commodity_registry.py`:

```python
def test_expanded_seeds_have_minimum_specs():
    """Every commodity should have at least 4 specs after expansion."""
    from app.services.commodity_registry import COMMODITY_SPEC_SEEDS

    for commodity, specs in COMMODITY_SPEC_SEEDS.items():
        assert len(specs) >= 4, f"{commodity} has only {len(specs)} specs, expected >= 4"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_commodity_registry.py::test_expanded_seeds_have_minimum_specs -v`
Expected: FAIL — several commodities have < 4 specs (e.g., inductors=3, flash=3, gpu=3, power_supplies=3)

- [ ] **Step 3: Add new specs to commodity_seeds.json**

Add these entries to each commodity in `app/data/commodity_seeds.json`. Use existing format — every entry needs `spec_key`, `display_name`, `data_type`, and `sort_order`. Enum specs need `enum_values`. Numeric specs need `unit`, `canonical_unit`, `numeric_range`.

**Capacitors** — add after existing 5 specs (sort_order 6):
```json
{
  "spec_key": "mounting",
  "display_name": "Mounting",
  "data_type": "enum",
  "enum_values": ["SMD", "through-hole", "press-fit"],
  "sort_order": 6
}
```

**Resistors** — add after existing 4 specs (sort_order 5):
```json
{
  "spec_key": "mounting",
  "display_name": "Mounting",
  "data_type": "enum",
  "enum_values": ["SMD", "through-hole", "press-fit"],
  "sort_order": 5
}
```

**Inductors** — add after existing 3 specs (sort_order 4-5):
```json
{
  "spec_key": "mounting",
  "display_name": "Mounting",
  "data_type": "enum",
  "enum_values": ["SMD", "through-hole", "press-fit"],
  "sort_order": 4
},
{
  "spec_key": "inductor_type",
  "display_name": "Type",
  "data_type": "enum",
  "enum_values": ["Ferrite", "Wirewound", "Multilayer", "Film", "Ceramic"],
  "sort_order": 5
}
```

**Diodes** — add after existing 4 specs (sort_order 5):
```json
{
  "spec_key": "mounting",
  "display_name": "Mounting",
  "data_type": "enum",
  "enum_values": ["SMD", "through-hole", "press-fit"],
  "sort_order": 5
}
```

**MOSFETs** — add after existing 5 specs (sort_order 6):
```json
{
  "spec_key": "mounting",
  "display_name": "Mounting",
  "data_type": "enum",
  "enum_values": ["SMD", "through-hole", "press-fit"],
  "sort_order": 6
}
```

**Microcontrollers** — add after existing 5 specs (sort_order 6-10):
```json
{
  "spec_key": "supply_voltage",
  "display_name": "Supply Voltage",
  "data_type": "numeric",
  "unit": "V",
  "canonical_unit": "V",
  "numeric_range": {"min": 1.0, "max": 5.5},
  "sort_order": 6
},
{
  "spec_key": "has_uart",
  "display_name": "UART",
  "data_type": "boolean",
  "sort_order": 7
},
{
  "spec_key": "has_spi",
  "display_name": "SPI",
  "data_type": "boolean",
  "sort_order": 8
},
{
  "spec_key": "has_i2c",
  "display_name": "I2C",
  "data_type": "boolean",
  "sort_order": 9
},
{
  "spec_key": "has_usb",
  "display_name": "USB",
  "data_type": "boolean",
  "sort_order": 10
},
{
  "spec_key": "has_can",
  "display_name": "CAN",
  "data_type": "boolean",
  "sort_order": 11
}
```

**CPU** — add after existing 5 specs (sort_order 6):
```json
{
  "spec_key": "family",
  "display_name": "Family",
  "data_type": "enum",
  "enum_values": ["Xeon", "Core i-series", "Ryzen", "EPYC", "Threadripper", "Atom", "ARM"],
  "sort_order": 6
}
```

**Flash** — add after existing 3 specs (sort_order 4-5):
```json
{
  "spec_key": "voltage",
  "display_name": "Voltage",
  "data_type": "numeric",
  "unit": "V",
  "canonical_unit": "V",
  "numeric_range": {"min": 1.2, "max": 5.0},
  "sort_order": 4
},
{
  "spec_key": "flash_form_factor",
  "display_name": "Form Factor",
  "data_type": "enum",
  "enum_values": ["DIP", "TSOP", "BGA", "WSON", "SOIC"],
  "sort_order": 5
}
```

**SSD** — add after existing 4 specs (sort_order 5-6):
```json
{
  "spec_key": "write_speed_mbps",
  "display_name": "Write Speed (MB/s)",
  "data_type": "numeric",
  "unit": "MB/s",
  "canonical_unit": "MB/s",
  "numeric_range": {"min": 100, "max": 7500},
  "sort_order": 5
},
{
  "spec_key": "nand_type",
  "display_name": "NAND Type",
  "data_type": "enum",
  "enum_values": ["SLC", "MLC", "TLC", "QLC", "PLC"],
  "sort_order": 6
}
```

**Connectors** — add after existing 5 specs (sort_order 6):
```json
{
  "spec_key": "connector_type",
  "display_name": "Connector Type",
  "data_type": "enum",
  "enum_values": ["USB", "RJ45", "HDMI", "PCIe", "D-Sub", "JST", "Molex", "FPC/FFC", "M.2", "SATA", "SAS"],
  "sort_order": 6
}
```

**Power Supplies** — add after existing 3 specs (sort_order 4-6):
```json
{
  "spec_key": "input_voltage",
  "display_name": "Input Voltage",
  "data_type": "numeric",
  "unit": "V",
  "canonical_unit": "V",
  "numeric_range": {"min": 90, "max": 480},
  "sort_order": 4
},
{
  "spec_key": "output_voltage",
  "display_name": "Output Voltage",
  "data_type": "numeric",
  "unit": "V",
  "canonical_unit": "V",
  "numeric_range": {"min": 1.0, "max": 48.0},
  "sort_order": 5
},
{
  "spec_key": "psu_connector_type",
  "display_name": "Connector Type",
  "data_type": "enum",
  "enum_values": ["ATX 24-pin", "EPS 8-pin", "PCIe 6-pin", "PCIe 8-pin", "Barrel", "Molex", "SATA"],
  "sort_order": 6
}
```

**Motherboards** — add after existing 4 specs (sort_order 5-6):
```json
{
  "spec_key": "max_memory_gb",
  "display_name": "Max Memory (GB)",
  "data_type": "numeric",
  "unit": "GB",
  "canonical_unit": "GB",
  "numeric_range": {"min": 8, "max": 6144},
  "sort_order": 5
},
{
  "spec_key": "pcie_gen",
  "display_name": "PCIe Generation",
  "data_type": "enum",
  "enum_values": ["Gen3", "Gen4", "Gen5"],
  "sort_order": 6
}
```

**Network Cards** — add after existing 4 specs (sort_order 5):
```json
{
  "spec_key": "media_type",
  "display_name": "Media Type",
  "data_type": "enum",
  "enum_values": ["Copper", "Fiber", "Copper/Fiber"],
  "sort_order": 5
}
```

**GPU** — add after existing 3 specs (sort_order 4-5):
```json
{
  "spec_key": "gpu_family",
  "display_name": "Family",
  "data_type": "enum",
  "enum_values": ["GeForce", "Quadro", "RTX", "Radeon", "Radeon Pro", "Tesla", "A-series", "H-series"],
  "sort_order": 4
},
{
  "spec_key": "tdp_watts",
  "display_name": "TDP (W)",
  "data_type": "numeric",
  "unit": "W",
  "canonical_unit": "W",
  "numeric_range": {"min": 25, "max": 700},
  "sort_order": 5
}
```

**No changes needed for:** DRAM (already 5 specs), HDD (already 4 specs — covers capacity, RPM, form factor, interface).

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_commodity_registry.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/data/commodity_seeds.json tests/test_commodity_registry.py
git commit -m "feat: expand commodity specs with sourcing-relevant parameters"
```

---

### Task 2: Alembic Migration for Manufacturer + Lifecycle Indexes

**Files:**
- Create: `alembic/versions/XXX_add_manufacturer_lifecycle_indexes.py` (via autogenerate)
- Modify: `app/models/intelligence.py` (add index declarations)

- [ ] **Step 1: Add index declarations to MaterialCard model**

In `app/models/intelligence.py`, add `index=True` to the `manufacturer` and `lifecycle_status` column definitions on `MaterialCard`:

```python
manufacturer = Column(String(255), index=True)
lifecycle_status = Column(String(50), index=True)
```

These columns currently lack `index=True` — this step is mandatory.

- [ ] **Step 2: Generate Alembic migration**

Run inside Docker:
```bash
docker compose exec app alembic revision --autogenerate -m "add manufacturer and lifecycle status indexes"
```

- [ ] **Step 3: Review the generated migration**

Verify it only contains `CREATE INDEX` for `manufacturer` and `lifecycle_status`. No other changes.

- [ ] **Step 4: Test migration up/down**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

- [ ] **Step 5: Commit**

```bash
git add app/models/intelligence.py alembic/versions/
git commit -m "feat: add indexes on manufacturer and lifecycle_status for filter queries"
```

---

### Task 3: Manufacturer Global Filter — Backend

**Files:**
- Modify: `app/services/faceted_search_service.py`
- Modify: `app/routers/htmx_views.py`
- Test: `tests/test_faceted_search_service.py`
- Test: `tests/test_faceted_routes.py`

- [ ] **Step 1: Write test for get_manufacturer_options**

Add to `tests/test_faceted_search_service.py`:

```python
def test_get_manufacturer_options_returns_sorted_list(db_session):
    """get_manufacturer_options returns distinct manufacturers sorted by count."""
    from app.services.faceted_search_service import get_manufacturer_options

    db_session.add(MaterialCard(normalized_mpn="a", display_mpn="A", manufacturer="Texas Instruments", category="resistors"))
    db_session.add(MaterialCard(normalized_mpn="b", display_mpn="B", manufacturer="Texas Instruments", category="resistors"))
    db_session.add(MaterialCard(normalized_mpn="c", display_mpn="C", manufacturer="Murata", category="capacitors"))
    db_session.flush()

    result = get_manufacturer_options(db_session)
    assert len(result) == 2
    # TI has 2 cards, should be first
    assert result[0]["name"] == "Texas Instruments"
    assert result[0]["count"] == 2
    assert result[1]["name"] == "Murata"


def test_get_manufacturer_options_scoped_to_commodity(db_session):
    """When commodity is given, only return manufacturers in that commodity."""
    from app.services.faceted_search_service import get_manufacturer_options

    db_session.add(MaterialCard(normalized_mpn="a", display_mpn="A", manufacturer="TI", category="resistors"))
    db_session.add(MaterialCard(normalized_mpn="b", display_mpn="B", manufacturer="Murata", category="capacitors"))
    db_session.flush()

    result = get_manufacturer_options(db_session, commodity="resistors")
    assert len(result) == 1
    assert result[0]["name"] == "TI"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_search_service.py::test_get_manufacturer_options_returns_sorted_list tests/test_faceted_search_service.py::test_get_manufacturer_options_scoped_to_commodity -v`
Expected: FAIL — `get_manufacturer_options` doesn't exist

- [ ] **Step 3: Implement get_manufacturer_options**

Add to `app/services/faceted_search_service.py`:

```python
def get_manufacturer_options(
    db: Session,
    commodity: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return distinct manufacturers sorted by card count (descending).

    Args:
        commodity: If set, scope to this commodity only.
        limit: Max results to return (default 20 per spec).

    Returns: [{"name": str, "count": int}, ...]
    """
    query = db.query(
        MaterialCard.manufacturer,
        func.count(MaterialCard.id).label("cnt"),
    ).filter(
        MaterialCard.deleted_at.is_(None),
        MaterialCard.manufacturer.isnot(None),
        MaterialCard.manufacturer != "",
    )

    if commodity:
        query = query.filter(
            func.lower(func.trim(MaterialCard.category)) == commodity.lower().strip()
        )

    rows = (
        query.group_by(MaterialCard.manufacturer)
        .order_by(func.count(MaterialCard.id).desc())
        .limit(limit)
        .all()
    )
    return [{"name": name, "count": count} for name, count in rows]
```

- [ ] **Step 4: Write test for manufacturer filter in search_materials_faceted**

Add to `tests/test_faceted_search_service.py`:

```python
def test_search_faceted_filters_by_manufacturer(db_session):
    """search_materials_faceted respects manufacturer filter."""
    from app.services.faceted_search_service import search_materials_faceted

    db_session.add(MaterialCard(normalized_mpn="a", display_mpn="A", manufacturer="TI", category="resistors"))
    db_session.add(MaterialCard(normalized_mpn="b", display_mpn="B", manufacturer="Murata", category="resistors"))
    db_session.flush()

    results, total = search_materials_faceted(db_session, commodity="resistors", manufacturers=["TI"])
    assert total == 1
    assert results[0].manufacturer == "TI"
```

- [ ] **Step 5: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_search_service.py::test_search_faceted_filters_by_manufacturer -v`
Expected: FAIL — `manufacturers` param doesn't exist

- [ ] **Step 6: Add manufacturers param to search_materials_faceted**

Modify `app/services/faceted_search_service.py`, update `search_materials_faceted` signature and add filter:

```python
def search_materials_faceted(
    db: Session,
    *,
    commodity: str | None = None,
    q: str | None = None,
    sub_filters: dict | None = None,
    manufacturers: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MaterialCard], int]:
```

Add after the `q` filter block (after line 139), **before** the `if sub_filters and commodity:` block. This must be at the top level of the function, not nested inside the sub_filters block, since manufacturer filtering works globally (with or without a commodity selected):

```python
    if manufacturers:
        query = query.filter(MaterialCard.manufacturer.in_(manufacturers))
```

- [ ] **Step 7: Run all faceted search tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_search_service.py -v`
Expected: ALL PASS

- [ ] **Step 8: Update htmx_views.py to pass manufacturers param**

In `app/routers/htmx_views.py`, update `materials_faceted_partial` (line ~6168) to accept and parse `manufacturers` from the sub_filters JSON and pass to `search_materials_faceted`:

In the parsed_filters handling, extract `manufacturers` key if present:

```python
    manufacturers = None
    if parsed_filters and "manufacturers" in parsed_filters:
        mfr_val = parsed_filters.pop("manufacturers")
        manufacturers = mfr_val if isinstance(mfr_val, list) else [mfr_val]

    materials, total = search_materials_faceted(
        db,
        commodity=commodity or None,
        q=q or None,
        sub_filters=parsed_filters or None,
        manufacturers=manufacturers,
        limit=limit,
        offset=offset,
    )
```

Also update `materials_workspace_partial` to pass manufacturer options:

```python
    from ..services.faceted_search_service import get_manufacturer_options
    ctx["manufacturer_options"] = get_manufacturer_options(db)
```

And update `materials_filters_sub_partial` to pass manufacturer options scoped to commodity:

```python
    from ..services.faceted_search_service import get_manufacturer_options
    ctx["manufacturer_options"] = get_manufacturer_options(db, commodity=commodity)
```

- [ ] **Step 9: Commit**

```bash
git add app/services/faceted_search_service.py app/routers/htmx_views.py tests/test_faceted_search_service.py
git commit -m "feat: add manufacturer filter to faceted search backend"
```

---

### Task 4: Manufacturer Global Filter — Frontend

**Files:**
- Modify: `app/templates/htmx/partials/materials/workspace.html`
- Modify: `app/templates/htmx/partials/materials/filters/subfilters.html`
- Modify: `app/static/htmx_app.js`

- [ ] **Step 1: Add manufacturer state to Alpine.js component**

In `app/static/htmx_app.js`, inside `materialsFilter` data (line ~318), the manufacturer filter values will be stored inside `subFilters` using the key `manufacturers` — this means the existing `toggleFilter` and `removeFilter` methods already handle it. No changes to Alpine state needed.

The manufacturers will be passed as `subFilters.manufacturers = ["TI", "Murata"]` which gets serialized into the `sub_filters` JSON param automatically.

- [ ] **Step 2: Add manufacturer dropdown to workspace sidebar**

In `app/templates/htmx/partials/materials/workspace.html`, add a manufacturer filter section above the commodity tree (between line 31 and line 33, after the "Categories" header close div):

```html
      {# Manufacturer filter — always visible, loaded with sub-filters #}
      <div id="manufacturer-filter-container"
           hx-get="/v2/partials/materials/filters/manufacturers"
           hx-trigger="load, commodity-changed from:body"
           hx-vals='js:{"commodity": Alpine.evaluate(document.querySelector("#materials-workspace"), "commodity") || ""}'
           hx-swap="innerHTML">
      </div>
```

- [ ] **Step 3: Create manufacturer filter partial template**

Create `app/templates/htmx/partials/materials/filters/manufacturers.html`:

```html
{# Manufacturer type-ahead multi-select filter.
   Called by: workspace.html manufacturer-filter-container
   Depends on: manufacturer_options from htmx_views
#}
{% if manufacturer_options %}
<div class="mb-3" x-data="{mfgSearch: '', mfgExpanded: false}">
  <h4 class="text-[10px] font-semibold text-gray-400 uppercase tracking-widest mb-1.5">Manufacturer</h4>
  <input type="text" x-model="mfgSearch"
         @focus="mfgExpanded = true"
         placeholder="Search manufacturers..."
         class="w-full px-2 py-1 text-xs border border-gray-200 rounded focus:ring-1 focus:ring-brand-500 focus:border-brand-500 mb-1">
  <div class="max-h-40 overflow-y-auto space-y-0.5" x-show="mfgExpanded || (subFilters.manufacturers && subFilters.manufacturers.length > 0)">
    {% for mfr in manufacturer_options %}
    <label x-data="{name: {{ mfr.name | tojson }}}"
           x-show="!mfgSearch || name.toLowerCase().includes(mfgSearch.toLowerCase())"
           class="flex items-center gap-2 px-1 py-0.5 rounded hover:bg-gray-50 cursor-pointer text-xs">
      <input type="checkbox"
             :checked="(subFilters.manufacturers || []).includes(name)"
             @change="toggleFilter('manufacturers', name)"
             class="rounded border-gray-300 text-brand-500 focus:ring-brand-500 h-3 w-3">
      <span class="truncate flex-1" x-text="name"></span>
      <span class="text-gray-400 text-[10px]">{{ mfr.count }}</span>
    </label>
    {% endfor %}
  </div>
</div>
{% endif %}
```

- [ ] **Step 4: Add the manufacturers HTMX route**

In `app/routers/htmx_views.py`, add a new route for the manufacturer filter partial:

```python
@router.get("/v2/partials/materials/filters/manufacturers", response_class=HTMLResponse)
async def materials_filters_manufacturers_partial(
    request: Request,
    commodity: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render manufacturer filter dropdown."""
    from ..services.faceted_search_service import get_manufacturer_options

    options = get_manufacturer_options(db, commodity=commodity or None)
    ctx = _base_ctx(request, user, "materials")
    ctx["manufacturer_options"] = options
    return templates.TemplateResponse(
        "htmx/partials/materials/filters/manufacturers.html", ctx
    )
```

- [ ] **Step 5: Write route test**

Add to `tests/test_faceted_routes.py`:

```python
def test_manufacturer_filter_partial_renders(client):
    resp = client.get("/v2/partials/materials/filters/manufacturers")
    assert resp.status_code == 200
```

- [ ] **Step 6: Run all faceted route tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_routes.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add app/templates/htmx/partials/materials/workspace.html app/templates/htmx/partials/materials/filters/manufacturers.html app/static/htmx_app.js app/routers/htmx_views.py tests/test_faceted_routes.py
git commit -m "feat: add manufacturer type-ahead filter to materials sidebar"
```

---

### Task 5: Lifecycle Auto-Detection — Enrichment Integration

**Files:**
- Modify: `app/services/material_enrichment_service.py`
- Test: `tests/test_material_enrichment.py` (or create if doesn't exist)

- [ ] **Step 1: Write test for lifecycle extraction during enrichment**

```python
def test_enrichment_sets_lifecycle_status(db_session):
    """Enrichment should set lifecycle_status from AI response."""
    card = MaterialCard(normalized_mpn="test123", display_mpn="TEST123")
    db_session.add(card)
    db_session.flush()

    # Simulate AI response with lifecycle
    from app.services.material_enrichment_service import _apply_enrichment_result
    ai_result = {
        "mpn": "TEST123",
        "description": "Test component",
        "category": "resistors",
        "lifecycle_status": "active",
    }
    _apply_enrichment_result(card, ai_result)

    assert card.lifecycle_status == "active"
    assert card.category == "resistors"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_material_enrichment.py::test_enrichment_sets_lifecycle_status -v`
Expected: FAIL — `_apply_enrichment_result` doesn't exist or doesn't handle lifecycle

- [ ] **Step 3: Update enrichment prompt and schema**

In `app/services/material_enrichment_service.py`:

Update `_SYSTEM_PROMPT` to include lifecycle:

```python
_SYSTEM_PROMPT = (
    "You are an expert electronic component engineer. "
    "Given a manufacturer part number (MPN) and optional manufacturer name, "
    "generate a concise technical description, classify the component into "
    "the correct commodity category, and assess its lifecycle status.\n\n"
    "Rules:\n"
    "- description: 1-2 sentences describing what the part is, key specs if inferable from the MPN.\n"
    "- category: choose from the provided list. Use 'other' only if no category fits.\n"
    "- lifecycle_status: one of 'active', 'eol', 'obsolete', 'nrfnd', 'ltb'. Use 'active' if unknown.\n"
    "- If you cannot identify the part at all, set description to null and category to 'other'.\n"
    "- Do NOT hallucinate specs — only include what you can confidently infer from the MPN."
)
```

Update `_PART_SCHEMA` to include lifecycle:

```python
"lifecycle_status": {
    "type": "string",
    "enum": ["active", "eol", "obsolete", "nrfnd", "ltb"]
},
```

Add it to the `"required"` list in the item schema.

- [ ] **Step 4: Extract _apply_enrichment_result and handle lifecycle**

Refactor the enrichment application logic in `_enrich_batch` (lines 125-141) into a standalone function:

```python
VALID_LIFECYCLE = {"active", "eol", "obsolete", "nrfnd", "ltb"}

def _apply_enrichment_result(card: MaterialCard, ai: dict) -> None:
    """Apply AI enrichment result to a MaterialCard."""
    desc = ai.get("description")
    cat = ai.get("category", "other")
    lifecycle = ai.get("lifecycle_status", "active")

    if cat not in VALID_CATEGORIES:
        cat = "other"
    if lifecycle not in VALID_LIFECYCLE:
        lifecycle = "active"

    if desc:
        card.description = desc
    card.category = cat
    card.lifecycle_status = lifecycle
    card.enrichment_source = "claude_haiku"
    card.enriched_at = datetime.now(timezone.utc)
```

Update `_enrich_batch` to call `_apply_enrichment_result(card, ai)` instead of inline logic.
Do the same for `process_material_batch_results`.

- [ ] **Step 5: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_material_enrichment.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/material_enrichment_service.py tests/test_material_enrichment.py
git commit -m "feat: add lifecycle status detection to enrichment prompt"
```

---

### Task 6: Lifecycle Sweep Job

**Files:**
- Create: `app/jobs/lifecycle_jobs.py`
- Modify: `app/jobs/__init__.py`
- Test: `tests/test_lifecycle_jobs.py`

- [ ] **Step 1: Write test for lifecycle sweep logic**

Create `tests/test_lifecycle_jobs.py`:

```python
"""Tests for lifecycle sweep job."""

from app.models import MaterialCard


def test_lifecycle_sweep_finds_active_cards(db_session):
    """Sweep should query cards marked active."""
    from app.jobs.lifecycle_jobs import get_cards_for_lifecycle_check

    db_session.add(MaterialCard(normalized_mpn="a", display_mpn="A", lifecycle_status="active"))
    db_session.add(MaterialCard(normalized_mpn="b", display_mpn="B", lifecycle_status="obsolete"))
    db_session.add(MaterialCard(normalized_mpn="c", display_mpn="C", lifecycle_status=None))
    db_session.flush()

    cards = get_cards_for_lifecycle_check(db_session)
    # Should include active and None (unknown), not obsolete
    mpns = {c.normalized_mpn for c in cards}
    assert "a" in mpns
    assert "c" in mpns
    assert "b" not in mpns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_jobs.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement lifecycle_jobs.py**

Create `app/jobs/lifecycle_jobs.py`:

```python
"""Lifecycle sweep job — checks active parts for EOL/obsolete status.

Called by: scheduler via register_lifecycle_jobs()
Depends on: MaterialCard, Nexar/DigiKey connectors for lifecycle data
"""

from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy.orm import Session

from ..models import MaterialCard
from ..scheduler import _traced_job


def get_cards_for_lifecycle_check(
    db: Session,
    *,
    limit: int = 200,
) -> list[MaterialCard]:
    """Get cards that need lifecycle status verification.

    Returns active or unknown-status cards, oldest-checked first.
    """
    return (
        db.query(MaterialCard)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.lifecycle_status.in_(["active", None]),
        )
        .order_by(MaterialCard.enriched_at.asc().nullsfirst())
        .limit(limit)
        .all()
    )


def register_lifecycle_jobs(scheduler, settings):
    """Register lifecycle sweep as a weekly job."""
    scheduler.add_job(
        _job_lifecycle_sweep,
        CronTrigger(day_of_week="sun", hour=2, minute=0),
        id="lifecycle_sweep",
        name="Weekly lifecycle status sweep",
        replace_existing=True,
    )
    logger.info("Registered lifecycle sweep job (Sundays 2:00 AM)")


@_traced_job
async def _job_lifecycle_sweep():
    """Check lifecycle status on active/unknown parts via enrichment."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        cards = get_cards_for_lifecycle_check(db)
        if not cards:
            logger.info("lifecycle_sweep: no cards to check")
            return

        card_ids = [c.id for c in cards]
        logger.info("lifecycle_sweep: checking %d cards", len(card_ids))

        from ..services.material_enrichment_service import enrich_material_cards

        stats = await enrich_material_cards(card_ids, db)
        logger.info("lifecycle_sweep: %s", stats)
    except Exception:
        logger.exception("lifecycle_sweep failed")
        db.rollback()
    finally:
        db.close()
```

- [ ] **Step 4: Register in jobs/__init__.py**

In `app/jobs/__init__.py`, add:

```python
from .lifecycle_jobs import register_lifecycle_jobs
```

And call `register_lifecycle_jobs(scheduler, settings)` in `register_all_jobs()`.

- [ ] **Step 5: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lifecycle_jobs.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add app/jobs/lifecycle_jobs.py app/jobs/__init__.py tests/test_lifecycle_jobs.py
git commit -m "feat: add weekly lifecycle sweep job for active part status checks"
```

---

### Task 7: Bulk Re-Enrichment Command

**Files:**
- Create: `app/management/reenrich.py`
- Test: manual verification (management command)

- [ ] **Step 1: Create re-enrichment management script**

Create `app/management/reenrich.py`:

```python
"""Bulk re-enrichment command — re-enriches all material cards to populate new specs.

Usage: python -m app.management.reenrich [--limit N] [--batch-size N]

Called by: admin manually after deploying new commodity specs
Depends on: material_enrichment_service.enrich_material_cards
"""

import argparse
import asyncio

from loguru import logger


async def main(limit: int = 500, batch_size: int = 30):
    """Re-enrich material cards in batches."""
    from app.database import SessionLocal
    from app.models import MaterialCard
    from app.services.material_enrichment_service import enrich_material_cards

    db = SessionLocal()
    try:
        cards = (
            db.query(MaterialCard.id)
            .filter(MaterialCard.deleted_at.is_(None))
            .order_by(MaterialCard.enriched_at.asc().nullsfirst())
            .limit(limit)
            .all()
        )
        card_ids = [c[0] for c in cards]
        logger.info("Re-enriching %d cards (limit=%d, batch_size=%d)", len(card_ids), limit, batch_size)

        stats = await enrich_material_cards(card_ids, db, batch_size=batch_size)
        logger.info("Re-enrichment complete: %s", stats)

        # Backfill MaterialSpecFacet rows from updated specs_structured
        from app.services.spec_write_service import record_spec

        enriched_cards = db.query(MaterialCard).filter(MaterialCard.id.in_(card_ids)).all()
        facet_count = 0
        for card in enriched_cards:
            if not card.specs_structured or not card.category:
                continue
            for spec_key, spec_data in card.specs_structured.items():
                value = spec_data.get("value") if isinstance(spec_data, dict) else spec_data
                if value is not None:
                    record_spec(
                        db, card.id, spec_key, value,
                        source=card.enrichment_source or "reenrich",
                        confidence=0.85,
                    )
                    facet_count += 1
        db.commit()
        logger.info("Backfilled %d facet rows", facet_count)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk re-enrich material cards")
    parser.add_argument("--limit", type=int, default=500, help="Max cards to re-enrich")
    parser.add_argument("--batch-size", type=int, default=30, help="Cards per AI batch call")
    args = parser.parse_args()

    asyncio.run(main(limit=args.limit, batch_size=args.batch_size))
```

- [ ] **Step 2: Commit**

```bash
git add app/management/reenrich.py
git commit -m "feat: add bulk re-enrichment management command"
```

---

### Task 8: Full Test Suite + Deploy

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

Expected: ALL PASS

- [ ] **Step 2: Seed new specs on startup**

Verify that `seed_commodity_schemas()` in `app/startup.py` is called on app boot — it already is (from SP1-3 work). The function auto-detects and inserts missing `CommoditySpecSchema` rows, so deploying the updated `commodity_seeds.json` will automatically create the new spec definitions.

- [ ] **Step 3: Commit any remaining changes and push**

```bash
git push origin main
```

- [ ] **Step 4: Deploy**

```bash
cd /root/availai && docker compose up -d --build
```

Check logs:
```bash
docker compose logs -f app 2>&1 | head -50
```

Verify: seed log should show new specs being inserted.

- [ ] **Step 5: Run bulk re-enrichment (optional, can be deferred)**

```bash
docker compose exec app python -m app.management.reenrich --limit 100 --batch-size 20
```
