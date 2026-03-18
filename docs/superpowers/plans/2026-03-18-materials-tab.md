# Materials Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-class "Materials" tab to the bottom navigation with AI-powered search, full-page detail view with vendor/customer/sourcing/price history tabs, and price snapshot tracking.

**Architecture:** Enhance existing MaterialCard templates and routes. Add Haiku-powered semantic search that auto-routes based on query type. New MaterialPriceSnapshot table records price changes for historical tracking. Bottom nav flattened to 10 visible tabs (no More menu).

**Tech Stack:** FastAPI, SQLAlchemy (Column style), PostgreSQL, Jinja2, HTMX, Alpine.js, Anthropic Haiku API, Alembic

**Spec:** `docs/superpowers/specs/2026-03-18-materials-tab-design.md`

---

## File Structure

### New Files
- `app/services/material_search_service.py` — AI search routing (MPN vs Haiku) and query interpretation
- `app/models/price_snapshot.py` — MaterialPriceSnapshot model
- `app/services/price_snapshot_service.py` — `record_price_snapshot()` function
- `app/templates/htmx/partials/materials/tabs/vendors.html` — Vendors tab partial
- `app/templates/htmx/partials/materials/tabs/customers.html` — Customers tab partial
- `app/templates/htmx/partials/materials/tabs/sourcing.html` — Sourcing tab partial
- `app/templates/htmx/partials/materials/tabs/price_history.html` — Price History tab partial
- `tests/test_material_search_service.py` — AI search tests
- `tests/test_price_snapshot.py` — Price snapshot tests
- `tests/test_materials_tab.py` — Materials tab route + template tests

### Modified Files
- `app/templates/htmx/base.html:114-123` — Flatten nav to 10 tabs, add Materials
- `app/templates/htmx/partials/materials/list.html` — Replace columns, add command-style search bar, match Reqs density
- `app/templates/htmx/partials/materials/detail.html` — Hero header, collapsible specs, lazy-load tab structure
- `app/routers/htmx_views.py:5705-5784` — Enhance list route (vendor_count, best_price), add tab routes
- `app/models/__init__.py:46-57` — Export MaterialPriceSnapshot
- `app/routers/materials.py:457-480` — Call record_price_snapshot on stock import
- `app/search_service.py:1420-1445` — Call record_price_snapshot on search sightings
- `app/jobs/inventory_jobs.py:280-302` — Call record_price_snapshot on email imports
- `app/services/material_card_service.py:228-247` — Call record_price_snapshot on merge

---

### Task 1: MaterialPriceSnapshot Model + Migration

**Files:**
- Create: `app/models/price_snapshot.py`
- Modify: `app/models/__init__.py`
- Test: `tests/test_price_snapshot.py`

- [ ] **Step 1: Write the model test**

```python
# tests/test_price_snapshot.py
"""Tests for MaterialPriceSnapshot model and service."""
from datetime import datetime, timezone
from app.models.price_snapshot import MaterialPriceSnapshot


def test_price_snapshot_creation(db_session):
    """Verify MaterialPriceSnapshot can be created with all fields."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="TEST-MPN-001", display_mpn="TEST-MPN-001")
    db_session.add(card)
    db_session.flush()

    snap = MaterialPriceSnapshot(
        material_card_id=card.id,
        vendor_name="Test Vendor",
        price=12.50,
        currency="USD",
        quantity=100,
        source="api_sighting",
        recorded_at=datetime.now(timezone.utc),
    )
    db_session.add(snap)
    db_session.commit()

    saved = db_session.query(MaterialPriceSnapshot).first()
    assert saved.price == 12.50
    assert saved.vendor_name == "Test Vendor"
    assert saved.material_card_id == card.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_price_snapshot.py::test_price_snapshot_creation -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.price_snapshot'`

- [ ] **Step 3: Create the model**

