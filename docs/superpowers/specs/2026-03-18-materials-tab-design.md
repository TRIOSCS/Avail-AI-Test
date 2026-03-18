# Materials Tab — Design Spec

**Date**: 2026-03-18
**Status**: Approved

## Overview

Promote the existing MaterialCard system to a first-class "Materials" tab in the bottom navigation, giving users direct access to the global parts catalog with full history and knowledge for every part number.

## Bottom Navigation

Remove the "More" dropdown. All 10 tabs displayed flat:

1. **Reqs** — Requisitions workspace
2. **Search** — Global search
3. **Buys** — Buy Plans
4. **Vendors** — Vendor management
5. **Materials** — Global parts catalog (NEW)
6. **Cos** — Companies/Customers
7. **Proact** — Proactive matches
8. **Quotes** — Quote management
9. **Prospect** — Prospecting pipeline
10. **Config** — Settings

Short labels, small icons, compact padding. No hamburger or overflow menu.

## Materials List View

Enhance the existing `materials/list.html` template. Route: `/v2/partials/materials`.

### Search

Single command-style search bar. The user types anything — the system invisibly routes:

- **MPN detected** (fewer than 3 whitespace-separated words): local PostgreSQL search using existing trigram + full-text indexes on MaterialCard.
- **Natural language detected** (3+ whitespace-separated words, e.g., "DDR5 16GB memory", "UHD LCD panel", "7200 rpm drive"): sent to Claude Haiku with prompt to interpret as electronic component search and return structured filters (category, specs, keywords, MPN patterns).
- Haiku's structured filters are used to query MaterialCards by description, category, specs_summary, cross_references.
- If Haiku returns no useful interpretation, fall back to local full-text + trigram search. Single code path — no branching.
- When AI was used, show interpreted query as a dismissible chip above results (e.g., `Showing results for: DDR5 memory, 16GB capacity`).
- No Redis cache. Add later if needed.

### Filters and Table

- **Lifecycle filter pills** (reuse existing): All / Active / EOL / Obsolete / NRFND / LTB
- **Table columns**: MPN, Manufacturer, Category, Lifecycle, Vendor Count, Last Searched, Best Price
- **Density**: JetBrains Mono, compact rows — match Requisitions workspace style
- **Default sort**: Most recently searched/seen
- **Click row** → full-page material detail

## Material Detail View

Full-page view (not split-panel). Route: `/v2/partials/materials/{card_id}`.

### Header

- **Hero MPN** — large, dominant text
- Manufacturer and description — secondary
- Compact badge pills: lifecycle status, RoHS status
- Search count + last searched date
- Enrich button (placeholder — stays non-functional this sprint)
- **Collapsible specs section** within header (default: expanded): package type, pin count, category, datasheet link, cross-references, specs summary. Not a separate tab. Toggle via Alpine.js `x-show`.

### Tabs (4)

Default tab: **Vendors**

#### Vendors
Table of all vendors who've offered this part (from MaterialVendorHistory):
- Vendor name, authorized status, last price, last qty, currency
- First seen, last seen, times seen
- Vendor SKU

#### Customers
Which customers have bought this part (from CustomerPartHistory):
- Company name, purchase count, total quantity, avg unit price
- Last purchased date, source

#### Sourcing
All requisitions/requirements that referenced this MPN (from Requirement → MaterialCard link):
- Requisition number, requirement status, date, customer
- Click through to requisition detail

#### Price History
Timeline of price observations (from new MaterialPriceSnapshot table):
- Table: date, vendor, price, quantity, source
- Chart comes later as data accumulates
- Empty state: "Price tracking active. Data will appear as new vendor sightings are recorded."

## New Data Model: MaterialPriceSnapshot

```python
class MaterialPriceSnapshot(Base):
    __tablename__ = "material_price_snapshots"

    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, ForeignKey("material_cards.id"), index=True, nullable=False)
    vendor_name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)  # Float to match MaterialVendorHistory.last_price
    currency = Column(String(3), default="USD")
    quantity = Column(Integer, nullable=True)
    source = Column(String(50), nullable=False)  # api_sighting, stock_list, manual
    recorded_at = Column(UTCTimestamp, server_default=func.now(), index=True)
```

**When to record**: Via a service function `record_price_snapshot()` called at each MaterialVendorHistory create/update site. Not a SQLAlchemy event listener — explicit calls at each site for clarity and testability.

**Indexes**: `(material_card_id, recorded_at)` for efficient per-material timeline queries.

## Technical Approach

- **List view**: Enhance existing `materials/list.html` and `/v2/partials/materials` route. Replace current columns (Searches, Enriched) with Vendor Count, Best Price, Last Searched. Htmx view route needs to compute vendor_count and best_price (join MaterialVendorHistory).
- **Detail view**: Enhance existing `materials/detail.html` and `/v2/partials/materials/{card_id}` route. Standardize route parameter as `card_id` (not `material_id`). Tabs lazy-load via `hx-get` on tab click.
- **Bottom nav**: Modify `base.html` — remove "More" menu, add all 10 tabs flat
- **AI search**: Extend existing materials list endpoint with Haiku integration
- **Price snapshots**: New model + Alembic migration, service function `record_price_snapshot()` called at each MaterialVendorHistory update site
- **Sourcing tab**: Filter `Requirement.material_card_id IS NOT NULL` (FK uses `ondelete="SET NULL"`)
- **Templates follow existing patterns**: Jinja2 partials, HTMX swaps into `#main-content`, Alpine.js for state
