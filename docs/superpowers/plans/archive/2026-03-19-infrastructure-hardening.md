# Infrastructure Hardening & Acceleration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden AvailAI's infrastructure with 8 improvements: crash-safe background tasks, faster fuzzy matching and JSON, cache invalidation on writes, a SearchBuilder utility, Jinja2 component macros, batch API cost savings, SSE real-time events, and worker deduplication.

**Architecture:** All changes slot into existing patterns — no new frameworks, no architectural shifts. Each task is independently deployable and testable. Priority order ensures the most critical (crash safety) ships first.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Redis, HTMX 2.x, Alpine.js 3.x, Jinja2, Anthropic Batch API, SSE (sse-starlette)

**Spec:** `docs/superpowers/specs/2026-03-19-infrastructure-hardening-design.md`

---

## File Map

### New Files
| File | Responsibility |
|------|---------------|
| `app/utils/json_helpers.py` | orjson wrapper (dumps/loads) for caching layer |
| `app/utils/search_builder.py` | Unified ILIKE + FTS query builder |
| `app/templates/htmx/partials/shared/_macros.html` | Jinja2 component macros (badges, buttons, pills, cards) |
| `app/services/batch_queue.py` | Batch API lifecycle helper (pending → submitted → completed) |
| `app/routers/events.py` | SSE stream endpoint for real-time notifications |
| `tests/test_json_helpers.py` | Tests for orjson wrapper |
| `tests/test_search_builder.py` | Tests for SearchBuilder |
| `tests/test_batch_queue.py` | Tests for batch queue helper |
| `tests/test_sse_events.py` | Tests for SSE event endpoint |
| `tests/test_search_worker_base.py` | Tests for consolidated worker base |

### Modified Files (by task)
| Task | Files Modified |
|------|---------------|
| 1 (background tasks) | `app/routers/tagging_admin.py`, `app/routers/materials.py`, `app/routers/crm/companies.py`, `app/routers/crm/sites.py`, `app/routers/crm/offers.py`, `app/routers/vendor_contacts.py`, `app/routers/prospect_suggested.py`, `app/services/buyplan_notifications.py`, `app/main.py`, `app/search_service.py` |
| 2 (rapidfuzz) | `requirements.txt`, `app/utils/vendor_helpers.py`, `app/routers/vendors_crud.py`, `app/company_utils.py`, `app/services/auto_dedup_service.py`, `app/vendor_utils.py` |
| 3 (orjson) | `requirements.txt`, `app/cache/decorators.py`, `app/cache/intel_cache.py` |
| 4 (cache invalidation) | `app/routers/requisitions/core.py`, `app/routers/materials.py` |
| 5 (SearchBuilder) | `app/routers/vendors_crud.py`, `app/routers/materials.py`, `app/routers/crm/companies.py`, `app/services/global_search_service.py`, `app/routers/htmx_views.py` |
| 6 (macros) | `partials/requisitions/req_row.html`, `partials/requisitions/detail_header.html`, `partials/buy_plans/detail.html`, `partials/sourcing/lead_card.html`, `partials/quotes/list.html` |
| 7 (Batch API) | `app/services/material_enrichment_service.py`, `app/services/signature_parser.py`, `app/jobs/core_jobs.py` |
| 8 (SSE) | `app/templates/htmx/base.html`, `app/routers/crm/offers.py`, `app/services/buyplan_notifications.py` |
| 9 (workers) | `app/services/search_worker_base/`, `app/services/ics_worker/`, `app/services/nc_worker/` |

---

### Task 1: Wrap All `asyncio.create_task()` with `safe_background_task()`

**Files:**
- Modify: `app/routers/tagging_admin.py` (lines 191, 214, 236, 262, 353, 404, 427, 481, 578, 592)
- Modify: `app/routers/materials.py:504`
- Modify: `app/routers/crm/companies.py:533`
- Modify: `app/routers/crm/sites.py:57`
- Modify: `app/routers/crm/offers.py:444`
- Modify: `app/routers/vendor_contacts.py:607`
- Modify: `app/routers/prospect_suggested.py:264`
- Modify: `app/services/buyplan_notifications.py:50`
- Modify: `app/main.py:154`
- Modify: `app/search_service.py:1677`

**Note:** `app/search_service.py:1823` (`asyncio.create_task(_run())` inside `asyncio.wait()`) is a **coordinated task**, not fire-and-forget. It must stay as `asyncio.create_task` because the caller tracks it via `task_map` and processes results from the `done` set. Wrapping it would swallow errors that the search flow depends on.

- [ ] **Step 1: Verify `safe_background_task` exists and works**

Run: `grep -n "async def safe_background_task" app/utils/async_helpers.py`
Expected: Shows the function definition at line 16. The function is already implemented and handles CancelledError re-raise and exception logging.

- [ ] **Step 2: Replace all `asyncio.create_task` calls in `tagging_admin.py`**

In `app/routers/tagging_admin.py`, add import at the top:
```python
from app.utils.async_helpers import safe_background_task
```

