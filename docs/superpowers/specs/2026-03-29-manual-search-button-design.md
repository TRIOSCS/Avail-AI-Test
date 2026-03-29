# Manual Search Button — Design Spec

**Date:** 2026-03-29
**Status:** Approved

## Problem

When the system doesn't auto-fire a search on requirement save, or when results go stale (3+ days), users have no way to manually trigger a fresh search from the RFQ parts tab. The sightings page has a bare "Refresh" button but lacks a staleness indicator or rate protection.

## Solution

Add a manual search button with "Searched X ago" timestamps to both the RFQ parts tab (batch toolbar) and sightings detail panel. Protect API budget with a server-side rate guard.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Per-requirement `last_searched_at` | Add column to `Requirement` model | Different requirements in the same RFQ can be searched at different times; parent-level timestamp loses this granularity |
| Per-row search button on RFQ tab | **Cut** | Search is async — row swap timing is wrong. Batch toolbar button is sufficient |
| Cooldown mechanism | Server-side 5-min guard returning a toast | Simpler than client-side Alpine timers; no state lost on navigation; more reliable |
| Confirmation popover | **Cut** | Server-side guard handles rate protection; no UI friction needed for a non-destructive action |
| Shared macro | Add to existing `_macros.html` | Avoids duplication between sightings detail and future uses; follows existing pattern (`btn_primary`, `btn_secondary`, etc.) |
| New endpoints | **None** | Reuse existing `sightings/{id}/refresh` and `sightings/batch-refresh` |
| New Jinja2 filter | **None** | `timeago` already exists at `template_env.py:83` |
| Icon | Magnifier (not refresh/cycle) | This is a search action hitting external APIs, consistent with "Search All Sources" button |

## Data Model

### New column: `Requirement.last_searched_at`

```python
# app/models/sourcing.py — Requirement class, after created_at
last_searched_at = Column(DateTime)
```

- Nullable, default `None` ("Never searched")
- No index needed — display-only, not filtered in queries
- Stamped inside `search_service.search_requirement()` after successful search — single location covers all callers (auto-search-on-save, manual refresh, batch refresh, scheduler job)

### Migration

- `alembic revision --autogenerate -m "add_requirement_last_searched_at"`
- Backfill in `upgrade()`:
  ```sql
  UPDATE requirements
  SET last_searched_at = (
    SELECT last_searched_at FROM requisitions
    WHERE requisitions.id = requirements.requisition_id
  )
  WHERE last_searched_at IS NULL
  ```
- Backfill goes in the migration, not `startup.py` (per CLAUDE.md absolute rules)

## Search Service Change

In `app/search_service.py`, inside `search_requirement()`, after `write_db.commit()`:

```python
write_req.last_searched_at = now  # `now` already computed at top of function
```

This stamps the field for all callers: sightings refresh, batch-refresh, search-all, and the scheduler job.

### Belt-and-suspenders in `sightings_refresh()`

After `search_requirement()` returns, the caller's session object is stale. Set `requirement.last_searched_at` explicitly in the endpoint too, then `db.commit()`. This avoids an extra `db.refresh()` round-trip.

## Server-Side Rate Guard

In both `sightings_refresh()` and `sightings_batch_refresh()`, before calling `search_requirement()`:

```python
from datetime import datetime, timezone

now = datetime.now(timezone.utc)
if requirement.last_searched_at and (now - requirement.last_searched_at).total_seconds() < 300:
    return _oob_toast("Already searched within the last 5 minutes.", "info")
```

- 5-minute threshold (configurable later via `settings` if needed)
- Returns an informational toast — no confirmation gate, no client-side timer
- For batch: count recently-searched items, skip them, include count in toast: "Searched 3/5 requirements. 2 were already fresh."
- Uses existing `_oob_toast()` helper in `sightings.py`

## Template: Shared Macro

Add `search_button` macro to `app/templates/htmx/partials/shared/_macros.html`:

```jinja2
{% macro search_button(requirement, target="#sightings-detail", swap="innerHTML") %}
<div class="flex items-center gap-2">
  <button hx-post="/v2/partials/sightings/{{ requirement.id }}/refresh"
          hx-target="{{ target }}"
          hx-swap="{{ swap }}"
          hx-indicator="#search-spinner-{{ requirement.id }}"
          class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-gray-200
                 text-xs font-medium text-gray-600 hover:bg-gray-50 transition-colors">
    {# Magnifier icon — search action, not a refresh #}
    <svg id="search-spinner-{{ requirement.id }}" class="h-3.5 w-3.5 htmx-indicator animate-spin"
         fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round"
            d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/>
    </svg>
    Search
  </button>
  {% if requirement.last_searched_at %}
  <span class="text-[10px] text-gray-400">{{ requirement.last_searched_at|timeago }}</span>
  {% else %}
  <span class="text-[10px] text-gray-400">Never searched</span>
  {% endif %}
</div>
{% endmacro %}
```

**Notes:**
- `target` and `swap` are macro parameters — sightings detail passes `#sightings-detail` / `innerHTML`; other contexts can override
- Spinner uses `htmx-indicator` class (hidden by default, shown during request) — matches existing pattern
- Magnifier icon matches "Search All Sources" button in parts tab

## Template: Sightings Detail Panel

In `app/templates/htmx/partials/sightings/detail.html`:

- Import: `{% from "htmx/partials/shared/_macros.html" import search_button %}`
- Replace lines 20-29 (bare Refresh button) with: `{{ search_button(requirement) }}`
- The `sightings_detail` view already passes `requirement` in context — no server change needed

## Template: RFQ Parts Tab Toolbar

In `app/templates/htmx/partials/requisitions/tabs/parts.html`:

- Add checkbox selection state: `x-data="{ selectedReqIds: [] }"` on the outer wrapper
- Add checkbox `<th>` in header and checkbox `<td>` in `req_row.html`, wired to `selectedReqIds`
- Update `colspan="16"` to `colspan="17"` in `req_row.html` edit mode
- Add "Search Selected" button next to existing "Search All Sources":
  ```html
  <button x-show="selectedReqIds.length > 0"
          hx-post="/v2/partials/sightings/batch-refresh"
          hx-vals="js:{requirement_ids: JSON.stringify($data.selectedReqIds)}"
          hx-swap="none"
          class="...">
    Search Selected
  </button>
  ```
- `hx-swap="none"` because `batch-refresh` returns OOB toast only

## Sightings Batch Refresh Enhancement

Modify existing `sightings_batch_refresh()` in `app/routers/sightings.py`:

- Before the search loop, count requirements with `last_searched_at` within 5 minutes
- Skip recently-searched items
- Include skip count in toast: "Searched 3/5 requirements. 2 skipped (already fresh)."

## Files Modified

| File | Change |
|------|--------|
| `app/models/sourcing.py` | Add `last_searched_at` to `Requirement` |
| `alembic/versions/[new].py` | Migration + backfill |
| `app/search_service.py` | Stamp `last_searched_at` after successful search |
| `app/routers/sightings.py` | Rate guard in `refresh` + `batch-refresh`; belt-and-suspenders stamp |
| `app/templates/htmx/partials/shared/_macros.html` | Add `search_button` macro |
| `app/templates/htmx/partials/sightings/detail.html` | Replace bare Refresh with macro |
| `app/templates/htmx/partials/requisitions/tabs/parts.html` | Add checkbox state + "Search Selected" toolbar button |
| `app/templates/htmx/partials/requisitions/tabs/req_row.html` | Add checkbox `<td>`, update colspan |

## Files NOT Modified

- No new template files (macro goes in existing `_macros.html`)
- No new endpoints (reuse `sightings/{id}/refresh` and `sightings/batch-refresh`)
- No new Jinja2 filters (`timeago` already exists)
- No new Alpine stores or CSS classes
- No `template_env.py` changes

## Build Sequence

1. **Data layer** — model column + migration + backfill
2. **Service layer** — stamp in `search_requirement()`
3. **Endpoint updates** — rate guard + belt-and-suspenders stamp
4. **Sightings detail template** — macro + replacement
5. **RFQ parts tab template** — checkboxes + toolbar button
6. **Full test suite** — pytest + ruff + mypy
