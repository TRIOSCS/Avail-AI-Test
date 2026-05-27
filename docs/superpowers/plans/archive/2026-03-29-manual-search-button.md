# Manual Search Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add manual search buttons with "Searched X ago" timestamps to the RFQ parts tab (batch) and sightings detail panel, with server-side rate protection.

**Architecture:** Add `last_searched_at` column to `Requirement` model, stamp it inside `search_requirement()` (single source of truth). Add a `search_button` Jinja2 macro to `_macros.html`. Replace the bare sightings Refresh button with the macro. Add a "Search Selected" batch button to the RFQ parts tab toolbar. Protect API budget with a server-side 5-minute rate guard.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, HTMX 2.x, Alpine.js 3.x, Jinja2, Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-03-29-manual-search-button-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/models/sourcing.py` | Modify | Add `last_searched_at` column to `Requirement` |
| `alembic/versions/[auto].py` | Create | Migration + backfill from parent Requisition |
| `app/search_service.py` | Modify | Stamp `last_searched_at` after successful search |
| `app/routers/sightings.py` | Modify | Rate guard + belt-and-suspenders stamp in refresh endpoints |
| `app/templates/htmx/partials/shared/_macros.html` | Modify | Add `search_button` macro |
| `app/templates/htmx/partials/sightings/detail.html` | Modify | Replace bare Refresh button with macro |
| `app/templates/htmx/partials/requisitions/tabs/parts.html` | Modify | Add checkbox header + "Search Selected" toolbar button |
| `app/templates/htmx/partials/requisitions/tabs/req_row.html` | Modify | Add checkbox cell, update colspan, add timestamp to sightings cell |
| `tests/test_sightings_router.py` | Modify | Add tests for rate guard + timestamp behavior |
| `tests/test_search_service.py` | Modify | Add test for `last_searched_at` stamping |
| `tests/test_manual_search.py` | Create | Integration tests for the full feature |

---

### Task 1: Add `last_searched_at` Column to Requirement Model

**Files:**
- Modify: `app/models/sourcing.py:122` (after `created_at`)
- Create: `alembic/versions/[auto].py` (migration)
- Modify: `tests/test_sightings_router.py:21-28` (update `_seed_data` fixture)

- [ ] **Step 1: Write the failing test**

Create `tests/test_manual_search.py`:

```python
"""Tests for manual search button feature.

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

from datetime import datetime, timezone

from app.models.sourcing import Requirement, Requisition


class TestRequirementLastSearchedAt:
    def test_requirement_has_last_searched_at_column(self, db_session):
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.flush()
        assert r.last_searched_at is None

    def test_last_searched_at_accepts_datetime(self, db_session):
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        now = datetime.now(timezone.utc)
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST-002",
            manufacturer="TestMfr",
            target_qty=50,
            sourcing_status="open",
            last_searched_at=now,
        )
        db_session.add(r)
        db_session.flush()
        assert r.last_searched_at == now
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_manual_search.py -v`
Expected: FAIL — `TypeError` because `last_searched_at` is not a valid column on `Requirement`

- [ ] **Step 3: Add the column to the model**

In `app/models/sourcing.py`, after line 122 (`created_at`), add:

```python
    last_searched_at = Column(DateTime)
```

The Requirement class columns should now read:

```python
    assigned_buyer_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_searched_at = Column(DateTime)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_manual_search.py -v`
Expected: PASS (both tests green)

- [ ] **Step 5: Generate Alembic migration**

Run: `cd /root/availai && alembic revision --autogenerate -m "add_requirement_last_searched_at"`

Review the generated migration file. It should contain:

```python
def upgrade():
    op.add_column('requirements', sa.Column('last_searched_at', sa.DateTime(), nullable=True))
```

Add the backfill after the `add_column` call:

```python
def upgrade():
    op.add_column('requirements', sa.Column('last_searched_at', sa.DateTime(), nullable=True))
    # Backfill from parent requisition's last_searched_at
    op.execute("""
        UPDATE requirements
        SET last_searched_at = (
            SELECT last_searched_at FROM requisitions
            WHERE requisitions.id = requirements.requisition_id
        )
        WHERE last_searched_at IS NULL
    """)


def downgrade():
    op.drop_column('requirements', 'last_searched_at')
```

- [ ] **Step 6: Apply migration**

Run: `cd /root/availai && alembic upgrade head`
Expected: Migration applies successfully

- [ ] **Step 7: Commit**

```bash
cd /root/availai
git add app/models/sourcing.py alembic/versions/ tests/test_manual_search.py
git commit -m "feat: add last_searched_at column to Requirement model

Tracks when each individual requirement was last searched.
Backfills from parent Requisition.last_searched_at."
```

---

### Task 2: Stamp `last_searched_at` in Search Service

**Files:**
- Modify: `app/search_service.py:239` (after `write_db.commit()`)
- Modify: `tests/test_manual_search.py` (add stamping test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_manual_search.py`:

```python
from unittest.mock import AsyncMock, patch


class TestSearchRequirementStamp:
    @patch("app.search_service._fire_connectors", new_callable=AsyncMock)
    async def test_search_requirement_stamps_last_searched_at(self, mock_fire, db_session):
        """search_requirement() should set requirement.last_searched_at after success."""
        mock_fire.return_value = ([], [])

        req = Requisition(name="Stamp Test", status="active", customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="STAMP-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()

        assert r.last_searched_at is None

        from app.search_service import search_requirement

        await search_requirement(r, db_session)

        db_session.refresh(r)
        assert r.last_searched_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_manual_search.py::TestSearchRequirementStamp -v`
Expected: FAIL — `last_searched_at` is still `None` after search

- [ ] **Step 3: Add the stamp to search_service.py**

In `app/search_service.py`, find the `search_requirement()` function. After the line `write_db.commit()` (approximately line 239), add:

```python
        write_req.last_searched_at = now
        write_db.commit()
```

The surrounding code should look like:

```python
        write_db.commit()

        # Stamp per-requirement search timestamp
        write_req.last_searched_at = now
        write_db.commit()
```

Note: `now` is already defined at the top of the function (approximately line 186) as `now = datetime.now(timezone.utc)`. `write_req` is already fetched (approximately line 210) as `write_req = write_db.get(Requirement, req_id)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_manual_search.py::TestSearchRequirementStamp -v`
Expected: PASS

- [ ] **Step 5: Run existing search service tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service.py -v`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
cd /root/availai
git add app/search_service.py tests/test_manual_search.py
git commit -m "feat: stamp Requirement.last_searched_at in search_requirement()

Single source of truth — all callers (manual refresh, batch, scheduler)
now get per-requirement search timestamps."
```

---

### Task 3: Add Server-Side Rate Guard to Sightings Refresh Endpoints

**Files:**
- Modify: `app/routers/sightings.py:527-612` (both refresh endpoints)
- Modify: `tests/test_manual_search.py` (add rate guard tests)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_manual_search.py`:

```python
from datetime import timedelta

from app.models.vendors import VendorCard
from app.models.vendor_sighting_summary import VendorSightingSummary


def _seed_requirement(db_session, mpn="RATE-001", last_searched_at=None):
    """Create a requisition + requirement for testing."""
    req = Requisition(name="Rate Test RFQ", status="active", customer_name="Acme")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        manufacturer="TestMfr",
        target_qty=100,
        sourcing_status="open",
        last_searched_at=last_searched_at,
    )
    db_session.add(r)
    # Add a vendor summary so detail panel renders
    vs = VendorSightingSummary(
        requirement_id=r.id,
        vendor_name="Test Vendor",
        estimated_qty=200,
        listing_count=1,
        score=50.0,
        tier="Good",
    )
    db_session.add(vs)
    db_session.commit()
    return req, r


class TestSingleRefreshRateGuard:
    def test_refresh_returns_toast_when_recently_searched(self, client, db_session):
        """Refresh within 5 minutes should return info toast, not re-search."""
        now = datetime.now(timezone.utc)
        _, r = _seed_requirement(db_session, last_searched_at=now)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200
        assert "Already searched" in resp.text or "already searched" in resp.text

    def test_refresh_proceeds_when_not_recently_searched(self, client, db_session):
        """Refresh after 5 minutes should proceed normally."""
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        _, r = _seed_requirement(db_session, last_searched_at=old)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200
        # Should NOT contain the rate-limit toast
        assert "Already searched" not in resp.text

    def test_refresh_proceeds_when_never_searched(self, client, db_session):
        """First-time search should always proceed."""
        _, r = _seed_requirement(db_session, last_searched_at=None)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200
        assert "Already searched" not in resp.text


class TestBatchRefreshRateGuard:
    def test_batch_skips_recently_searched(self, client, db_session):
        """Batch refresh should skip recently-searched requirements."""
        now = datetime.now(timezone.utc)
        _, r1 = _seed_requirement(db_session, mpn="BATCH-001", last_searched_at=now)
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        _, r2 = _seed_requirement(db_session, mpn="BATCH-002", last_searched_at=old)
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": f"[{r1.id}, {r2.id}]"},
        )
        assert resp.status_code == 200
        # Should indicate that some were skipped
        assert "skipped" in resp.text.lower() or "fresh" in resp.text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_manual_search.py::TestSingleRefreshRateGuard tests/test_manual_search.py::TestBatchRefreshRateGuard -v`
Expected: FAIL — rate guard not implemented yet

- [ ] **Step 3: Add rate guard to `sightings_refresh()`**

In `app/routers/sightings.py`, in the `sightings_refresh()` function (line 527), add the rate guard after the 404 check (line 540) and before the search call (line 542):

```python
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    # Rate guard: skip if searched within 5 minutes
    now = datetime.now(timezone.utc)
    if requirement.last_searched_at and (now - requirement.last_searched_at).total_seconds() < 300:
        response = await sightings_detail(request, requirement_id, db, user)
        response.headers["HX-Trigger"] = (
            '{"showToast": {"message": "Already searched within the last 5 minutes.", "type": "info"}}'
        )
        return response

    refresh_failed = False
```

Also add the belt-and-suspenders stamp after the search succeeds. After the `except` block (around line 548) and before the `broker.publish` (line 550), add:

```python
    if not refresh_failed:
        requirement.last_searched_at = now
        db.commit()
```

Add `datetime` import at the top of the file if not already present:

```python
from datetime import datetime, timezone
```

- [ ] **Step 4: Add rate guard to `sightings_batch_refresh()`**

In `app/routers/sightings.py`, in the `sightings_batch_refresh()` function (line 565), replace the search loop (lines 586-598) with:

```python
    success = 0
    failed = 0
    skipped = 0
    now = datetime.now(timezone.utc)
    for rid in requirement_ids:
        req_obj = reqs_by_id.get(int(rid))
        if not req_obj:
            failed += 1
            continue
        # Skip if searched within 5 minutes
        if req_obj.last_searched_at and (now - req_obj.last_searched_at).total_seconds() < 300:
            skipped += 1
            continue
        try:
            await search_requirement(req_obj, db)
            req_obj.last_searched_at = now
            success += 1
        except Exception:
            logger.warning("Batch refresh failed for requirement %s", rid, exc_info=True)
            failed += 1

    if success:
        db.commit()
```

Update the toast message (around line 608):

```python
    parts = []
    total = success + failed + skipped
    parts.append(f"Searched {success}/{total} requirements.")
    if skipped:
        parts.append(f"{skipped} skipped (already fresh).")
    if failed:
        parts.append(f"{failed} failed.")
    msg = " ".join(parts)
    level = "warning" if failed else ("info" if skipped and not success else "success")
    return _oob_toast(msg, level)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_manual_search.py -v`
Expected: All tests pass

- [ ] **Step 6: Run existing sightings tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py tests/test_sightings_router_comprehensive.py -v`
Expected: All existing tests still pass

- [ ] **Step 7: Commit**

```bash
cd /root/availai
git add app/routers/sightings.py tests/test_manual_search.py
git commit -m "feat: add 5-minute rate guard to sightings refresh endpoints

Prevents excessive API calls. Single refresh returns info toast.
Batch refresh skips fresh requirements and reports counts."
```

---

### Task 4: Add `search_button` Macro to `_macros.html`

**Files:**
- Modify: `app/templates/htmx/partials/shared/_macros.html` (append after line 170)

- [ ] **Step 1: Add the macro**

Append to `app/templates/htmx/partials/shared/_macros.html` after the `stat_card` macro (after line 170):

```jinja2


{# ── Search Button ───────────────────────────────────────────────── #}

{% macro search_button(requirement, target="#sightings-detail", swap="innerHTML") %}
{# Manual search trigger with timestamp and magnifier icon.
   requirement: Requirement object (reads .id and .last_searched_at)
   target: HTMX target selector (varies by page context)
   swap: HTMX swap strategy
#}
<div class="flex items-center gap-2">
  <button hx-post="/v2/partials/sightings/{{ requirement.id }}/refresh"
          hx-target="{{ target }}"
          hx-swap="{{ swap }}"
          hx-indicator="#search-spinner-{{ requirement.id }}"
          data-loading-disable
          class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-gray-200 text-xs font-medium text-gray-600 hover:bg-gray-50 transition-colors">
    <svg id="search-spinner-{{ requirement.id }}" class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/>
    </svg>
    <span class="htmx-indicator">
      <svg class="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
      </svg>
    </span>
    Search
  </button>
  {% if requirement.last_searched_at %}
  <span class="text-[10px] text-gray-400">{{ requirement.last_searched_at|timeago }}</span>
  {% else %}
  <span class="text-[10px] text-gray-400 italic">Never searched</span>
  {% endif %}
</div>
{%- endmacro %}
```

- [ ] **Step 2: Verify template renders without errors**

Run: `cd /root/availai && python -c "from app.template_env import templates; t = templates.get_template('htmx/partials/shared/_macros.html'); print('OK')"`
Expected: Prints `OK` without Jinja2 syntax errors

- [ ] **Step 3: Commit**

```bash
cd /root/availai
git add app/templates/htmx/partials/shared/_macros.html
git commit -m "feat: add search_button macro to shared _macros.html

Magnifier icon, data-loading-disable, timeago timestamp.
Parameterized hx-target and hx-swap for use across pages."
```

---

### Task 5: Replace Sightings Detail Refresh Button with Macro

**Files:**
- Modify: `app/templates/htmx/partials/sightings/detail.html:9,20-29`

- [ ] **Step 1: Update the import line**

In `app/templates/htmx/partials/sightings/detail.html`, line 9 currently reads:

```jinja2
{% import "htmx/partials/shared/_macros.html" as m %}
```

This already imports the macros file as `m`. The `search_button` macro will be available as `m.search_button`.

- [ ] **Step 2: Replace the bare Refresh button**

Replace lines 20-29 in `detail.html`:

```jinja2
    <button hx-post="/v2/partials/sightings/{{ requirement.id }}/refresh"
            hx-target="#sightings-detail"
            hx-swap="innerHTML"
            hx-indicator="#refresh-spinner"
            class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-gray-200 text-xs font-medium text-gray-600 hover:bg-gray-50 transition-colors">
      <svg id="refresh-spinner" class="h-3.5 w-3.5 htmx-indicator animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
      </svg>
      Refresh
    </button>
```

With:

```jinja2
    {{ m.search_button(requirement, target="#sightings-detail", swap="innerHTML") }}
```

- [ ] **Step 3: Run sightings page tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
cd /root/availai
git add app/templates/htmx/partials/sightings/detail.html
git commit -m "feat: replace bare sightings Refresh button with search_button macro

Adds magnifier icon, 'Searched X ago' timestamp, consistent styling."
```

---

### Task 6: Add Checkbox Selection + "Search Selected" to RFQ Parts Tab

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/parts.html:6,195,211`
- Modify: `app/templates/htmx/partials/requisitions/tabs/req_row.html:6,54-66,84`

- [ ] **Step 1: Wrap the parts tab outer div with Alpine selection state**

In `app/templates/htmx/partials/requisitions/tabs/parts.html`, line 6 currently reads:

```jinja2
<div>
```

Replace with:

```jinja2
<div x-data="{ selectedReqIds: [] }">
```

- [ ] **Step 2: Add "Search Selected" button to the toolbar**

In `parts.html`, after the "Search All Sources" button (after line 29, before `</div>`), add:

```jinja2
      <button x-show="selectedReqIds.length > 0"
              x-cloak
              hx-post="/v2/partials/sightings/batch-refresh"
              :hx-vals="JSON.stringify({requirement_ids: JSON.stringify(selectedReqIds)})"
              hx-swap="none"
              data-loading-disable
              class="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-brand-600 bg-brand-50 border border-brand-200 rounded-lg hover:bg-brand-100 transition-colors">
        <svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/>
        </svg>
        Search Selected (<span x-text="selectedReqIds.length"></span>)
      </button>
```

- [ ] **Step 3: Add checkbox column header**

In `parts.html`, add a checkbox `<th>` before the MPN header (before line 196):

```jinja2
          <th class="px-2 py-2.5 w-8">
            <input type="checkbox" class="rounded border-gray-300 text-brand-500 focus:ring-brand-500"
                   @change="selectedReqIds = $el.checked ? [{% for r in requirements %}{{ r.id }}{{ ',' if not loop.last }}{% endfor %}] : []">
          </th>
```

- [ ] **Step 4: Add checkbox cell and timestamp to req_row.html**

In `app/templates/htmx/partials/requisitions/tabs/req_row.html`, add a checkbox `<td>` after line 9 (after the `@dblclick` line), before the first display `<td>`:

```jinja2
  <td class="px-2 py-2.5" x-show="!editing" x-cloak @dblclick.stop>
    <input type="checkbox" class="rounded border-gray-300 text-brand-500 focus:ring-brand-500"
           :value="{{ r.id }}"
           @change="$el.checked ? selectedReqIds.push({{ r.id }}) : selectedReqIds = selectedReqIds.filter(id => id !== {{ r.id }})">
  </td>
```

- [ ] **Step 5: Add "Searched X ago" to the sightings cell**

In `req_row.html`, replace the sightings cell (lines 54-66) with:

```jinja2
  <td data-col-key="sightings" class="px-4 py-2.5 text-sm text-right" x-show="!editing" x-cloak>
    {% if r.sighting_count > 0 %}
      <span class="inline-flex items-center gap-1 text-brand-600 font-medium tabular-nums">
        {{ r.sighting_count }}
        <svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
          <path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
        </svg>
      </span>
    {% else %}
      <span class="text-gray-400 tabular-nums">0</span>
    {% endif %}
    {% if r.last_searched_at %}
    <div class="text-[10px] text-gray-400">{{ r.last_searched_at|timeago }}</div>
    {% endif %}
  </td>
```

- [ ] **Step 6: Update colspan**

In `req_row.html`, line 84, change:

```jinja2
  <td colspan="16" x-show="editing" x-cloak>
```

To:

```jinja2
  <td colspan="17" x-show="editing" x-cloak>
```

- [ ] **Step 7: Verify template rendering**

Run: `cd /root/availai && python -c "from app.template_env import templates; t = templates.get_template('htmx/partials/requisitions/tabs/parts.html'); print('OK')"`
Expected: Prints `OK`

Run: `cd /root/availai && python -c "from app.template_env import templates; t = templates.get_template('htmx/partials/requisitions/tabs/req_row.html'); print('OK')"`
Expected: Prints `OK`

- [ ] **Step 8: Run existing requisition tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers.py -k requisition -v`
Expected: All existing tests pass

- [ ] **Step 9: Commit**

```bash
cd /root/availai
git add app/templates/htmx/partials/requisitions/tabs/parts.html app/templates/htmx/partials/requisitions/tabs/req_row.html
git commit -m "feat: add checkbox selection + 'Search Selected' to RFQ parts tab

Batch search posts to existing sightings/batch-refresh endpoint.
Sightings cell now shows 'Searched X ago' timestamp per requirement."
```

---

### Task 7: Full Test Suite + Lint + Final Verification

**Files:**
- All modified files from Tasks 1-6

- [ ] **Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests pass, including new tests from `test_manual_search.py`

- [ ] **Step 2: Run linter**

Run: `cd /root/availai && ruff check app/`
Expected: No lint errors

- [ ] **Step 3: Run type checker**

Run: `cd /root/availai && mypy app/models/sourcing.py app/routers/sightings.py app/search_service.py`
Expected: No type errors

- [ ] **Step 4: Run ruff format**

Run: `cd /root/availai && ruff format app/models/sourcing.py app/routers/sightings.py app/search_service.py`
Expected: Files already formatted or auto-fixed

- [ ] **Step 5: Final commit if any formatting changes**

```bash
cd /root/availai
git add -u
git diff --cached --stat  # Only commit if there are changes
git commit -m "style: format manual search button code"
```