Replace every `asyncio.create_task(_run())` (lines 191, 214, 236, 262, 353, 404, 427, 481, 578, 592) with:
```python
await safe_background_task(_run(), task_name="<descriptive_name>")
```

Use descriptive names matching the endpoint: `"tag_enrich_batch"`, `"tag_apply_batch"`, `"tag_backfill"`, `"tag_cross_validate"`, `"tag_nexar_validate"`, etc.

Remove the `import asyncio` line if it's no longer needed after the replacement.

- [ ] **Step 3: Replace `asyncio.create_task` in remaining 9 files**

For each file, add the import and replace:

| File | Line | task_name |
|------|------|-----------|
| `app/routers/materials.py` | 504 | `"enrich_vendor_bg"` |
| `app/routers/crm/companies.py` | 533 | `"enrich_company_bg"` |
| `app/routers/crm/sites.py` | 57 | `"enrich_site_bg"` |
| `app/routers/crm/offers.py` | 444 | `"enrich_vendor_from_offer"` |
| `app/routers/vendor_contacts.py` | 607 | `"enrich_vendor_from_contact"` |
| `app/routers/prospect_suggested.py` | 264 | `"deep_enrichment_prospect"` |
| `app/services/buyplan_notifications.py` | 50 | `"buyplan_notification"` |
| `app/main.py` | 154 | `"warm_caches"` |
| `app/search_service.py` | 1677 | `"enrich_search_cards"` |

- [ ] **Step 4: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 5: Verify zero raw `asyncio.create_task` remains outside `async_helpers.py`**

Run: `grep -rn "asyncio.create_task" app/ --include="*.py" | grep -v async_helpers.py | grep -v __pycache__`
Expected: Only `app/search_service.py:1823` remains (coordinated task, intentionally kept as `asyncio.create_task`).

- [ ] **Step 6: Commit**

```bash
git add app/routers/tagging_admin.py app/routers/materials.py app/routers/crm/companies.py app/routers/crm/sites.py app/routers/crm/offers.py app/routers/vendor_contacts.py app/routers/prospect_suggested.py app/services/buyplan_notifications.py app/main.py app/search_service.py
git commit -m "fix: wrap all asyncio.create_task with safe_background_task for crash isolation"
```

---

### Task 2: Swap `thefuzz` for `rapidfuzz`

**Files:**
- Modify: `requirements.txt`
- Modify: `app/utils/vendor_helpers.py:144`
- Modify: `app/routers/vendors_crud.py:63`
- Modify: `app/company_utils.py:17`
- Modify: `app/services/auto_dedup_service.py:68`
- Modify: `app/vendor_utils.py:174,198`

- [ ] **Step 1: Update requirements.txt**

Replace `thefuzz[speedup]==0.22.1` with `rapidfuzz>=3.0.0`.

- [ ] **Step 2: Install new dependency**

Run: `cd /root/availai && pip install rapidfuzz && pip uninstall thefuzz python-Levenshtein -y`

- [ ] **Step 3: Update all import sites**

In each file, change `from thefuzz import fuzz` to `from rapidfuzz import fuzz`:
- `app/utils/vendor_helpers.py:144`
- `app/routers/vendors_crud.py:63`
- `app/company_utils.py:17`
- `app/services/auto_dedup_service.py:68`
- `app/vendor_utils.py:174` and `app/vendor_utils.py:198`

For files with conditional imports (try/except), update both the try and the fallback message.

- [ ] **Step 4: Run tests to verify scoring thresholds are unchanged**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v -k "fuzz or dedup or vendor" --tb=short`
Expected: All fuzzy matching tests PASS with same results.

- [ ] **Step 5: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 6: Verify no thefuzz imports remain**

Run: `grep -rn "thefuzz" app/ --include="*.py" | grep -v __pycache__`
Expected: Zero results.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt app/utils/vendor_helpers.py app/routers/vendors_crud.py app/company_utils.py app/services/auto_dedup_service.py app/vendor_utils.py
git commit -m "perf: swap thefuzz for rapidfuzz (10-100x faster fuzzy matching)"
```

---

### Task 3: Add `orjson` to Caching Layer

**Files:**
- Create: `app/utils/json_helpers.py`
- Create: `tests/test_json_helpers.py`
- Modify: `requirements.txt`
- Modify: `app/cache/decorators.py:14`
- Modify: `app/cache/intel_cache.py:10`

- [ ] **Step 1: Write the failing test**

Create `tests/test_json_helpers.py`:
```python
"""Tests for orjson wrapper in app/utils/json_helpers.py."""

from app.utils.json_helpers import dumps, loads


def test_dumps_returns_string():
    result = dumps({"key": "value", "num": 42})
    assert isinstance(result, str)
    assert '"key"' in result


def test_loads_parses_string():
    result = loads('{"key": "value", "num": 42}')
    assert result == {"key": "value", "num": 42}


def test_roundtrip():
    original = {"items": [1, 2, 3], "nested": {"a": True}}
    assert loads(dumps(original)) == original


def test_dumps_handles_none_values():
    result = dumps({"key": None})
    assert '"key"' in result
    assert "null" in result


def test_dumps_sort_keys_not_default():
    """orjson does not sort keys by default — verify wrapper behavior."""
    result = dumps({"b": 1, "a": 2})
    parsed = loads(result)
    assert parsed == {"b": 1, "a": 2}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_json_helpers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.utils.json_helpers'`