```python
# app/models/price_snapshot.py
"""
MaterialPriceSnapshot — records price observations over time for trend tracking.
Called by: price_snapshot_service.record_price_snapshot()
Depends on: MaterialCard (FK)
"""
from sqlalchemy import Column, Integer, Float, String, ForeignKey
from sqlalchemy.sql import func

from app.database import Base, UTCTimestamp


class MaterialPriceSnapshot(Base):
    __tablename__ = "material_price_snapshots"

    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, ForeignKey("material_cards.id"), index=True, nullable=False)
    vendor_name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String(3), default="USD")
    quantity = Column(Integer, nullable=True)
    source = Column(String(50), nullable=False)
    recorded_at = Column(UTCTimestamp, server_default=func.now(), index=True)
```

- [ ] **Step 4: Add to model exports**

In `app/models/__init__.py`, add to the intelligence imports block:
```python
from .price_snapshot import MaterialPriceSnapshot
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_price_snapshot.py::test_price_snapshot_creation -v`
Expected: PASS

- [ ] **Step 6: Generate Alembic migration**

Run: `cd /root/availai && docker compose exec app alembic revision --autogenerate -m "add material_price_snapshots table"`
Review the generated migration. Verify it creates the table with correct columns and indexes.

- [ ] **Step 7: Commit**

```bash
git add app/models/price_snapshot.py app/models/__init__.py tests/test_price_snapshot.py alembic/versions/
git commit -m "feat: add MaterialPriceSnapshot model and migration"
```

---

### Task 2: Price Snapshot Service

**Files:**
- Create: `app/services/price_snapshot_service.py`
- Test: `tests/test_price_snapshot.py` (extend)

- [ ] **Step 1: Write the service test**

Add to `tests/test_price_snapshot.py`:

```python
from app.services.price_snapshot_service import record_price_snapshot


def test_record_price_snapshot(db_session):
    """Verify record_price_snapshot creates a snapshot row."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="SNAP-001", display_mpn="SNAP-001")
    db_session.add(card)
    db_session.flush()

    record_price_snapshot(
        db=db_session,
        material_card_id=card.id,
        vendor_name="Mouser",
        price=5.25,
        currency="USD",
        quantity=500,
        source="api_sighting",
    )

    snaps = db_session.query(MaterialPriceSnapshot).filter_by(material_card_id=card.id).all()
    assert len(snaps) == 1
    assert snaps[0].price == 5.25
    assert snaps[0].vendor_name == "Mouser"


def test_record_price_snapshot_skips_none_price(db_session):
    """Verify no snapshot created when price is None."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="SNAP-002", display_mpn="SNAP-002")
    db_session.add(card)
    db_session.flush()

    record_price_snapshot(
        db=db_session,
        material_card_id=card.id,
        vendor_name="DigiKey",
        price=None,
        currency="USD",
        quantity=100,
        source="api_sighting",
    )

    snaps = db_session.query(MaterialPriceSnapshot).filter_by(material_card_id=card.id).all()
    assert len(snaps) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_price_snapshot.py -v -k "record"`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the service**

```python
# app/services/price_snapshot_service.py
"""
Price snapshot recording service.
Called by: search_service, materials router (stock import), inventory_jobs, material_card_service (merge).
Depends on: MaterialPriceSnapshot model.
"""
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models.price_snapshot import MaterialPriceSnapshot


def record_price_snapshot(
    db: Session,
    material_card_id: int,
    vendor_name: str,
    price: float | None,
    currency: str = "USD",
    quantity: int | None = None,
    source: str = "api_sighting",
) -> None:
    """Record a price observation. Skips if price is None."""
    if price is None:
        return

    snap = MaterialPriceSnapshot(
        material_card_id=material_card_id,
        vendor_name=vendor_name,
        price=price,
        currency=currency,
        quantity=quantity,
        source=source,
        recorded_at=datetime.now(timezone.utc),
    )
    db.add(snap)
    logger.debug(f"Price snapshot: card={material_card_id} vendor={vendor_name} price={price}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_price_snapshot.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/price_snapshot_service.py tests/test_price_snapshot.py
git commit -m "feat: add price snapshot recording service"
```

---

