# Sightings Page — Substitute MPN Integration

**Date:** 2026-03-30
**Approach:** A (lightweight, no schema changes)

## Problem

The search pipeline already searches all substitute MPNs via `get_all_pns()` and stores `mpn_matched` on each `Sighting` row. But the sightings page UI shows zero sub information — buyers can't tell whether a vendor's stock is for the primary MPN or a substitute.

## Design

Three touchpoints, no migrations, no aggregation service changes.

### 1. Table rows — sub count badge + searchable

In `table.html`, next to the primary MPN in `render_row` and `render_card`, show a count badge when substitutes exist:

```
ABC123  +3 subs
```

Uses `requirement.substitutes|sub_mpns` (existing Jinja2 filter). Badge is a small `text-[10px]` amber pill, consistent with the existing sub pill style.

In `sightings.py` `sightings_list()`, extend the search filter to also match `Requirement.substitutes_text` (the computed text column, already populated by a PostgreSQL trigger):

```python
if filters.q:
    safe_q = escape_like(filters.q)
    query = query.filter(
        Requirement.primary_mpn.ilike(f"%{safe_q}%")
        | Requisition.customer_name.ilike(f"%{safe_q}%")
        | Requirement.substitutes_text.ilike(f"%{safe_q}%")
    )
```

### 2. Detail header — sub pills

In `detail.html`, below the primary MPN and manufacturer line, show amber pills for each substitute MPN. Reuses the exact pattern from `req_row.html` lines 40-51:

```html
{% set mpns = requirement.substitutes|sub_mpns %}
{% if mpns %}
<div class="flex flex-wrap gap-1 mt-1">
  {% for mpn in mpns %}
  <span class="px-1.5 py-0.5 text-[10px] font-mono font-medium rounded bg-amber-50 text-amber-700 border border-amber-200">{{ mpn }}</span>
  {% endfor %}
</div>
{% endif %}
```

No new context variable needed — `requirement.substitutes` is already loaded on the detail endpoint.

### 3. Vendor rows — matched MPN tag

**Backend:** In `sightings.py` detail endpoint, query distinct `(vendor_name, mpn_matched)` from `Sighting` rows for this requirement:

```python
matched_rows = (
    db.query(Sighting.vendor_name, Sighting.mpn_matched)
    .filter(
        Sighting.requirement_id == requirement.id,
        Sighting.mpn_matched.isnot(None),
    )
    .distinct()
    .all()
)
vendor_matched_mpns: dict[str, list[str]] = {}
for vendor_name, mpn in matched_rows:
    vendor_matched_mpns.setdefault(vendor_name, []).append(mpn)
```

Pass `vendor_matched_mpns` to the template context.

**Template:** In `_vendor_row.html`, below the vendor name badges, when a vendor has sightings for a sub MPN (any `mpn_matched` that differs from `requirement.primary_mpn`), show small tags:

```html
{% set matched = vendor_matched_mpns.get(s.vendor_name, []) %}
{% set sub_matches = matched|reject("equalto", requirement.primary_mpn)|list %}
{% if sub_matches %}
  {% for mpn in sub_matches %}
  <span class="px-1 py-0.5 text-[9px] font-mono rounded bg-amber-50 text-amber-600 border border-amber-100">via {{ mpn }}</span>
  {% endfor %}
{% endif %}
```

## Files Changed

| File | Change |
|------|--------|
| `app/routers/sightings.py` | Add `substitutes_text` to search filter; query `mpn_matched` on detail endpoint |
| `app/templates/htmx/partials/sightings/table.html` | Add sub count badge in `render_row` and `render_card` |
| `app/templates/htmx/partials/sightings/detail.html` | Add sub pills below primary MPN |
| `app/templates/htmx/partials/sightings/_vendor_row.html` | Add "via SUB-MPN" tag |
| `tests/test_sightings.py` | Tests for search filter, detail context, template rendering |

## Out of Scope

- No schema changes or migrations
- No changes to `VendorSightingSummary` aggregation
- No sub editing from the sightings page (edit subs on the requisition page)
- No grouping vendor rows by MPN