- [ ] **Step 3: Install orjson and create the wrapper**

Add `orjson>=3.9.0` to `requirements.txt`.
Run: `pip install orjson`

Create `app/utils/json_helpers.py`:
```python
"""json_helpers.py — Fast JSON serialization via orjson.

Drop-in replacement for stdlib json.dumps/json.loads, used in the caching
layer for faster Redis/PostgreSQL serialization.

Called by: app/cache/decorators.py, app/cache/intel_cache.py
Depends on: orjson
"""

import orjson


def dumps(obj, *, sort_keys: bool = False, default=None) -> str:
    """Serialize obj to JSON string.

    Mirrors json.dumps() signature for the params we use.
    Returns str (not bytes) for compatibility with Redis and SQL.
    """
    opts = 0
    if sort_keys:
        opts |= orjson.OPT_SORT_KEYS
    return orjson.dumps(obj, option=opts or None, default=default).decode()


def loads(s):
    """Deserialize JSON string or bytes to Python object."""
    return orjson.loads(s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_json_helpers.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Wire into caching layer**

In `app/cache/decorators.py`, replace line 14:
```python
# Before:
import json
# After:
from app.utils import json_helpers as json
```

In `app/cache/intel_cache.py`, replace line 10:
```python
# Before:
import json
# After:
from app.utils import json_helpers as json
```

- [ ] **Step 6: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS — caching behavior unchanged, just faster.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt app/utils/json_helpers.py tests/test_json_helpers.py app/cache/decorators.py app/cache/intel_cache.py
git commit -m "perf: use orjson in caching layer for 3-10x faster JSON serialization"
```

---

### Task 4: Add Missing Cache Invalidation on Writes

**Files:**
- Modify: `app/routers/requisitions/core.py:673,696`
- Modify: `app/routers/materials.py:161,202,238,275,300,322,347,359,496`

- [ ] **Step 1: Add invalidation to requisition claim/unclaim**

In `app/routers/requisitions/core.py`, verify `invalidate_prefix` is already imported (it is — used elsewhere in this file).

After line 673 (`db.commit()` in `claim_requisition_endpoint`), add:
```python
    invalidate_prefix("req_list")
```

After line 696 (`db.commit()` in `unclaim_requisition_endpoint`), add:
```python
    invalidate_prefix("req_list")
```

- [ ] **Step 2: Add invalidation to all materials write endpoints**

In `app/routers/materials.py`, `invalidate_prefix` is NOT currently imported. Add this import near the top with other cache imports:
```python
from app.cache.decorators import invalidate_prefix
```

After each `db.commit()` at lines 161, 202, 238, 275, 300, 322, 347, 359, and 496, add:
```python
    invalidate_prefix("material_list")
```

**Note:** Lines 161 and 202 are in `get_material()` and `get_material_by_mpn()` — these are GET endpoints that have a manufacturer-inference side-effect that calls `db.commit()`. Only add invalidation after those commits if the function actually mutated data (check for a conditional guard like `if inferred_manufacturer:`).

- [ ] **Step 3: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/routers/requisitions/core.py app/routers/materials.py
git commit -m "fix: add cache invalidation on writes for requisitions and materials"
```

---

### Task 5: Create SearchBuilder Utility

**Files:**
- Create: `app/utils/search_builder.py`
- Create: `tests/test_search_builder.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_builder.py`:
```python
"""Tests for SearchBuilder utility in app/utils/search_builder.py."""

import pytest
from unittest.mock import MagicMock
from sqlalchemy import Column, String

from app.utils.search_builder import SearchBuilder


class FakeModel:
    name = Column(String)
    industry = Column(String)


def test_init_strips_whitespace():
    sb = SearchBuilder("  hello  ")
    assert sb.q == "hello"


def test_init_escapes_like_chars():
    sb = SearchBuilder("test%value_here")
    assert "\\%" in sb.safe
    assert "\\_" in sb.safe


def test_empty_query():
    sb = SearchBuilder("")
    assert sb.q == ""
    assert sb.safe == ""


def test_ilike_filter_generates_contains_pattern():
    sb = SearchBuilder("test")
    filt = sb.ilike_filter(FakeModel.name)
    # Should produce an or_() clause with ILIKE %test%
    assert filt is not None


def test_ilike_filter_prefix_mode():
    sb = SearchBuilder("test")
    filt = sb.ilike_filter(FakeModel.name, prefix=True)
    assert filt is not None


def test_ilike_filter_multiple_columns():
    sb = SearchBuilder("test")
    filt = sb.ilike_filter(FakeModel.name, FakeModel.industry)
    assert filt is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_builder.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the SearchBuilder implementation**