### Task 3: Wire Price Snapshots into Existing Flows

**Files:**
- Modify: `app/routers/materials.py:457-480`
- Modify: `app/search_service.py:1420-1445`
- Modify: `app/jobs/inventory_jobs.py:280-302`
- Modify: `app/services/material_card_service.py:228-247`

- [ ] **Step 1: Add snapshot call to stock import** (`app/routers/materials.py`)

After each `mvh.last_price = parsed["price"]` and after creating new MVH rows with `last_price`, add:
```python
from app.services.price_snapshot_service import record_price_snapshot

# After updating existing MVH:
record_price_snapshot(db=db, material_card_id=card.id, vendor_name=norm_vendor, price=parsed.get("price"), source="stock_list")

# After creating new MVH:
record_price_snapshot(db=db, material_card_id=card.id, vendor_name=norm_vendor, price=parsed.get("price"), source="stock_list")
```

- [ ] **Step 2: Add snapshot call to search sightings** (`app/search_service.py`)

After each `vh.last_price = s.unit_price` and after creating new MVH rows, add:
```python
from app.services.price_snapshot_service import record_price_snapshot

record_price_snapshot(db=db, material_card_id=card.id, vendor_name=s.vendor_name, price=s.unit_price, currency=s.currency or "USD", quantity=s.qty_available, source="api_sighting")
```

- [ ] **Step 3: Add snapshot call to email imports** (`app/jobs/inventory_jobs.py`)

After each `mvh.last_price` assignment and after creating new MVH rows, add:
```python
from app.services.price_snapshot_service import record_price_snapshot

price = row.get("unit_price") or row.get("price")
record_price_snapshot(db=db, material_card_id=card.id, vendor_name=norm_vendor, price=price, source="email_auto_import")
```

- [ ] **Step 4: Add snapshot call to merge** (`app/services/material_card_service.py`)

After `tvh.last_price = svh.last_price`, add:
```python
from app.services.price_snapshot_service import record_price_snapshot

record_price_snapshot(db=db, material_card_id=target_id, vendor_name=tvh.vendor_name, price=svh.last_price, source="merge")
```

- [ ] **Step 5: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: ALL PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add app/routers/materials.py app/search_service.py app/jobs/inventory_jobs.py app/services/material_card_service.py
git commit -m "feat: wire price snapshot recording into all MVH update flows"
```

---

### Task 4: Bottom Nav — Flatten to 10 Tabs

**Files:**
- Modify: `app/templates/htmx/base.html:114-123`

- [ ] **Step 1: Add Materials to bottom_items and remove More menu**

Replace the `bottom_items` array (lines 114-123 of `app/templates/htmx/base.html`) with all 10 tabs flat. Insert Materials at position 5 (after Vendors, before Companies). Use a chip/component SVG icon.

Materials tuple:
```python
('materials', 'Materials', '/v2/materials', '/v2/partials/materials', 'M9 3v2m6-2v2M9 19v2m6-2v2M3 9h2m-2 6h2m14-6h2m-2 6h2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z')
```

Remove any "More" dropdown markup and JS. All items in `bottom_items`, no overflow.

- [ ] **Step 2: Verify nav renders in browser**

Hard refresh the app and verify all 10 tabs appear, Materials is at position 5, clicking it loads the materials list.

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/base.html
git commit -m "feat: flatten bottom nav to 10 visible tabs, add Materials tab"
```

---

### Task 5: AI Search Service (MPN vs Haiku Routing)

**Files:**
- Create: `app/services/material_search_service.py`
- Test: `tests/test_material_search_service.py`

- [ ] **Step 1: Write tests for query classification**

```python
# tests/test_material_search_service.py
"""Tests for material search routing (MPN vs natural language)."""
from app.services.material_search_service import classify_query


def test_classify_mpn_simple():
    assert classify_query("LM358DR") == "mpn"


def test_classify_mpn_with_dashes():
    assert classify_query("RC0805FR-07100KL") == "mpn"


def test_classify_mpn_two_words():
    assert classify_query("STM32 F407") == "mpn"


def test_classify_natural_language():
    assert classify_query("DDR5 memory 16GB") == "natural_language"


def test_classify_natural_language_description():
    assert classify_query("UHD LCD panel for automotive") == "natural_language"


def test_classify_empty():
    assert classify_query("") == "mpn"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_material_search_service.py -v`
