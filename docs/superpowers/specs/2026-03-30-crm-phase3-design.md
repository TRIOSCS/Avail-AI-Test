# CRM Phase 3: Vendor Discovery Rethink

**Created:** 2026-03-30
**Status:** Approved
**Parent:** [CRM Master Roadmap](2026-03-29-crm-master-roadmap.md)

## Goal

Make it easy for sourcers to find the right vendor for a specific part. Add "Find by Part" as a third inner tab on the Vendors sub-tab, query MaterialVendorHistory for MPN-to-vendor lookup, enhance vendor search to include brand/commodity tags, and show MPN-specific sighting history when viewing vendor details from a part search context.

## Design Decisions

- **Keep CRM shell** — Customers | Vendors | Performance stays. No nav restructure.
- **3 inner tabs on Vendors**: All Vendors | My Vendors | **Find by Part**
- **Query MaterialVendorHistory** for MPN discovery (deduplicated, has times_seen/last_price/last_qty)
- **Enhance existing vendor detail** with MPN context param (not a new focused card template)
- **Enhanced Browse search** — also matches brand_tags and commodity_tags via ilike
- **Defer filter dropdowns** — no production data yet; add faceted chips later when data exists
- **Required migration** — JSON→JSONB on tag columns + GIN indexes + compound sighting index

## Part 1: Database Migrations

### JSON→JSONB Migration

`VendorCard.brand_tags` and `VendorCard.commodity_tags` are `JSON` type. PostgreSQL's `@>` containment operator and GIN indexing require `JSONB`. Migrate both columns:

```sql
ALTER TABLE vendor_cards ALTER COLUMN brand_tags TYPE JSONB USING brand_tags::jsonb;
ALTER TABLE vendor_cards ALTER COLUMN commodity_tags TYPE JSONB USING commodity_tags::jsonb;
```

Add GIN indexes for future filter support:

```sql
CREATE INDEX ix_vendor_brand_tags ON vendor_cards USING gin(brand_tags jsonb_path_ops);
CREATE INDEX ix_vendor_commodity_tags ON vendor_cards USING gin(commodity_tags jsonb_path_ops);
```

Update model in `app/models/vendors.py` to use `JSONB` type instead of `JSON`.

### Compound Sighting Index

Add compound index for the MPN+vendor query used by the focused vendor card:

```sql
CREATE INDEX ix_sightings_mpn_vendor_norm ON sightings (normalized_mpn, vendor_name_normalized);
```

## Part 2: Find by Part — Inner Tab + MPN Search

### New Inner Tab

Add "Find by Part" as a third tab in the vendor list's existing tab bar (alongside "All Vendors" and "My Vendors"). Follows the same `border-b-2` underline pattern.

Clicking "Find by Part" loads a new partial via HTMX:

**Route:** `GET /v2/partials/vendors/find-by-part`
**Parameters:** `mpn` (string, required for results)

### MPN Search Bar

The Find by Part partial contains:
- A prominent search input labeled "Enter MPN to find vendors"
- `hx-get="/v2/partials/vendors/find-by-part"` with `hx-trigger="input delay:500ms"`
- Results appear below the search bar

### MPN Search Query

Query `MaterialVendorHistory` joined to `MaterialCard` and `VendorCard`:

```python
results = (
    db.query(MaterialVendorHistory, VendorCard)
    .join(MaterialCard, MaterialVendorHistory.material_card_id == MaterialCard.id)
    .outerjoin(VendorCard, VendorCard.normalized_name == MaterialVendorHistory.vendor_name_normalized)
    .filter(MaterialCard.normalized_mpn == normalize_mpn(mpn))
    .order_by(
        MaterialVendorHistory.times_seen.desc(),  # availability (most seen = most likely to have it)
        VendorCard.response_rate.desc().nullslast(),  # reliability
        VendorCard.total_pos.desc().nullslast(),  # relationship
        VendorCard.avg_response_hours.asc().nullslast(),  # speed
    )
    .limit(30)
    .all()
)
```

Ranking priority matches user requirements: availability, reliability, relationship, speed, price.

### Results Table

| Column | Source |
|--------|--------|
| Vendor | MVH.vendor_name (linked to VendorCard detail if exists) |
| Times Seen | MVH.times_seen |
| Last Price | MVH.last_price |
| Last Qty | MVH.last_qty |
| Last Seen | MVH.last_seen (timeago filter) |
| Win Rate | VendorCard.overall_win_rate (if joined) |
| Response Time | VendorCard.avg_response_hours (if joined) |

Click a vendor row → HTMX lazy-loads the vendor detail (existing `/v2/partials/vendors/{id}`) with `?mpn={searched_mpn}` context param.

## Part 3: Vendor Detail MPN Context

When the vendor detail view receives an `?mpn=` query parameter:

1. The Overview tab's "Recent Sightings" table filters to show only sightings for that MPN
2. A header appears above the sightings: "Sightings for {MPN}" with a badge showing `times_seen`
3. MVH summary stats shown: times seen, last price, last qty, first seen date

This requires minimal template changes — the existing sightings table and overview layout stay, just filtered and annotated.

**Route change:** `vendor_detail_partial()` and `vendor_tab()` accept optional `mpn` query param. When present, filter sightings query and pass `mpn` to template context.

## Part 4: Enhanced Browse Vendors Search

The existing vendor search (line 3368 of htmx_views.py) searches only `display_name` and `domain`. Extend to also search `brand_tags` and `commodity_tags` via ilike on the JSONB text representation:

```python
if q.strip():
    sb = SearchBuilder(q.strip())
    term = f"%{q.strip()}%"
    query = query.filter(
        or_(
            sb.ilike_filter(VendorCard.display_name, VendorCard.domain),
            VendorCard.brand_tags.cast(sa.Text).ilike(term),
            VendorCard.commodity_tags.cast(sa.Text).ilike(term),
        )
    )
```

This lets sourcers type "TI" or "Microcontrollers" in the vendor search and find matching vendors.

## Technical Architecture

### New Files

| File | Responsibility |
|------|---------------|
| `app/templates/htmx/partials/vendors/find_by_part.html` | Find by Part partial (MPN search + results table) |
| `alembic/versions/XXX_jsonb_tags_and_sighting_index.py` | Migration: JSON→JSONB + GIN indexes + compound sighting index |
| `tests/test_vendor_discovery.py` | Tests for MPN search, enhanced browse, MPN context on detail |

### Modified Files

| File | Change |
|------|--------|
| `app/models/vendors.py` | Change `brand_tags` and `commodity_tags` from JSON to JSONB |
| `app/models/sourcing.py` | Add compound index `(normalized_mpn, vendor_name_normalized)` |
| `app/routers/htmx_views.py` | Add `/v2/partials/vendors/find-by-part` route, enhance browse search, add `mpn` param to vendor detail/tab routes |
| `app/templates/htmx/partials/vendors/list.html` | Add "Find by Part" tab to inner tab bar |
| `app/templates/htmx/partials/vendors/detail.html` | MPN context header + filtered sightings |

### No Navigation Changes

CRM shell, bottom nav, and v2_page dispatcher are unchanged.

## What This Does NOT Include

- Brand/commodity filter dropdowns (deferred — no production data yet)
- New focused vendor card template (enhance existing detail view instead)
- Navigation restructure (CRM shell stays)
- Send RFQ button on vendor card (existing RFQ workflow unchanged)
- Vendor affinity recommendations in UI (L1/L2/L3 exist but stay backend-only for now)