Create `app/utils/search_builder.py`:
```python
"""search_builder.py — Unified search query builder for ILIKE and optional FTS.

Consolidates the escape_like + ILIKE pattern used across 13 files and the
FTS-with-fallback cascade in vendors_crud.py into a single reusable utility.

Called by: routers and services that build search queries
Depends on: app.utils.sql_helpers.escape_like, sqlalchemy
"""

from sqlalchemy import or_, text as sqltext, true as sa_true
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.utils.sql_helpers import escape_like


class SearchBuilder:
    """Build ILIKE and FTS filters from user search input.

    Usage:
        sb = SearchBuilder("resistor 100k")
        query = query.filter(sb.ilike_filter(Material.description, Material.mpn))

        # Or with FTS fallback:
        query = sb.fts_or_fallback(query, VendorCard, [VendorCard.normalized_name])
    """

    def __init__(self, q: str):
        self.q = q.strip()
        self.safe = escape_like(self.q)

    def ilike_filter(self, *columns, prefix=False):
        """Return an or_() filter across columns using ILIKE.

        Args:
            *columns: SQLAlchemy column objects to search
            prefix: If True, use 'term%' instead of '%term%'

        Returns:
            SQLAlchemy BooleanClauseList (or_() of ILIKE filters)
        """
        if not self.q:
            # Empty search — match everything
            return sa_true()
        pattern = f"{self.safe}%" if prefix else f"%{self.safe}%"
        return or_(*[col.ilike(pattern) for col in columns])

    def fts_or_fallback(self, query, model, fallback_columns, *, min_len=3):
        """Try PostgreSQL full-text search, fall back to ILIKE.

        Uses model.search_vector for FTS if available and query is long enough.
        Falls back to ILIKE on fallback_columns if FTS returns no results,
        isn't available (SQLite in tests), or the query is too short.

        Args:
            query: SQLAlchemy query object to filter
            model: SQLAlchemy model class (must have search_vector column for FTS)
            fallback_columns: List of columns for ILIKE fallback
            min_len: Minimum query length to attempt FTS (default 3)

        Returns:
            Filtered query object
        """
        if not self.q or len(self.q) < min_len or not hasattr(model, "search_vector"):
            return query.filter(self.ilike_filter(*fallback_columns))

        try:
            fts_query = (
                query.filter(
                    model.search_vector.isnot(None),
                    sqltext("search_vector @@ plainto_tsquery('english', :q)"),
                )
                .params(q=self.q)
                .order_by(
                    sqltext(
                        "ts_rank(search_vector, plainto_tsquery('english', :q)) DESC"
                    )
                )
                .params(q=self.q)
            )
            if fts_query.count() > 0:
                return fts_query
            return query.filter(self.ilike_filter(*fallback_columns))
        except (ProgrammingError, OperationalError):
            return query.filter(self.ilike_filter(*fallback_columns))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_builder.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit the utility (before migrating callers)**

```bash
git add app/utils/search_builder.py tests/test_search_builder.py
git commit -m "feat: add SearchBuilder utility for unified ILIKE and FTS queries"
```

---

### Task 6: Migrate Top 5 Files to SearchBuilder

**Files:**
- Modify: `app/routers/vendors_crud.py`
- Modify: `app/routers/materials.py`
- Modify: `app/routers/crm/companies.py`
- Modify: `app/services/global_search_service.py`
- Modify: `app/routers/htmx_views.py`

- [ ] **Step 1: Migrate `vendors_crud.py` (includes FTS)**

Read `app/routers/vendors_crud.py` and find the search block (around lines 142-180). Replace the manual `escape_like` + ILIKE + FTS cascade with:

```python
from app.utils.search_builder import SearchBuilder

sb = SearchBuilder(q)
# ... existing tag_filter stays as-is ...
name_filter = sb.ilike_filter(VendorCard.normalized_name)
# For the FTS block, replace the entire if/try/except with:
query = sb.fts_or_fallback(query, VendorCard, [VendorCard.normalized_name])
```

Adapt to preserve existing tag filtering logic — SearchBuilder handles the name/FTS part, tags stay as-is.

- [ ] **Step 2: Run vendor tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v -k "vendor" --tb=short`
Expected: All vendor tests PASS.

- [ ] **Step 3: Migrate `materials.py`**

Read `app/routers/materials.py` and find the search block. Replace `escape_like` + ILIKE with SearchBuilder. Note: materials uses prefix match (`{safe}%`) for MPN — use `sb.ilike_filter(col, prefix=True)`.

- [ ] **Step 4: Migrate `crm/companies.py`**

Read `app/routers/crm/companies.py` and find the search block. Replace `escape_like` + ILIKE with SearchBuilder.

- [ ] **Step 5: Migrate `global_search_service.py`**

Read `app/services/global_search_service.py` and find the search blocks. Replace `escape_like` + ILIKE with SearchBuilder.

- [ ] **Step 6: Migrate `htmx_views.py`**

Read `app/routers/htmx_views.py` and find all search blocks (there are ~12 `escape_like` calls). Replace each with SearchBuilder. This is the largest file — take care to preserve all existing filtering logic.

- [ ] **Step 7: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 8: Verify reduced escape_like usage**

