# MPN Clickable Links + Materials Nav Tab

**Date:** 2026-03-30
**Status:** Approved (user-directed implementation)

## Summary

Two features:
1. MPN chips on the sightings page become clickable links to material card detail pages
2. A "Materials" tab is added to the bottom navigation bar for a searchable material card library

## Feature 1: Clickable MPN Chips in Sightings

### Current State
- `mpn_chips` macro already supports `link_map` parameter (dict of MPN → card ID)
- Sightings table and detail templates call `mpn_chips(r)` without `link_map`
- MaterialCard records are linked to Requirements via `material_card_id` FK

### Design
- In `sightings_list()` router: query MaterialCard IDs for the page's requirements and build a `link_map` dict
- Pass `link_map` to the template context
- Update `mpn_chips(r)` calls in `table.html` and `detail.html` to pass `link_map`
- Also look up cards by `normalized_mpn` for substitute MPNs
- Click navigates to `/v2/materials/{card_id}` via HTMX (already implemented in macro)

### Data Flow
```
sightings_list() router
  → query MaterialCard by normalized_mpn for all MPNs on page
  → build link_map: {mpn_string: card_id}
  → pass to template
  → mpn_chips(r, link_map=link_map) renders <a> tags instead of <span>
```

## Feature 2: Materials Tab in Bottom Nav

### Current State
- Materials workspace fully built: `/v2/materials`, `/v2/partials/materials/workspace`
- Search, filters, detail pages, tabs all operational
- Missing from bottom nav (mobile_nav.html)

### Design
- Add "Materials" entry to `nav_items` list in `mobile_nav.html`
- Add `materials` → `"materials"` mapping in `urlToNav()` JS function
- Use a beaker/cube icon (consistent with material/library concept)
- Position after "Sightings" tab (logical workflow: search → sightings → materials)
- Route mapping in `v2_page()` already handles `/v2/materials` → `current_view = "materials"`

## Files Changed

1. `app/routers/sightings.py` — build link_map in `sightings_list()` and `sightings_detail()`
2. `app/templates/htmx/partials/sightings/table.html` — pass link_map to mpn_chips
3. `app/templates/htmx/partials/sightings/detail.html` — pass link_map to mpn_chips
4. `app/templates/htmx/partials/shared/mobile_nav.html` — add Materials tab
5. `tests/test_sightings_router.py` — test link_map population

## Out of Scope
- No changes to the Materials workspace itself
- No changes to the mpn_chips macro (already supports links)
