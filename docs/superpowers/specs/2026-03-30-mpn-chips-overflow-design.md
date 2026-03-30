# MPN Chips Overflow — Collapse with "+N more"

**Date:** 2026-03-30
**Status:** Approved

## Problem

The `mpn_chips` macro renders all MPNs (primary + substitutes) inline with `whitespace-nowrap`. When a requirement has many substitutes (e.g., 12), all 13 chips render in a single row, blowing out table column widths and pushing other columns off-screen.

## Design

**Approach:** Collapse with "+N more" toggle, matching the existing vendor collapse pattern.

**Behavior:**
- Show **2 chips max** by default (primary + 1 substitute)
- If more exist, show a "+N more" pill that expands inline on click
- Expanded state resets on navigation (no persistence)
- Collapsed chips use Alpine.js `x-data` / `x-show` for toggle

**Visual:**
```
Collapsed: [LM358] [LM358N] [+10 more]
Expanded:  [LM358] [LM358N] [LM358P] [LM358DR] ... [−less]
```

**Implementation scope:**
- Single file change: `app/templates/htmx/partials/shared/_mpn_chips.html`
- Add `max_visible` parameter (default 2) to the macro
- First `max_visible` chips render normally
- Remaining chips wrapped in `x-show="expanded"` span
- "+N more" / "−less" toggle pill styled consistently with existing chip styling
- The `whitespace-nowrap` on the outer span stays; chips remain inline
- All 9 call sites inherit the fix automatically (no caller changes needed)

**Edge cases:**
- 0 subs: just primary chip, no toggle — unchanged
- 1 sub: primary + 1 sub = 2 total, no toggle needed
- 2+ subs: primary + 1 sub visible, rest collapsed

**No changes to:**
- `sub_mpns` filter
- `link_map` functionality
- Any calling template
- CSS/styles.css