Run: `grep -rn "escape_like" app/ --include="*.py" | grep -v __pycache__ | grep -v search_builder.py | grep -v sql_helpers.py`
Expected: Significantly fewer results than the original 45 (remaining files migrate later).

- [ ] **Step 9: Commit**

```bash
git add app/routers/vendors_crud.py app/routers/materials.py app/routers/crm/companies.py app/services/global_search_service.py app/routers/htmx_views.py
git commit -m "refactor: migrate top 5 search files to SearchBuilder utility"
```

---

### Task 7: Create Jinja2 Component Macro Library

**Files:**
- Create: `app/templates/htmx/partials/shared/_macros.html`

- [ ] **Step 1: Read existing status badge patterns**

Read these files to extract the exact color maps and HTML patterns currently in use:
- `app/templates/htmx/partials/requisitions/req_row.html`
- `app/templates/htmx/partials/requisitions/detail_header.html`
- `app/templates/htmx/partials/buy_plans/detail.html`
- `app/templates/htmx/partials/sourcing/lead_card.html`
- `app/templates/htmx/partials/quotes/list.html`

- [ ] **Step 2: Create the macro library**

Create `app/templates/htmx/partials/shared/_macros.html` with macros that produce **identical HTML** to the patterns found in Step 1. Include:

- `status_badge(value, status_map=None)` — with default map covering: active, draft, won, lost, archived, rejected, reviewed, flagged, pending, expired, cancelled
- `risk_badge(level)` — low_risk, medium_risk, high_risk, unknown
- `urgency_badge(value)` — critical (rose), hot (amber), normal (gray), with SVG icons matching the existing ones
- `btn_primary(text, attrs={})` — brand-500 bg, passes through HTMX attrs
- `btn_secondary(text, attrs={})` — white bg, gray border
- `btn_danger(text, attrs={})` — rose-500 bg
- `filter_pill(label, value, current, attrs={})` — active/inactive states with brand colors
- `stat_card(label, value, subtitle=None)` — metric display card

Each macro must produce HTML that is **visually identical** to what's currently hardcoded.

- [ ] **Step 3: Verify macros render correctly**

Start the dev server and manually load a page that would use these macros. Confirm the HTML output matches.

- [ ] **Step 4: Commit the macro library (before migrating templates)**

```bash
git add app/templates/htmx/partials/shared/_macros.html
git commit -m "feat: add Jinja2 component macro library for badges, buttons, pills, cards"
```

---

### Task 8: Migrate 5 Templates to Use Macros

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/req_row.html`
- Modify: `app/templates/htmx/partials/requisitions/detail_header.html`
- Modify: `app/templates/htmx/partials/buy_plans/detail.html`
- Modify: `app/templates/htmx/partials/sourcing/lead_card.html`
- Modify: `app/templates/htmx/partials/quotes/list.html`

- [ ] **Step 1: Migrate `req_row.html`**

Read the file. Add import at top:
```jinja2
{% from "htmx/partials/shared/_macros.html" import status_badge, urgency_badge %}
```
Replace inline `{% set status_colors = {...} %}` + `<span>` blocks with `{{ status_badge(req.status) }}`.
Replace inline urgency blocks with `{{ urgency_badge(req.urgency) }}`.

- [ ] **Step 2: Migrate `detail_header.html`**

Same pattern — import macros, replace inline status/urgency badges.

- [ ] **Step 3: Migrate `buy_plans/detail.html`**

Import macros. Replace status badges and stat cards with macro calls.

- [ ] **Step 4: Migrate `sourcing/lead_card.html`**

Import macros. Replace status and risk badges with macro calls.

- [ ] **Step 5: Migrate `quotes/list.html`**

Import macros. Replace status badges and filter pills with macro calls.

- [ ] **Step 6: Verify visual output is identical**

Start the dev server and visually compare each migrated page against the current output. The rendered HTML must be identical.

- [ ] **Step 7: Commit**

```bash
git add app/templates/htmx/partials/requisitions/req_row.html app/templates/htmx/partials/requisitions/detail_header.html app/templates/htmx/partials/buy_plans/detail.html app/templates/htmx/partials/sourcing/lead_card.html app/templates/htmx/partials/quotes/list.html
git commit -m "refactor: migrate 5 templates to use shared macro library"
```

---

### Task 9: Create Batch Queue Helper

**Files:**
- Create: `app/services/batch_queue.py`
- Create: `tests/test_batch_queue.py`

- [ ] **Step 1: Read existing batch patterns**

Read `app/services/tagging_ai_batch.py` and `app/email_service.py` (around line 839 and 1053) to understand the existing batch submit/poll pattern.

- [ ] **Step 2: Write failing tests**

Create `tests/test_batch_queue.py`:
```python
"""Tests for batch queue lifecycle helper."""

from app.services.batch_queue import BatchQueue


def test_enqueue_adds_item():
    bq = BatchQueue(prefix="test")
    bq.enqueue("item_1", {"prompt": "test", "schema": {}})
    assert bq.pending_count() == 1


def test_build_batch_returns_requests():
    bq = BatchQueue(prefix="test")
    bq.enqueue("item_1", {"prompt": "test", "schema": {}})
    requests = bq.build_batch()
    assert len(requests) == 1
    assert requests[0]["custom_id"] == "test:item_1"


