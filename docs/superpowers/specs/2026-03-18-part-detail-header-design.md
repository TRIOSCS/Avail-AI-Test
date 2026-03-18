# Sub-Project 2: Part Detail Header + Inline Editing

**Date:** 2026-03-18
**Status:** Approved
**Scope:** Fixed header strip above tabs in the parts workspace right panel, with inline editing for key Requirement fields.

This is the second of 7 sub-projects completing the requisitions workspace. It adds the missing structural layer — persistent part context — that all subsequent sub-projects (offer management, vendor outreach, quote generation) depend on.

---

## 1. Header Layout & Content

A fixed header strip above the tab bar, visible whenever a part is selected. Does NOT scroll with tab content. Compact height (48–56px), border-bottom separator, dense info layout.

### Fields

| Field | Display | Editable? | Edit Control |
|-------|---------|-----------|-------------|
| MPN | Large, bold — primary identifier | No | — |
| Brand | Next to MPN, smaller text | Yes | Text input |
| Target Qty | Formatted number | Yes | Number input |
| Target Price | Formatted $X.XXXX | Yes | Number input (step=0.0001) |
| Status | Colored badge (Open, Sourcing, Offered, etc.) | Yes | Select dropdown |
| Condition | Text (New, Used, etc.) | Yes | Select dropdown |
| Requisition | Parent req name + customer name | No | — (read-only context) |

Additional editable fields accessible but not shown by default: `notes`, `date_codes`, `packaging`. These can be edited via the same inline pattern but are not displayed in the compact header — they appear in a "More" expansion or are deferred to a future sub-project.

### Interaction Pattern

Click any editable field → inline edit cell appears (input or select) → Enter saves via PATCH, Escape cancels → field refreshes to display mode. This matches the existing inline edit pattern used for requisition inline editing (`htmx_views.py:954`), vendor headers (`htmx_views.py:2684`), and company headers (`htmx_views.py:3752`).

---

## 2. Workspace Integration

### Part Selection Flow (updated)

When a buyer clicks a part row in the left panel:

1. Sets `selectedPartId` in Alpine state
2. Fetches header via `GET /v2/partials/parts/{id}/header` → injects into `#part-header`
3. Fetches the active tab content (same as current behavior)
4. **Remembers the last active tab** instead of always resetting to offers — switching between parts preserves working context

### DOM Structure Change

```
#part-detail (right panel)
  ├── #part-header        ← NEW: fixed position, does not scroll
  ├── tab bar             ← existing, unchanged
  └── #tab-content        ← existing, scrollable
```

### Tab Content Cleanup

Each tab template currently has its own `<h3>` showing the part name (redundant once the header exists). Remove these headings from:

- `parts/tabs/offers.html`
- `parts/tabs/sourcing.html`
- `parts/tabs/activity.html`
- `parts/tabs/comms.html`

The header provides that context persistently.

---

## 3. Backend — Endpoints

Three thin routes following the existing inline edit pattern. No new models, no migrations, no new services.

### Display Header

```
GET /v2/partials/parts/{requirement_id}/header
```

- Fetches `Requirement` + joined `Requisition` (for customer/req name context)
- Returns `htmx/partials/parts/header.html` in display mode
- 404 if requirement not found

### Edit Cell

```
GET /v2/partials/parts/{requirement_id}/header/edit/{field}
```

- Returns inline edit cell HTML for the specified field
- `field` must be one of: `brand`, `target_qty`, `target_price`, `condition`, `sourcing_status`, `notes`, `date_codes`, `packaging`
- Returns 400 for invalid field names
- Edit cell includes: current value pre-filled, Enter to save (hx-patch), Escape to cancel (hx-get to re-fetch display)

### Save

```
PATCH /v2/partials/parts/{requirement_id}/header
```

- Reads `field` and `value` from form data
- Validates field name is in allowed set
- Saves to Requirement model
- Returns refreshed `header.html` in display mode
- Sets `HX-Trigger: {"part-updated": {"id": requirement_id}}` header so the left panel table row refreshes in sync
- For `sourcing_status` changes: validates against `ALLOWED_TRANSITIONS` in `requirement_status.py`

### Left Panel Sync

The left panel listens for `part-updated` events on the window and refreshes the affected row (or the full list). This ensures status changes, qty updates, etc. reflect immediately in both panels without a full page reload.

---

## 4. Template

### New File: `app/templates/htmx/partials/parts/header.html`

Compact header (~60–80 lines) showing:

- **Left section:** MPN (bold, text-lg) + Brand (text-sm, editable) + Requisition context (text-xs, gray)
- **Right section:** Status badge (editable dropdown) + Condition (editable) + Qty (editable) + Target Price (editable)

Each editable field wrapped in an Alpine-aware container:
- Display mode: shows value, click triggers `hx-get` to fetch edit cell
- Edit mode: swapped in via HTMX, `hx-patch` on form submit, `hx-get` on Escape to cancel

Styling matches existing workspace density: `text-sm` base, `px-3 py-2`, `border-b border-gray-200`.

---

## 5. Testing Strategy

| Test | What it verifies |
|------|-----------------|
| `test_get_part_header` | GET returns 200, contains MPN, qty, price, status badge |
| `test_get_part_header_not_found` | GET with bad ID returns 404 |
| `test_edit_cell_returns_input` | GET edit/{field} returns an input/select element |
| `test_edit_cell_invalid_field` | GET edit/bogus_field returns 400 |
| `test_patch_header_updates_field` | PATCH saves target_qty, returns updated header |
| `test_patch_header_status_change` | PATCH sourcing_status, verify it persists |
| `test_patch_header_hx_trigger` | PATCH response includes HX-Trigger header for list refresh |

7 tests following existing patterns from `test_archive_system.py`.

Target: 100% coverage maintained — no regressions.

---

## 6. Files Changed

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `app/routers/htmx_views.py` | 3 new routes (~120 lines) |
| Create | `app/templates/htmx/partials/parts/header.html` | Header template (~60–80 lines) |
| Modify | `app/templates/htmx/partials/parts/workspace.html` | Add `#part-header` div, update `selectPart()` |
| Modify | `app/templates/htmx/partials/parts/tabs/offers.html` | Remove redundant h3 heading |
| Modify | `app/templates/htmx/partials/parts/tabs/sourcing.html` | Remove redundant h3 heading |
| Modify | `app/templates/htmx/partials/parts/tabs/activity.html` | Remove redundant h3 heading |
| Modify | `app/templates/htmx/partials/parts/tabs/comms.html` | Remove redundant h3 heading |
| Create | `tests/test_part_header.py` | 7 tests |

---

## 7. Dependencies on Future Sub-Projects

| This Sub-Project Provides | Used By |
|--------------------------|---------|
| Persistent part context in header | Sub-Project 3 (Offer Management) — target price visible during offer comparison |
| Inline edit pattern for parts | Sub-Project 3 — offer accept/reject can follow same pattern |
| `part-updated` HX-Trigger event | Sub-Project 3, 4 — any tab mutation that affects the parts list |
| Tab memory (last active tab preserved) | All future sub-projects — better workflow continuity |

---

## 8. Constraints

- **No layout changes** to the split-panel structure — header is additive above tabs
- **No new dependencies** — HTMX + Alpine.js + existing inline edit pattern
- **No migrations** — all fields already exist on Requirement model
- **Design language unity** — header matches existing workspace density and colors
- **htmx_views.py growth** — ~120 lines across 3 thin handlers (minimal)