Expected: FAIL

- [ ] **Step 3: Write the search service**

```python
# app/services/material_search_service.py
"""
AI-powered material search routing.
Classifies queries as MPN (local search) or natural language (Haiku interpretation).
Called by: htmx_views materials_list_partial route.
Depends on: Anthropic API (Haiku), MaterialCard model.
"""
import anthropic
from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.config import settings
from app.models.intelligence import MaterialCard


def classify_query(query: str) -> str:
    """Classify a search query as 'mpn' or 'natural_language'.

    Rule: 3+ whitespace-separated words = natural language, otherwise MPN.
    """
    words = query.strip().split()
    if len(words) >= 3:
        return "natural_language"
    return "mpn"


def search_materials_local(db: Session, query: str, lifecycle: str = "", limit: int = 50, offset: int = 0):
    """Search MaterialCards using local trigram + full-text search."""
    q = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))

    if query:
        pattern = f"%{query}%"
        q = q.filter(
            or_(
                MaterialCard.normalized_mpn.ilike(pattern),
                MaterialCard.display_mpn.ilike(pattern),
                MaterialCard.manufacturer.ilike(pattern),
                MaterialCard.description.ilike(pattern),
            )
        )

    if lifecycle:
        q = q.filter(MaterialCard.lifecycle_status == lifecycle)

    total = q.count()
    materials = q.order_by(MaterialCard.search_count.desc(), MaterialCard.created_at.desc()).offset(offset).limit(limit).all()
    return materials, total


async def interpret_with_haiku(query: str) -> dict:
    """Send natural language query to Claude Haiku for interpretation.

    Returns dict with keys: keywords, category, description_terms.
    """
    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": f"""Interpret this as an electronic component search query. Extract search terms I can use to find matching parts in a database.

Query: "{query}"

Reply with ONLY a JSON object (no markdown, no explanation):
{{"keywords": ["term1", "term2"], "category": "category or empty string", "description_terms": ["phrase1", "phrase2"]}}"""
            }],
        )
        import json
        text = response.content[0].text.strip()
        return json.loads(text)
    except Exception as e:
        logger.warning(f"Haiku interpretation failed: {e}")
        return {}


async def search_materials_ai(db: Session, query: str, lifecycle: str = "", limit: int = 50, offset: int = 0):
    """Search MaterialCards using Haiku-interpreted natural language query."""
    interpretation = await interpret_with_haiku(query)

    if not interpretation:
        # Fallback to local search
        return search_materials_local(db, query, lifecycle, limit, offset), query

    # Build search from interpretation
    all_terms = interpretation.get("keywords", []) + interpretation.get("description_terms", [])
    category = interpretation.get("category", "")
    interpreted_label = ", ".join(all_terms)
    if category:
        interpreted_label = f"{category}: {interpreted_label}"

    q = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))

    if all_terms:
        conditions = []
        for term in all_terms:
            pattern = f"%{term}%"
            conditions.append(MaterialCard.description.ilike(pattern))
            conditions.append(MaterialCard.specs_summary.ilike(pattern))
            conditions.append(MaterialCard.category.ilike(pattern))
            conditions.append(MaterialCard.normalized_mpn.ilike(pattern))
        q = q.filter(or_(*conditions))

    if category:
        q = q.filter(MaterialCard.category.ilike(f"%{category}%"))

    if lifecycle:
        q = q.filter(MaterialCard.lifecycle_status == lifecycle)

    total = q.count()
    materials = q.order_by(MaterialCard.search_count.desc()).offset(offset).limit(limit).all()
    return (materials, total), interpreted_label
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_material_search_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/material_search_service.py tests/test_material_search_service.py
git commit -m "feat: add AI-powered material search service with MPN/Haiku routing"
```