def test_empty_queue_returns_empty_batch():
    bq = BatchQueue(prefix="test")
    assert bq.build_batch() == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_batch_queue.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement BatchQueue**

Create `app/services/batch_queue.py`:
```python
"""batch_queue.py — Lifecycle helper for Claude Batch API submissions.

Manages the pending → submitted → completed cycle for batch AI processing.
Wraps claude_batch_submit/claude_batch_results with queue management.

Called by: scheduler jobs, enrichment services
Depends on: app.utils.claude_client
"""

from loguru import logger


class BatchQueue:
    """In-memory batch queue for collecting items before batch submission.

    Usage:
        bq = BatchQueue(prefix="material_enrich")
        bq.enqueue("mat_123", {"prompt": "...", "schema": {...}})
        bq.enqueue("mat_456", {"prompt": "...", "schema": {...}})
        requests = bq.build_batch()
        # Submit via claude_batch_submit(requests)
    """

    def __init__(self, prefix: str):
        self.prefix = prefix
        self._pending: dict[str, dict] = {}

    def enqueue(self, item_id: str, request: dict) -> None:
        """Add an item to the pending queue."""
        self._pending[item_id] = request

    def pending_count(self) -> int:
        """Return number of items waiting for batch submission."""
        return len(self._pending)

    def build_batch(self) -> list[dict]:
        """Build batch request list from pending items.

        Returns list of dicts ready for claude_batch_submit().
        Clears the pending queue.
        """
        if not self._pending:
            return []

        requests = []
        for item_id, req in self._pending.items():
            requests.append({
                "custom_id": f"{self.prefix}:{item_id}",
                "prompt": req["prompt"],
                "schema": req["schema"],
                "system": req.get("system", ""),
                "model_tier": req.get("model_tier", "fast"),
                "max_tokens": req.get("max_tokens", 1024),
            })

        self._pending.clear()
        logger.info("Built batch of %d items for prefix '%s'", len(requests), self.prefix)
        return requests
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_batch_queue.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/batch_queue.py tests/test_batch_queue.py
git commit -m "feat: add BatchQueue helper for Batch API lifecycle management"
```

---

### Task 10: Convert Material Enrichment to Batch API

**Note on scope:** The spec identifies 4 batch candidates (material enrichment, vendor contacts, signatures, knowledge service). This plan implements 2 (material enrichment + signatures) to meet the success criterion of "at least 2 additional services." Vendor contact enrichment and knowledge service are left for a follow-up plan — they have more complex data flows that need their own design pass.

**Files:**
- Modify: `app/services/material_enrichment_service.py`
- Modify: `app/jobs/core_jobs.py`

- [ ] **Step 1: Read current material enrichment implementation**

Read `app/services/material_enrichment_service.py` to understand the current per-material `claude_structured()` call pattern, the schema used, and the system prompt. Note the exact schema dict and system prompt string — these must be reused identically in the batch version.

- [ ] **Step 2: Write failing test for batch enrichment**

Create a test in the appropriate test file that verifies `batch_enrich_materials(db)` collects unenriched materials and returns a batch_id. Mock `claude_batch_submit` to avoid real API calls.

- [ ] **Step 3: Add batch enrichment function**

Add a new function `batch_enrich_materials(db)` to `material_enrichment_service.py` that:
1. Queries for materials with `enriched_at IS NULL` (up to 200)
2. Builds a `BatchQueue` with the **same prompt/schema** as the existing real-time enrichment
3. Submits via `claude_batch_submit()`
4. Stores the batch_id in Redis key `batch:material_enrich:current` for later polling
5. Returns the batch_id or None if no materials to enrich

Keep the existing real-time `enrich_material()` function unchanged as the fallback for "enrich now" user actions.

- [ ] **Step 4: Write failing test for batch results processor**

Test that `process_material_batch_results(db)` applies results correctly when batch is complete. Mock `claude_batch_results` to return test data.

- [ ] **Step 5: Add batch results processor**

Add a function `process_material_batch_results(db)` that:
1. Loads the batch_id from Redis key `batch:material_enrich:current`
2. Calls `claude_batch_results(batch_id)`
3. If status is `"ended"`, iterate results and apply to each material (same field-mapping logic as real-time enrichment)
4. Sets `enriched_at = utcnow()` on each successfully enriched material
5. Clears the Redis key

- [ ] **Step 6: Add scheduler jobs**

In `app/jobs/core_jobs.py`, add two new jobs:
- `batch_enrich_materials_job` — runs every 30 minutes, calls `batch_enrich_materials(db)`
- `poll_material_batch_results_job` — runs every 5 minutes, calls `process_material_batch_results(db)`

