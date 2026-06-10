# Sightings Vendor Row Status Treatment — Design

**Date:** 2026-06-10
**Status:** Approved (user selected "Soft tint + dim + badge" from 3 presented options)
**Scope:** Presentation-only change to the sightings workspace vendor rows.

## Problem

On the sightings workspace detail panel, a vendor sighting that is **unavailable** shows
only a small gray pill (`bg-gray-100 text-gray-500`) — easy to miss, reads like a neutral
state. A sighting **converted to an offer** (`offer-in`) shows a small emerald pill but the
row itself looks identical to untouched rows. The user wants both states visible at a
glance: red-ish for unavailable, mild green for converted.

## Decision

Apply a row-level soft tint + text dim + stronger badge, keyed off the existing computed
vendor status `vs` (server-side precedence already resolved in
`app/services/sighting_status.py`: `blacklisted > offer-in > contacted > unavailable >
sighting` — no template-side precedence logic needed).

This reuses the page's existing vocabulary: hot requirement rows use `bg-rose-50/30`,
accepted excess bids use `bg-emerald-50/50`, status pills are 50-shade chips.

## Exact changes — `app/templates/htmx/partials/sightings/_vendor_row.html`

1. **Row container** (the always-visible flex div, currently
   `hover:bg-gray-50/50`): status-aware background + hover, default unchanged:
   - `vs == 'unavailable'` → `bg-rose-50/60 hover:bg-rose-50/80`
   - `vs == 'offer-in'` → `bg-emerald-50/50 hover:bg-emerald-50/70`
   - all other statuses → `hover:bg-gray-50/50` (exactly as today)
   Implemented via a Jinja `{% set %}` dict lookup with **full literal class strings**
   (Tailwind content-scan requirement — never concatenate class fragments).

2. **Badge styles** (`vs_styles` dict): 50-shade chips disappear against 50-shade row
   tints, so tinted rows get 100-shade badges:
   - `'unavailable'`: `bg-gray-100 text-gray-500` → `bg-rose-100 text-rose-700`
   - `'offer-in'`: `bg-emerald-50 text-emerald-700` → `bg-emerald-100 text-emerald-700`
   - `blacklisted`, `contacted`, `sighting`: unchanged.

3. **Dim unavailable row content** (unavailable only — converted rows keep full-strength
   text):
   - Vendor name span: `text-gray-900` → `text-gray-400` when unavailable.
   - Right-side qty (`text-gray-600`) → `text-gray-400` when unavailable.
   - Score color: force `text-gray-400` when unavailable (skip the emerald/amber score
     scale).

4. **Everything else unchanged:** expanded detail stays neutral gray (`bg-gray-50/50`),
   `<tr>` borders unchanged, action-button visibility unchanged (already hidden for
   unavailable/blacklisted), no new UI elements added or removed.

## Out of scope (deliberate)

- No change to status computation/precedence (an unavailable vendor that later gets an
  offer correctly shows green — the offer is the dominant fact).
- No treatment change for `blacklisted` (already has a red badge; user asked for
  unavailable + converted only).
- No changes to the left requirements table or the Offers tab.
- No schema/route changes — `vs` is already in row context.

## Testing

Route-level render tests in `tests/test_sightings_router.py` (pattern: existing
`TestSightingsMarkUnavailable`), asserting on the detail-panel HTML:
- Vendor with all sightings `is_unavailable=True` → response contains `bg-rose-50/60`
  and `bg-rose-100 text-rose-700` (don't assert absence of `text-gray-900` — other page
  elements legitimately use it).
- Vendor with an `Offer` on the requirement → contains `bg-emerald-50/50` and
  `bg-emerald-100 text-emerald-700`.
- Vendor with neither → contains neither row-tint class (seed a single vendor per case).

## Risks

- Tailwind purge: all classes are literal strings in the template, covered by the
  content scan; `deploy.sh` additionally verifies template classes exist in built CSS.
- Translucent tints (`/60`, `/50`) sit on white panel background — consistent with the
  existing `bg-rose-50/30` hot-row treatment.