---

### Task 6: Enhance Materials List Route + Template

**Files:**
- Modify: `app/routers/htmx_views.py:5705-5744`
- Modify: `app/templates/htmx/partials/materials/list.html`

- [ ] **Step 1: Update the list route to use search service and compute vendor_count/best_price**

In `app/routers/htmx_views.py`, update the `/v2/partials/materials` handler:
- Import `classify_query`, `search_materials_local`, `search_materials_ai`
- Route based on `classify_query(q)`
- For each material, compute `vendor_count` (count of MaterialVendorHistory rows) and `best_price` (min of last_price)
- Pass `interpreted_query` to template context (empty string if local search)

- [ ] **Step 2: Update list template**

Replace columns in `app/templates/htmx/partials/materials/list.html`:
- Remove: "Searches", "Enriched"
- Add: "Vendor Count", "Best Price", "Last Searched"
- Add command-style search bar (monospace input, same styling as Reqs workspace)
- Add interpreted query chip (dismissible, shown only when `interpreted_query` is not empty)
- Match JetBrains Mono density from Reqs workspace

- [ ] **Step 3: Test in browser**

Verify: search bar works for MPN queries, lifecycle pills filter, table shows new columns, clicking a row loads detail.

- [ ] **Step 4: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/materials/list.html
git commit -m "feat: enhance materials list with AI search routing and new columns"
```

---

### Task 7: Redesign Material Detail Header + Collapsible Specs

**Files:**
- Modify: `app/templates/htmx/partials/materials/detail.html`

- [ ] **Step 1: Redesign header**

In `app/templates/htmx/partials/materials/detail.html`:
- Hero MPN: large text (text-2xl or text-3xl, font-bold, JetBrains Mono)
- Manufacturer + description: secondary text below
- Badge pills inline with MPN: lifecycle status, RoHS status (reuse existing badge colors)
- Search count + last searched: small muted text
- Enrich button: keep existing (placeholder)

- [ ] **Step 2: Make specs collapsible**

Wrap the specifications section in Alpine.js toggle:
```html
<div x-data="{ specsOpen: true }">
  <button @click="specsOpen = !specsOpen" class="...">
    Specifications <span x-text="specsOpen ? '▾' : '▸'"></span>
  </button>
  <div x-show="specsOpen" x-transition>
    <!-- existing specs grid -->
  </div>
</div>
```

- [ ] **Step 3: Replace sightings/offers sections with tab structure**

Remove the inline Recent Sightings and Offers sections. Replace with a tab bar:
```html
<div x-data="{ activeTab: 'vendors' }">
  <div class="flex border-b">
    <button @click="activeTab = 'vendors'" :class="activeTab === 'vendors' ? 'border-b-2 border-brand-500' : ''"
      hx-get="/v2/partials/materials/{{ card.id }}/tab/vendors" hx-target="#material-tab-content" hx-trigger="click once">
      Vendors
    </button>
    <!-- Same pattern for: customers, sourcing, price_history -->
  </div>
  <div id="material-tab-content">
    <!-- Lazy loaded via hx-get on tab click. Vendors loads by default. -->
  </div>
</div>
```

Auto-load vendors tab on page load with `hx-trigger="load"` on the vendors button.

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/materials/detail.html
git commit -m "feat: redesign material detail with hero header, collapsible specs, tab structure"
```

---

### Task 8: Material Detail Tab Partials + Routes

**Files:**
- Create: `app/templates/htmx/partials/materials/tabs/vendors.html`
- Create: `app/templates/htmx/partials/materials/tabs/customers.html`
- Create: `app/templates/htmx/partials/materials/tabs/sourcing.html`
- Create: `app/templates/htmx/partials/materials/tabs/price_history.html`
- Modify: `app/routers/htmx_views.py`

- [ ] **Step 1: Add tab route**

