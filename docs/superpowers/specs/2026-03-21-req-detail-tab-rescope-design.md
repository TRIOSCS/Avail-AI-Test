# REQ Detail Tab Rescope — Design Spec

**Date:** 2026-03-21
**Status:** Approved
**Scope:** Finish incomplete REQ Detail tab work + compact layout + fix review findings

## Context

The REQ Detail tab was partially built in a prior session. Six spec columns were moved off the left panel into a single "Specs" count pill, and a Part Specifications section was added to the top of the detail tab. However, two templates were created but the layout was never compacted, and several issues were found during review.

## What Exists (Committed + Uncommitted)

**Committed:**
- Left panel: 6 spec columns replaced with "Specs" `n/6` pill
- "Need By" renamed to "Bid Due"

**Uncommitted (modified files):**
- `app/routers/htmx_views.py` — Backend routes `edit-spec` / `save-spec` with `_SPEC_EDITABLE` / `_SPEC_LABELS` constants; `joinedload(Requirement.offers)` on sibling query
- `app/templates/htmx/partials/parts/list.html` — Specs count pill renderer, Bid Due rename
- `app/templates/htmx/partials/parts/tabs/req_details.html` — Part Specifications section at top with click-to-edit; 5 new sibling table columns (Brand, Tgt $, Cust PN, Subs, Offers/Best $)
- `tests/test_req_details_tab.py` — 6 new tests

**Untracked (new files):**
- `app/templates/htmx/partials/parts/spec_edit.html` — inline edit form for spec fields
- `app/templates/htmx/partials/parts/spec_display.html` — post-save display span

## Design Decisions (from brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Spec edit interaction | Inline swap — blur/Enter saves, Escape cancels |
| 2 | Tighten requisition info | Compact grid — label:value on same line, `gap-y-1.5 mb-3` |
| 3 | Sibling table density | Apply `compact-table` CSS class (gets sticky headers, alternating rows, proper fonts) |
| 4 | Spec field types | All text inputs (MVP) |
| 5 | Live update specs count | Yes — via existing `part-updated` HX-Trigger |

## Changes Required

### 1. Compact Part Specifications Grid

**File:** `app/templates/htmx/partials/parts/tabs/req_details.html`

- Change grid from `gap-x-6 gap-y-3 text-sm mb-6` to `gap-x-6 gap-y-1.5 text-sm`
- Switch from stacked label-over-value to inline `Label: Value` on same line
- **Important:** Label must remain a separate non-targeted `<span>` so click-to-edit only replaces the value. Use `flex items-center gap-1` on parent: `<div class="flex items-center gap-1"><span class="text-xs text-gray-500">Label:</span><div id="reqd-spec-x" hx-get="..." hx-target="...">Value</div></div>`

### 2. Compact Requisition Info Grid

**File:** `app/templates/htmx/partials/parts/tabs/req_details.html`

- Same spacing change: `gap-y-1.5` (no bottom margin — `<hr>` provides separation)
- Same inline label:value format

### 3. Add Section Dividers

**File:** `app/templates/htmx/partials/parts/tabs/req_details.html`

- Add `<hr class="my-2">` between Part Specifications and Requisition Info sections
- Add `<hr class="my-2">` between Requisition Info and Sibling Parts table
- Normalize all section headings to `mb-2` (currently mixed `mb-3`/`mb-2`)

### 4. Tighten Sibling Parts Table

**File:** `app/templates/htmx/partials/parts/tabs/req_details.html`

The sibling table already has 8 columns added in the prior session (checkbox, MPN, Brand, Status, Qty, Tgt $, Cust PN, Subs, Offers). These columns plus `joinedload(Requirement.offers)` are already in the uncommitted diff. Changes needed:

- Apply the `compact-table` CSS class to the table element (replaces manual `min-w-full divide-y divide-gray-200 text-sm` and per-cell `px-3 py-2` classes). This provides: sticky headers, alternating row colors, JetBrains Mono on data cells, proper hover/selection states, and `text-xs` density — all matching the left panel's table.
- Replace inline `part_status_colors` dict with `{{ status_badge(part.sourcing_status) }}` macro (already imported)

### 5. Unify Condition Choices

**Files:** `app/routers/htmx_views.py`, `app/templates/htmx/partials/parts/spec_edit.html`

- Pass `_CONDITION_CHOICES` from the route handler into `spec_edit.html` template context (avoids hardcoding choices in two places)
- Remove hardcoded choices list from `spec_edit.html`, use the context variable instead
- Remove `condition`, `date_codes`, and `packaging` from `_PART_HEADER_EDITABLE` — these are now in the spec section and having them editable in two places causes stale display (editing from one doesn't refresh the other)

### 6. Add Archive Guard

**File:** `app/routers/htmx_views.py`

- In both `part_spec_edit` and `part_spec_save`, check `req.sourcing_status == "archived"` and return 403
- Pattern: `if req.sourcing_status == "archived": return HTMLResponse("Cannot edit archived part", status_code=403)`

### 7. Fix Whitespace Bug

**File:** `app/routers/htmx_views.py`

- Change `clean = value.strip() if value else None` to `clean = (value or "").strip() or None`
- Ensures whitespace-only input becomes NULL, not empty string

### 8. Remove Redundant `_SPEC_EDITABLE` Set

**File:** `app/routers/htmx_views.py`

- Delete `_SPEC_EDITABLE` set
- Replace `if field not in _SPEC_EDITABLE` with `if field not in _SPEC_LABELS`

## Files Changed

| File | Changes |
|------|---------|
| `app/templates/htmx/partials/parts/tabs/req_details.html` | Compact grids (flex label:value), `<hr>` dividers, `compact-table` on sibling table, use status_badge macro |
| `app/templates/htmx/partials/parts/list.html` | Already modified (specs pill, Bid Due rename) — no further changes needed |
| `app/routers/htmx_views.py` | Archive guard, whitespace fix, remove _SPEC_EDITABLE, pass _CONDITION_CHOICES to template, remove condition/date_codes/packaging from header editable set |
| `app/templates/htmx/partials/parts/spec_edit.html` | Use context variable for condition choices instead of hardcoded list |
| `app/templates/htmx/partials/parts/spec_display.html` | Already exists — no changes needed |
| `tests/test_req_details_tab.py` | Add tests for archive guard, whitespace handling |

## Not Changing

- **No optimistic locking** — last-write-wins is acceptable for low-contention spec fields
- **No input length validation** — DB column constraints will enforce limits; this is internal tooling
- **No joinedload comment** — the query uses legacy API which auto-deduplicates
- **No archive guard on Requisition Info edits** — those routes operate on Requisition model (different status semantics); out of scope, noted as tech debt
- **No sibling row click-to-navigate** — sibling table is display-only; users navigate via left panel. Potential UX gap for future
- **No Escape key optimization** — Escape currently reloads the full tab via `reqDetailsRefresh`; heavy but correct. Could swap to local restore later

## Testing

- Existing 9 tests + 6 new uncommitted tests must continue passing
- Add test: spec edit on archived part returns 403
- Add test: whitespace-only value saves as NULL
- Extend `_make_requisition_and_parts()` helper with `**part_kwargs` to reduce fixture boilerplate across tests
- Run full `test_req_details_tab.py` before committing