- [ ] **Step 7: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/material_enrichment_service.py app/jobs/core_jobs.py
git commit -m "feat: add batch material enrichment via Claude Batch API (50% cost savings)"
```

---

### Task 11: Convert Signature Parsing to Batch API

**Files:**
- Modify: `app/services/signature_parser.py`
- Modify: `app/jobs/core_jobs.py`

- [ ] **Step 1: Read current signature parsing implementation**

Read `app/services/signature_parser.py` to understand the `claude_json()` call, schema, and system prompt. Note the exact parameters used.

- [ ] **Step 2: Write failing test for batch signature parsing**

Test that `batch_parse_signatures(db)` collects unparsed signatures and returns a batch_id. Mock `claude_batch_submit`.

- [ ] **Step 3: Add batch signature parsing**

Follow the same pattern as Task 10:
1. Add `batch_parse_signatures(db)` — queries for signatures where `parsed_at IS NULL`, builds BatchQueue with same prompt/schema as real-time, submits, stores batch_id in Redis key `batch:signature_parse:current`
2. Add `process_signature_batch_results(db)` — polls and applies results using same field-mapping logic
3. Keep the real-time `parse_signature()` for immediate single-item use

- [ ] **Step 4: Add scheduler jobs**

In `app/jobs/core_jobs.py`, add:
- `batch_parse_signatures_job` — runs every 10 minutes
- `poll_signature_batch_results_job` — runs every 5 minutes

- [ ] **Step 5: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/signature_parser.py app/jobs/core_jobs.py
git commit -m "feat: add batch signature parsing via Batch API"
```

---

### Task 12: Create SSE Event Endpoint

**Files:**
- Create: `app/routers/events.py`
- Create: `tests/test_sse_events.py`
- Modify: `app/main.py` (register router)

- [ ] **Step 1: Write failing test**

Create `tests/test_sse_events.py`:
```python
"""Tests for SSE event endpoint."""

from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.main import app


def test_sse_endpoint_requires_auth():
    client = TestClient(app)
    resp = client.get("/api/events/stream")
    assert resp.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sse_events.py -v`