In `app/routers/htmx_views.py`, add:
```python
@router.get("/v2/partials/materials/{card_id}/tab/{tab_name}", response_class=HTMLResponse)
async def material_tab_partial(request: Request, card_id: int, tab_name: str, user=Depends(require_user), db: Session = Depends(get_db)):
    card = db.get(MaterialCard, card_id)
    if not card:
        return HTMLResponse("<p>Material not found</p>", status_code=404)

    ctx = _base_ctx(request, user, "materials")
    ctx["card"] = card

    if tab_name == "vendors":
        ctx["vendors"] = db.query(MaterialVendorHistory).filter_by(material_card_id=card_id).order_by(MaterialVendorHistory.last_seen.desc()).all()
        return templates.TemplateResponse("htmx/partials/materials/tabs/vendors.html", ctx)
    elif tab_name == "customers":
        from app.models.purchase_history import CustomerPartHistory
        ctx["customers"] = db.query(CustomerPartHistory).filter_by(material_card_id=card_id).order_by(CustomerPartHistory.last_purchased_at.desc()).all()
        return templates.TemplateResponse("htmx/partials/materials/tabs/customers.html", ctx)
    elif tab_name == "sourcing":
        from app.models.sourcing import Requirement
        ctx["requirements"] = db.query(Requirement).filter(Requirement.material_card_id == card_id).order_by(Requirement.created_at.desc()).all()
        return templates.TemplateResponse("htmx/partials/materials/tabs/sourcing.html", ctx)
    elif tab_name == "price_history":
        from app.models.price_snapshot import MaterialPriceSnapshot
        ctx["snapshots"] = db.query(MaterialPriceSnapshot).filter_by(material_card_id=card_id).order_by(MaterialPriceSnapshot.recorded_at.desc()).limit(200).all()
        return templates.TemplateResponse("htmx/partials/materials/tabs/price_history.html", ctx)
    else:
        return HTMLResponse("<p>Unknown tab</p>", status_code=404)
```

- [ ] **Step 2: Create vendors tab template**

```html
<!-- app/templates/htmx/partials/materials/tabs/vendors.html -->
<!-- Vendors tab for material detail. Loaded via hx-get. -->
<table class="w-full text-xs font-mono">
  <thead><tr class="text-left text-gray-500 border-b">
    <th class="py-1 px-2">Vendor</th>
    <th class="py-1 px-2">Auth</th>
    <th class="py-1 px-2 text-right">Last Price</th>
    <th class="py-1 px-2 text-right">Last Qty</th>
    <th class="py-1 px-2">Currency</th>
    <th class="py-1 px-2">First Seen</th>
    <th class="py-1 px-2">Last Seen</th>
    <th class="py-1 px-2 text-right">Times</th>
    <th class="py-1 px-2">SKU</th>
  </tr></thead>
  <tbody>
    {% for v in vendors %}
    <tr class="border-b border-gray-100 hover:bg-gray-50">
      <td class="py-1 px-2 font-semibold">{{ v.vendor_name }}</td>
      <td class="py-1 px-2">{% if v.is_authorized %}<span class="text-green-600">Yes</span>{% else %}-{% endif %}</td>
      <td class="py-1 px-2 text-right">{{ "%.4f"|format(v.last_price) if v.last_price else '-' }}</td>
      <td class="py-1 px-2 text-right">{{ "{:,}"|format(v.last_qty) if v.last_qty else '-' }}</td>
      <td class="py-1 px-2">{{ v.last_currency or 'USD' }}</td>
      <td class="py-1 px-2">{{ v.first_seen.strftime('%Y-%m-%d') if v.first_seen else '-' }}</td>
      <td class="py-1 px-2">{{ v.last_seen.strftime('%Y-%m-%d') if v.last_seen else '-' }}</td>
      <td class="py-1 px-2 text-right">{{ v.times_seen or 0 }}</td>
      <td class="py-1 px-2">{{ v.vendor_sku or '-' }}</td>
    </tr>
    {% else %}
    <tr><td colspan="9" class="py-4 text-center text-gray-400">No vendor history</td></tr>
    {% endfor %}
  </tbody>
</table>
```

