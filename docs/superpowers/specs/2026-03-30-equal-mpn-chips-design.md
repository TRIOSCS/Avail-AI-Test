# Equal MPN Chips — Design Spec

**Date:** 2026-03-30
**Status:** Approved (revised after architect + simplifier + code review)
**Summary:** Treat all MPNs (primary + substitutes) as visually and functionally equal. No hierarchy between primary and substitute part numbers.

---

## Problem

Substitutes are currently displayed as secondary elements — small grey/amber pills in a sub-row, visually distinct from the primary MPN shown as plain text. In practice, there is no meaningful difference between a primary MPN and a substitute — they are all acceptable part numbers for the requirement. The visual distinction is misleading and the subs are hard to see.

## Design

### Visual: Inline Chips (Option A)

All MPNs rendered as identical chips in a single flex-wrap row within the "Part Numbers" column.

**Chip style:**
- `bg-brand-50 text-brand-700 border border-brand-200`
- `font-mono text-[12px] font-medium`
- `px-2.5 py-1 rounded-md`
- Hover: `bg-brand-100`, subtle lift shadow
- All chips identical — no primary vs substitute distinction

Note: `text-brand-700` (not 600) for WCAG AA contrast at 12px on `bg-brand-50`.

**Column header:** Visible text renamed from "MPN" to "Part Numbers". `data-col-key="mpn"` stays unchanged.

**No labels:** No "alt:", "subs:", or other prefixes. Just chips.

**Single-MPN requirements** still display as a chip for visual consistency.

**Shared macro:** Create a `_mpn_chips.html` Jinja2 macro that takes a requirement and renders the chip row. All templates call this macro instead of duplicating chip markup.

### Templates Changed

| View | File | Change |
|------|------|--------|
| **Parts list** (left panel) | `parts/list.html` | Replace MPN text + sub-row with chip macro in main row. Delete the separate sub-row `<tr>` block (lines ~207-234). |
| **Part header** | `parts/header.html` | Replace MPN heading + sub badges with chip row. Preserve click-to-edit: entire chip row is the `hx-get` trigger for editing MPNs. |
| **Req parts tab row** | `requisitions/tabs/req_row.html` | Replace MPN text + debug markup with chip macro. Remove debug yellow/red inline styles. |
| **Req parts tab header** | `requisitions/tabs/parts.html` | Rename visible "MPN" label to "Part Numbers". Keep `data-col-key="mpn"`. |
| **Sightings table** | `sightings/table.html` | Replace primary_mpn text + amber `+N subs` badge with chip row. |
| **Sightings detail** | `sightings/detail.html` | Replace primary_mpn heading + amber sub chips with chip row. |
| **Sourcing workspace** | `sourcing/workspace.html` | Replace primary_mpn-only title with chip row. |
| **Sourcing results** | `sourcing/results.html` | Replace primary_mpn-only heading with chip row. |
| **Req details tab** | `parts/tabs/req_details.html` | Replace primary_mpn + raw substitute count with chip row. |

**Not changed:**
- `requisitions/unified_modal.html` — edit form still lets you add/remove MPNs, labeling can stay as-is since it's an edit context
- `requisitions/rfq_compose.html` — RFQ email context, primary_mpn used for email subject line (intentional)
- Edit forms — still input fields for each MPN

### Search: Already Implemented (No Change Needed)

`search_service.py` already searches all MPNs via `get_all_pns()` (line 152) which collects `[primary_mpn] + substitutes`. The `_fetch_fresh()` function fires all connector x MPN combos via `asyncio.gather()`. No search changes needed.

### Bug Fix: `get_all_pns()` dict-format substitutes

**Pre-existing bug** in `search_service.py` line 166: `str(sub)` on dict-format substitutes produces garbage strings like `"{'mpn': 'ABC123', 'manufacturer': 'TI'}"` which fail to match anything.

**Fix:** Extract `sub.get("mpn")` for dict-format subs, matching the pattern in `template_env.py`'s `_sub_mpns_filter`.

### Search Coverage Gaps (Fix in this spec)

These pre-existing gaps become more visible when MPNs are visually equal:

| Surface | File | Issue | Fix |
|---------|------|-------|-----|
| **Global search** | `global_search_service.py:169` | Only queries `primary_mpn` | Add `OR substitutes_text.ilike(pattern)` |
| **Parts list search box** | `htmx_views.py:366` | Only queries `primary_mpn` | Add `OR substitutes_text.ilike(pattern)` |

### Out of Scope (Noted for Future)

These are pre-existing gaps that don't block this spec:

- **ICS/NC background workers** only search primary_mpn — separate worker architecture, fix independently
- **`total_searches` counter inflation** — pre-existing, N× per MPN. Document counter semantics, fix independently
- **`sub_card_map`** in `htmx_views.py` — only builds material card links for substitutes, not primary. When using the shared chip macro, build a unified `mpn_card_map` covering all MPNs

### Data Model: No Changes

- `primary_mpn` and `substitutes` columns stay as-is
- The first MPN entered is stored as `primary_mpn`, the rest as `substitutes` — this is an implementation detail invisible to the user
- No migration needed

## Mockup

Interactive mockup available at: `app/static/public/mockup-parts-layout.html`
(Option A was selected from three proposed layouts)

## Out of Scope

- Reordering MPNs within a requirement
- Per-MPN sighting counts (sightings roll up to requirement level)
- Database schema changes
- Changes to the RFQ email template (still uses primary_mpn for the subject line)
- ICS/NC worker search coverage (fix independently)
- `total_searches` counter semantics (fix independently)