Expected: FAIL (endpoint doesn't exist yet — 404)

- [ ] **Step 3: Implement SSE event endpoint**

Create `app/routers/events.py`:
```python
"""events.py — SSE stream endpoint for real-time notifications.

Provides a single SSE connection per user session. Events are published
by background tasks and services via the SSE broker.

Called by: base.html (sse-connect attribute)
Depends on: app/services/sse_broker.py, app/dependencies.py
"""

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from app.dependencies import require_user
from app.services.sse_broker import broker

router = APIRouter(tags=["events"])


@router.get("/api/events/stream")
async def event_stream(request: Request, user=Depends(require_user)):
    """SSE endpoint — one connection per user session.

    Listens on user-specific channel and yields events as they arrive.
    Frontend connects via hx-ext="sse" sse-connect="/api/events/stream".
    """

    async def generate():
        async for msg in broker.listen(f"user:{user.id}"):
            if await request.is_disconnected():
                break
            yield {"event": msg["event"], "data": msg.get("data", "")}

    return EventSourceResponse(generate())
```

Register in `app/main.py`:
```python
from app.routers.events import router as events_router
app.include_router(events_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sse_events.py -v`
Expected: PASS (401 for unauthenticated).

- [ ] **Step 5: Commit**

```bash
git add app/routers/events.py tests/test_sse_events.py app/main.py
git commit -m "feat: add SSE event stream endpoint for real-time notifications"
```

---

### Task 13: Wire SSE Events into Frontend and Backend

**Files:**
- Modify: `app/templates/htmx/base.html`
- Modify: Background task callers (enrichment services, offer creation)

- [ ] **Step 1: Add SSE connection to base layout**

In `app/templates/htmx/base.html`, add SSE connection to the body or main content div:
```html
<div id="sse-listener"
     hx-ext="sse"
     sse-connect="/api/events/stream"
     sse-swap="none"
     style="display:none">
</div>
```

Add Alpine.js listener for toast notifications:
```html
<script>
  document.getElementById('sse-listener').addEventListener('sse:enrichment_complete', function(e) {
    Alpine.store('toast').show('Enrichment complete', 'success');
  });
  document.getElementById('sse-listener').addEventListener('sse:quote_updated', function(e) {
    Alpine.store('toast').show('Quote updated', 'info');
  });
  document.getElementById('sse-listener').addEventListener('sse:rfq_response', function(e) {
    Alpine.store('toast').show('New RFQ response received', 'info');
  });
</script>
```

- [ ] **Step 2: Publish enrichment_complete events from background tasks**

In each service that does background enrichment (companies, vendors, materials), add after successful enrichment:
```python
from app.services.sse_broker import broker
await broker.publish(f"user:{user_id}", "enrichment_complete", json.dumps({"entity_type": "vendor", "entity_id": card_id}))
```

**Important:** Background tasks must receive `user_id` as a parameter when dispatched (not from request context, which is gone by the time the task runs). Pass `user.id` from the route handler into the background coroutine. Example: `await safe_background_task(_enrich_company_bg(company_id, user.id), task_name="enrich_company_bg")`.

Start with vendor enrichment in `app/routers/crm/companies.py` as the first proving ground.

- [ ] **Step 3: Publish quote_updated events**

In `app/routers/crm/offers.py`, after creating an offer:
```python
await broker.publish(f"user:{user_id}", "quote_updated", json.dumps({"quote_id": quote_id}))
```

- [ ] **Step 4: Test end-to-end**

Start the dev server. Open a detail page. Trigger an enrichment. Verify the toast notification appears.

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/base.html app/routers/crm/companies.py app/routers/crm/offers.py
git commit -m "feat: wire SSE events for enrichment and quote notifications"
```

---

### Task 14: Diff and Test SearchWorkerBase

**Files:**
- Create: `tests/test_search_worker_base.py`

- [ ] **Step 1: Read and diff the two workers**

Read all modules in `app/services/ics_worker/` and `app/services/nc_worker/`. Identify exactly which files are identical, which differ only in config values, and which have genuinely different logic.

Compare side-by-side: `circuit_breaker.py`, `session_manager.py`, `queue_manager.py`, `monitoring.py`, `search_engine.py`, `ai_gate.py`, `sighting_writer.py`, `mpn_normalizer.py`.

Document findings: which modules are identical, which differ, and what the differences are.

- [ ] **Step 2: Write tests for the shared base logic**

Create `tests/test_search_worker_base.py` with tests for the shared modules (circuit breaker state transitions, session lifecycle, queue management, monitoring health check).

- [ ] **Step 3: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_worker_base.py -v`
Expected: FAIL (base class doesn't have the logic yet)

- [ ] **Step 4: Commit test scaffold**

```bash
git add tests/test_search_worker_base.py
git commit -m "test: add test scaffold for SearchWorkerBase consolidation"
```

---

### Task 15: Move Shared Modules into SearchWorkerBase

**Files:**
- Modify: `app/services/search_worker_base/` (absorb shared modules)

- [ ] **Step 1: Move shared modules into `search_worker_base/`**

Move the following from `ics_worker/` into `search_worker_base/` (or merge if partial versions already exist):
- `circuit_breaker.py`
- `session_manager.py`
- `queue_manager.py`
- `search_engine.py` (abstract base with `parse_results` hook)
- `ai_gate.py`
- `sighting_writer.py`
- `mpn_normalizer.py`

For each module, use the ICS version as the base (it was the original) and parameterize any hardcoded ICS-specific values.

- [ ] **Step 2: Run base tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_worker_base.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add app/services/search_worker_base/
git commit -m "refactor: move shared worker modules into search_worker_base"
```

---

### Task 16: Migrate ICS Worker to Use SearchWorkerBase

**Files:**
- Modify: `app/services/ics_worker/` (reduce to config + parsing)

- [ ] **Step 1: Reduce ICS worker to thin subclass**

Modify `app/services/ics_worker/worker.py` to import from `search_worker_base` and only override:
- Config values (API URL, credentials, concurrency limits)
- `parse_results()` (ICS-specific response parsing)
- Any ICS-specific auth flow

Delete modules now provided by `search_worker_base` (circuit_breaker.py, session_manager.py, etc.) from `ics_worker/`.

- [ ] **Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS — ICS worker still functions correctly via the base.

- [ ] **Step 3: Commit**

```bash
git add app/services/ics_worker/
git commit -m "refactor: reduce ICS worker to thin subclass of SearchWorkerBase"
```

---

### Task 17: Migrate NC Worker to Use SearchWorkerBase

**Files:**
- Modify: `app/services/nc_worker/` (reduce to config + parsing)

- [ ] **Step 1: Reduce NC worker to thin subclass**

Same as Task 16 but for NC worker. Import from `search_worker_base`, override only NC-specific parts. Delete modules now provided by base.

- [ ] **Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 3: Verify no duplicated logic remains**

Compare the ICS and NC worker directories — each should have only `worker.py` (thin subclass), `config.py` (service-specific config), and `result_parser.py` (service-specific parsing).

- [ ] **Step 4: Commit**

```bash
git add app/services/nc_worker/
git commit -m "refactor: reduce NC worker to thin subclass of SearchWorkerBase"
```

---

### Task 18: Final Verification & Coverage Check

- [ ] **Step 1: Run full test suite with coverage**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
Expected: 100% coverage maintained (or improved). No regression.

- [ ] **Step 2: Verify all success criteria**

Check each criterion:
1. `grep -rn "asyncio.create_task" app/ --include="*.py" | grep -v async_helpers.py | grep -v __pycache__` → only `search_service.py:1823` (coordinated task, intentionally kept)
2. `grep -rn "thefuzz" app/ --include="*.py" | grep -v __pycache__` → zero results
3. `grep -rn "import json$" app/cache/ --include="*.py"` → zero results (all using json_helpers)
4. Review materials.py and requisitions/core.py for `invalidate_prefix` calls after all `db.commit()` operations
5. Verify SearchBuilder is used in the 5 target files
6. Verify _macros.html exists and is imported in 5 target templates
7. Verify batch enrichment jobs exist in core_jobs.py
8. Verify SSE endpoint at `/api/events/stream` is registered
9. Verify worker directories — shared logic in base, thin subclasses

- [ ] **Step 3: Commit any final fixes**

If any criterion fails, fix and commit before marking complete.