- [ ] **Step 3: Create customers tab template**

Same table pattern — Company, Purchases, Total Qty, Avg Price, Last Purchased, Source. Empty state: "No customer purchase history".

- [ ] **Step 4: Create sourcing tab template**

Table: Requisition #, Status badge, Customer, Date. Click row links to requisition detail. Empty state: "No sourcing activity for this part". Filter `requirements` for non-null `material_card_id`.

- [ ] **Step 5: Create price history tab template**

Table: Date, Vendor, Price, Qty, Source. Empty state: "Price tracking active. Data will appear as new vendor sightings are recorded."

- [ ] **Step 6: Test in browser**

Click a material → verify header, collapsible specs, all 4 tabs load via HTMX.

- [ ] **Step 7: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/materials/tabs/
git commit -m "feat: add material detail tab partials and routes (vendors, customers, sourcing, price history)"
```

---

### Task 9: Integration Tests

**Files:**
- Create: `tests/test_materials_tab.py`

- [ ] **Step 1: Write route tests**

```python
# tests/test_materials_tab.py
"""Integration tests for Materials tab routes."""
from unittest.mock import patch


def test_materials_list_returns_html(client, db_session):
    """GET /v2/partials/materials returns HTML with material table."""
    from app.models import MaterialCard
    card = MaterialCard(normalized_mpn="INT-TEST-001", display_mpn="INT-TEST-001", manufacturer="TestMfg")
    db_session.add(card)
    db_session.commit()

    resp = client.get("/v2/partials/materials")
    assert resp.status_code == 200
    assert "INT-TEST-001" in resp.text


def test_materials_list_search_mpn(client, db_session):
    """Search by MPN uses local search."""
    from app.models import MaterialCard
    card = MaterialCard(normalized_mpn="LM358DR", display_mpn="LM358DR")
    db_session.add(card)
    db_session.commit()

    resp = client.get("/v2/partials/materials?q=LM358")
    assert resp.status_code == 200
    assert "LM358DR" in resp.text


def test_material_detail_returns_html(client, db_session):
    """GET /v2/partials/materials/{id} returns detail page."""
    from app.models import MaterialCard
    card = MaterialCard(normalized_mpn="DETAIL-001", display_mpn="DETAIL-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    assert "DETAIL-001" in resp.text


def test_material_tab_vendors(client, db_session):
    """GET /v2/partials/materials/{id}/tab/vendors returns vendor table."""
    from app.models import MaterialCard
    card = MaterialCard(normalized_mpn="TAB-001", display_mpn="TAB-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/tab/vendors")
    assert resp.status_code == 200
    assert "No vendor history" in resp.text


def test_material_tab_price_history_empty(client, db_session):
    """Price history tab shows empty state."""
    from app.models import MaterialCard
    card = MaterialCard(normalized_mpn="PRICE-001", display_mpn="PRICE-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/tab/price_history")
    assert resp.status_code == 200
    assert "Price tracking active" in resp.text
```

- [ ] **Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_materials_tab.py -v`
Expected: ALL PASS

- [ ] **Step 3: Run full test suite + coverage**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
Expected: No coverage regression

- [ ] **Step 4: Commit**

```bash
git add tests/test_materials_tab.py
git commit -m "test: add integration tests for Materials tab"
```

---

### Task 10: Final Polish + Deploy

- [ ] **Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Run coverage check**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
Expected: No regression from baseline

- [ ] **Step 3: Push and deploy**

```bash
cd /root/availai && git push origin main && docker compose up -d --build
```

- [ ] **Step 4: Verify in browser**

- All 10 tabs visible in bottom nav
- Materials tab loads list with search bar
- MPN search works (type "LM358")
- Natural language search works (type "DDR5 memory module")
- Click material → detail page with hero header
- Collapsible specs section works
- All 4 tabs load (Vendors, Customers, Sourcing, Price History)
- Price History shows empty state message

- [ ] **Step 5: Check logs**

```bash
docker compose logs -f app | head -50
```
Verify no errors on startup or during navigation.
