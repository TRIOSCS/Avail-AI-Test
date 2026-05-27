# Sightings Redesign Plan B: Visual Triage + Intelligence

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat sightings table with a priority-driven triage interface (dashboard strip, coverage bars, heatmap rows) and surface hidden vendor/material intelligence in the detail panel without adding queries.

**Architecture:** Phase 2 adds 4 aggregate subqueries to `sightings_list()` for the dashboard strip and coverage map, replaces the Top Vendor column with a coverage bar, and adds two-state heatmap coloring computed from existing data. Phase 3 extracts detail.html into 4 sub-partials and piggybacks vendor intelligence (response_rate, ghost_rate, explain_lead) onto the existing VendorCard phone lookup query. All new template context vars are computed server-side and passed to Jinja2 — zero Alpine.js state additions beyond what Plan A already registered.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Jinja2, Alpine.js, Tailwind CSS, HTMX

**Spec:** `docs/superpowers/specs/2026-03-25-sightings-page-redesign.md`
**Depends on:** Plan A (Foundation + Performance) must be completed first. Specifically, Plan A Task 5 must have registered `$store.sightingSelection` in `htmx_app.js` and Plan A Task 3 must have run the Alembic migrations adding `newest_sighting_at`, `best_lead_time_days`, `min_moq`, `has_contact_info`, and `vendor_card_id` to `VendorSightingSummary`.

**Note on task atomicity:** Some backend tasks (Tasks 1, 3, 7) add context vars that are not rendered until later template tasks (Tasks 2, 5, 8). Tests for rendered HTML should be deferred to the template task commits. Backend task tests should verify context dict computation only (not HTML output).

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/routers/sightings.py` | Modify | Dashboard counters, coverage map, heatmap set, OOO query, vendor intelligence, suggested action, MaterialCard fetch |
| `app/templates/htmx/partials/sightings/table.html` | Modify | Dashboard strip, coverage bar column, heatmap rows, stale indicator, final 8-column layout |
| `app/templates/htmx/partials/sightings/detail.html` | Modify | Decompose into includes, constraints section, enrichment bar, suggested action, better empty states |
| `app/templates/htmx/partials/sightings/_constraints.html` | Create | Requirement constraints collapsible section |
| `app/templates/htmx/partials/sightings/_vendor_row.html` | Create | Single vendor row with expandable intelligence |
| `app/templates/htmx/partials/sightings/_suggested_action.html` | Create | State-machine-driven next action prompt |
| `app/templates/htmx/partials/sightings/_enrichment_bar.html` | Create | MaterialCard lifecycle/RoHS/category bar |
| `tests/test_sightings_router.py` | Modify | Tests for dashboard counters, coverage map, heatmap, vendor intelligence, suggested action, OOO, empty states |

---

### Task 1: Dashboard Strip — Backend Counters

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write failing tests for dashboard counters**

Add to `tests/test_sightings_router.py`:

```python
from datetime import datetime, timedelta, timezone

from app.models.intelligence import ActivityLog
from app.models.offers import Offer


class TestDashboardCounters:
    """Phase 2: Smart Priority Dashboard Strip counters in sightings_list context."""

    def test_urgent_count_high_priority(self, client, db_session):
        """Requirements with priority_score >= 70 counted as urgent."""
        req, r, _ = _seed_data(db_session)
        r.priority_score = 85.0
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "1 Urgent" in resp.text or "1 urgent" in resp.text.lower()

    def test_urgent_count_near_deadline(self, client, db_session):
        """Requirements with need_by_date within 48h counted as urgent."""
        from datetime import date

        req, r, _ = _seed_data(db_session)
        r.need_by_date = date.today() + timedelta(days=1)
        r.priority_score = 20.0  # Low priority but near deadline
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "urgent" in resp.text.lower()

    def test_stale_count(self, client, db_session):
        """Requirements with no recent activity counted as stale."""
        _seed_data(db_session)
        # No activity logs = stale
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "stale" in resp.text.lower()

    def test_pending_count(self, client, db_session):
        """Requirements with pending offers counted."""
        req, r, _ = _seed_data(db_session)
        offer = Offer(
            requirement_id=r.id,
            requisition_id=req.id,
            vendor_name="Good Vendor",
            status="pending_review",
            unit_price=1.50,
            qty_available=100,
        )
        db_session.add(offer)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "pending" in resp.text.lower()

    def test_unassigned_count(self, client, db_session):
        """Requirements with no assigned buyer counted."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "unassigned" in resp.text.lower()

    def test_counters_present_in_response(self, client, db_session):
        """All 4 dashboard counters appear in the HTML."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        text = resp.text.lower()
        for label in ["urgent", "stale", "pending", "unassigned"]:
            assert label in text, f"Dashboard counter '{label}' missing from response"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestDashboardCounters -v
```

Expected: FAIL — current template has no dashboard counter labels.

- [ ] **Step 3: Add dashboard counter queries to `sightings_list()`**

In `app/routers/sightings.py`, inside `sightings_list()`, after the existing `stat_counts` query (line ~111), add:

```python
    # ── Dashboard Strip Counters (Phase 2) ──────────────────────────
    from datetime import date

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    deadline_48h = date.today() + timedelta(days=2)

    # Active requirement IDs for dashboard (not just current page)
    active_req_subq = (
        db.query(Requirement.id)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status == RequisitionStatus.ACTIVE)
        .subquery()
    )

    # Urgent: priority >= 70 OR need_by_date within 48h
    urgent_count = (
        db.query(sqlfunc.count(Requirement.id))
        .filter(
            Requirement.id.in_(active_req_subq),
            (Requirement.priority_score >= 70) | (Requirement.need_by_date <= deadline_48h),
        )
        .scalar()
    ) or 0

    # Stale: no ActivityLog within sighting_stale_days
    stale_threshold = now_utc - timedelta(days=settings.sighting_stale_days)
    stale_subq = (
        db.query(ActivityLog.requirement_id)
        .filter(ActivityLog.requirement_id.isnot(None))
        .group_by(ActivityLog.requirement_id)
        .having(sqlfunc.max(ActivityLog.created_at) >= stale_threshold)
        .subquery()
    )
    stale_count = (
        db.query(sqlfunc.count(Requirement.id))
        .filter(
            Requirement.id.in_(active_req_subq),
            ~Requirement.id.in_(stale_subq),
        )
        .scalar()
    ) or 0

    # Pending: has at least one offer with status pending_review
    pending_count = (
        db.query(sqlfunc.count(sqlfunc.distinct(Offer.requirement_id)))
        .filter(
            Offer.requirement_id.in_(active_req_subq),
            Offer.status == OfferStatus.PENDING_REVIEW,
        )
        .scalar()
    ) or 0

    # Unassigned: assigned_buyer_id IS NULL
    unassigned_count = (
        db.query(sqlfunc.count(Requirement.id))
        .filter(
            Requirement.id.in_(active_req_subq),
            Requirement.assigned_buyer_id.is_(None),
        )
        .scalar()
    ) or 0

    dashboard_counters = {
        "urgent": urgent_count,
        "stale": stale_count,
        "pending": pending_count,
        "unassigned": unassigned_count,
    }
```

Add `dashboard_counters` to the template context dict:

```python
    ctx = {
        # ... existing keys ...
        "dashboard_counters": dashboard_counters,
    }
```

Add `Offer` import at the top of the file if not already present (it is imported on line 28 via `from ..models.offers import Offer`).

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestDashboardCounters -v
```

Note: Tests will still fail until Task 2 adds the template rendering. Proceed to Task 2 then re-run.

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add dashboard strip counter queries to sightings_list"
```

---

### Task 2: Dashboard Strip — Template

**Files:** `app/templates/htmx/partials/sightings/table.html`

- [ ] **Step 1: Replace stat pills with dashboard strip**

In `app/templates/htmx/partials/sightings/table.html`, replace the entire stat pills block (lines 10-29, from `{# -- Stat Pills --` through the closing `</div>`) with:

```html
{# ── Smart Priority Dashboard Strip ────────────────────────── #}
{% set dc = dashboard_counters|default({}) %}
<div class="px-3 pt-3 pb-2 border-b border-gray-100 flex items-center gap-2 flex-wrap">
  {# Urgent counter #}
  <button hx-get="/v2/partials/sightings?priority_min=70&q={{ q }}&assigned={{ assigned }}"
          hx-target="#sightings-table" hx-swap="innerHTML"
          class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium transition-colors
                 {{ 'bg-rose-100 text-rose-700' if dc.get('urgent', 0) > 0 else 'bg-gray-100 text-gray-500' }}">
    <span class="inline-block w-2 h-2 rounded-full {{ 'bg-rose-500' if dc.get('urgent', 0) > 0 else 'bg-gray-300' }}"></span>
    {{ dc.get('urgent', 0) }} Urgent
  </button>

  {# Stale counter #}
  <button hx-get="/v2/partials/sightings?stale=1&q={{ q }}&assigned={{ assigned }}"
          hx-target="#sightings-table" hx-swap="innerHTML"
          class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium transition-colors
                 {{ 'bg-amber-100 text-amber-700' if dc.get('stale', 0) > 0 else 'bg-gray-100 text-gray-500' }}">
    <span class="inline-block w-2 h-2 rounded-full {{ 'bg-amber-500' if dc.get('stale', 0) > 0 else 'bg-gray-300' }}"></span>
    {{ dc.get('stale', 0) }} Stale
  </button>

  {# Pending counter #}
  <button hx-get="/v2/partials/sightings?status=offered&q={{ q }}&assigned={{ assigned }}"
          hx-target="#sightings-table" hx-swap="innerHTML"
          class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium transition-colors
                 {{ 'bg-blue-100 text-blue-700' if dc.get('pending', 0) > 0 else 'bg-gray-100 text-gray-500' }}">
    <span class="inline-block w-2 h-2 rounded-full {{ 'bg-blue-500' if dc.get('pending', 0) > 0 else 'bg-gray-300' }}"></span>
    {{ dc.get('pending', 0) }} Pending
  </button>

  {# Unassigned counter #}
  <button hx-get="/v2/partials/sightings?assigned=none&q={{ q }}"
          hx-target="#sightings-table" hx-swap="innerHTML"
          class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium transition-colors
                 {{ 'bg-gray-200 text-gray-700' if dc.get('unassigned', 0) > 0 else 'bg-gray-100 text-gray-500' }}">
    <span class="inline-block w-2 h-2 rounded-full {{ 'bg-gray-400' if dc.get('unassigned', 0) > 0 else 'bg-gray-300' }}"></span>
    {{ dc.get('unassigned', 0) }} Unassigned
  </button>

  {# Status filter pills (moved to right) #}
  <div class="ml-auto flex items-center gap-1">
    {% set pills = [
      ('', 'All', total),
      ('open', 'New', stat_counts.get('open', 0)),
      ('sourcing', 'Sourcing', stat_counts.get('sourcing', 0)),
      ('offered', 'Offered', stat_counts.get('offered', 0)),
      ('quoted', 'Quoted', stat_counts.get('quoted', 0) + stat_counts.get('won', 0)),
    ] %}
    {% for val, label, count in pills %}
    <button hx-get="/v2/partials/sightings?status={{ val }}&q={{ q }}&group_by={{ group_by }}&assigned={{ assigned }}"
            hx-target="#sightings-table" hx-swap="innerHTML"
            class="px-2 py-0.5 rounded text-[10px] font-medium transition-colors
                   {{ 'bg-brand-100 text-brand-700' if status == val else 'text-gray-400 hover:text-gray-600' }}">
      {{ label }} <span class="text-[9px]">{{ count }}</span>
    </button>
    {% endfor %}
  </div>
</div>
```

- [ ] **Step 2: Run dashboard counter tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestDashboardCounters -v
```

Expected: All 6 PASS.

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/table.html
git commit -m "feat(sightings): replace stat pills with smart priority dashboard strip"
```

---

### Task 3: Fulfillment Coverage Bar — Backend

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write failing tests for coverage map**

Add to `tests/test_sightings_router.py`:

```python
class TestCoverageMap:
    """Phase 2: Fulfillment coverage bar data in sightings_list context."""

    def test_coverage_map_in_context(self, client, db_session):
        """Coverage map contains total estimated qty per requirement."""
        req, r, s = _seed_data(db_session)
        # s has estimated_qty=200, r has target_qty=100 => 200% coverage
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        # Coverage bar should show (rendered as percentage)
        assert "coverage" in resp.text.lower() or "200" in resp.text

    def test_coverage_zero_when_no_sightings(self, client, db_session):
        """Requirements with no sightings show 0% coverage."""
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="EMPTY-MPN",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestCoverageMap -v
```

- [ ] **Step 3: Add coverage query to `sightings_list()`**

In `app/routers/sightings.py`, inside `sightings_list()`, after the top vendors query block (line ~134), add:

```python
        # Fulfillment coverage per requirement (Phase 2)
        coverage_rows = (
            db.query(
                VendorSightingSummary.requirement_id,
                sqlfunc.sum(VendorSightingSummary.estimated_qty).label("total_qty"),
            )
            .filter(VendorSightingSummary.requirement_id.in_(req_ids))
            .group_by(VendorSightingSummary.requirement_id)
            .all()
        )
        coverage_map = {c.requirement_id: c.total_qty or 0 for c in coverage_rows}
```

Add `coverage_map` to the context dict:

```python
        "coverage_map": coverage_map,
```

Also add a default outside the `if requirements:` block:

```python
    coverage_map = {}
```

Place this on the line after `top_vendors = {}` (around line 113).

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestCoverageMap -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add fulfillment coverage map query to sightings_list"
```

---

### Task 4: Heatmap Rows — Backend

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write failing test for heatmap set**

Add to `tests/test_sightings_router.py`:

```python
class TestHeatmapRows:
    """Phase 2: Two-state heatmap row identification."""

    def test_high_priority_in_heatmap(self, client, db_session):
        """Requirements with priority >= 70 flagged for rose tint."""
        req, r, _ = _seed_data(db_session)
        r.priority_score = 85.0
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "bg-rose-50/30" in resp.text

    def test_normal_row_no_heatmap(self, client, db_session):
        """Low priority, non-stale requirements have no rose tint."""
        req, r, _ = _seed_data(db_session)
        r.priority_score = 20.0
        db_session.commit()
        # Add recent activity so not stale
        from app.models.intelligence import ActivityLog
        log = ActivityLog(
            activity_type="note", channel="system",
            requirement_id=r.id, requisition_id=req.id,
            notes="recent",
        )
        db_session.add(log)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert "bg-rose-50/30" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestHeatmapRows -v
```

- [ ] **Step 3: Compute heatmap set in `sightings_list()`**

In `app/routers/sightings.py`, inside `sightings_list()`, after the dashboard counters block, add:

```python
    # ── Heatmap Row Set (Phase 2) ─────────────────────────────────
    # Rose tint for: near deadline (48h), high-priority stale, critical/hot urgency
    heatmap_req_ids: set[int] = set()
    if requirements:
        for r in requirements:
            # Near deadline
            if r.need_by_date and r.need_by_date <= deadline_48h:
                heatmap_req_ids.add(r.id)
                continue
            # Stale AND medium+ priority
            if r.id in stale_req_ids and (r.priority_score or 0) >= 40:
                heatmap_req_ids.add(r.id)
                continue
            # Critical/hot urgency (from requisition)
            urgency = getattr(r.requisition, "urgency", None) or ""
            if urgency in ("critical", "hot"):
                heatmap_req_ids.add(r.id)
```

Add `heatmap_req_ids` to the context dict:

```python
        "heatmap_req_ids": heatmap_req_ids,
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestHeatmapRows -v
```

Note: Tests will pass only after Task 5 updates the template. Proceed to Task 5.

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): compute heatmap row set for two-state triage"
```

---

### Task 5: Table Template — Coverage Bar, Heatmap, Stale Indicator, Final 8 Columns

**Files:** `app/templates/htmx/partials/sightings/table.html`

- [ ] **Step 1: Replace table headers with final 8 columns**

Replace the `<thead>` block (the `<tr>` inside `<thead>`) with:

```html
      <tr>
        <th class="px-2 py-2 w-8">
          <input aria-label="Select all requirements" type="checkbox" x-model="toggleAll"
                 @change="document.querySelectorAll('.req-checkbox').forEach(cb => { cb.checked = toggleAll; if (toggleAll) $store.sightingSelection.toggle(parseInt(cb.value)); else $store.sightingSelection.clear(); })"
                 class="rounded border-gray-300">
        </th>
        <th class="px-3 py-2 text-left">MPN</th>
        <th class="px-3 py-2 text-right w-[60px]">Qty</th>
        <th class="px-3 py-2 text-left">Customer</th>
        <th class="px-3 py-2 text-left w-[80px]">Sales</th>
        <th class="px-3 py-2 text-left w-[80px]">Coverage</th>
        <th class="px-3 py-2 text-left w-[80px]">Status</th>
        <th class="px-3 py-2 text-center w-[60px]">Priority</th>
      </tr>
```

- [ ] **Step 2: Replace `render_row` macro with heatmap + coverage bar**

Replace the entire `render_row` macro (from `{% macro render_row(r) %}` through `{% endmacro %}`) with:

```html
      {% macro render_row(r) %}
      {% set is_hot = r.id in heatmap_req_ids %}
      {% set is_stale = r.id in stale_req_ids %}
      {% set cov_qty = coverage_map.get(r.id, 0) %}
      {% set cov_pct = ((cov_qty / r.target_qty * 100) if r.target_qty and r.target_qty > 0 else 0)|int %}
      {% set cov_pct = [cov_pct, 100]|min %}
      <tr class="group cursor-pointer {{ 'bg-rose-50/30' if is_hot else '' }}"
          :class="selectedReqId == {{ r.id }} ? 'row-selected' : ''"
          @click="selectReq({{ r.id }}); htmx.ajax('GET', '/v2/partials/sightings/{{ r.id }}/detail', {target: '#sightings-detail', swap: 'innerHTML'})"
          data-req-id="{{ r.id }}">
        <td class="px-2 py-2" @click.stop>
          <input aria-label="Select requirement {{ r.primary_mpn }}" type="checkbox" class="req-checkbox rounded border-gray-300" value="{{ r.id }}"
                 :checked="$store.sightingSelection.has({{ r.id }})"
                 @change="$store.sightingSelection.toggle({{ r.id }})">
        </td>
        <td class="px-3 py-2 font-mono font-medium text-gray-900">{{ r.primary_mpn }}</td>
        <td class="px-3 py-2 text-right font-mono">{{ r.target_qty or '—' }}</td>
        <td class="px-3 py-2 text-gray-600 truncate max-w-[140px]">
          {{ r.requisition.customer_name or '—' }}
        </td>
        <td class="px-3 py-2 text-gray-500 text-xs truncate">
          {{ r.requisition.creator.name if r.requisition.creator else '—' }}
        </td>
        <td class="px-3 py-2">
          <div class="relative flex items-center gap-1">
            <div class="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden" title="{{ cov_pct }}% coverage ({{ cov_qty }} / {{ r.target_qty or '?' }})">
              <div class="h-full rounded-full {{ 'bg-red-300' if cov_pct < 50 else 'bg-gray-400' }}"
                   style="width: {{ cov_pct }}%"></div>
            </div>
            <span class="text-[10px] text-gray-400 w-7 text-right">{{ cov_pct }}%</span>
            {% if is_stale %}
            <button class="ml-0.5 text-gray-300 hover:text-amber-500 transition-colors"
                    title="Stale — click to refresh"
                    hx-post="/v2/partials/sightings/{{ r.id }}/refresh"
                    hx-target="#sightings-detail"
                    hx-swap="innerHTML"
                    @click.stop>
              <svg class="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
              </svg>
            </button>
            {% endif %}
          </div>
        </td>
        <td class="px-3 py-2">
          {{ m.status_badge(r.sourcing_status) }}
        </td>
        <td class="px-3 py-2 text-center">
          {% if r.priority_score %}
            {% set pri = r.priority_score %}
            {% set pri_label = 'High' if pri >= 70 else ('Med' if pri >= 40 else 'Low') %}
            <span class="inline-flex px-1.5 py-0.5 text-[10px] font-semibold rounded-full {{ 'bg-rose-50 text-rose-700' if pri >= 70 else ('bg-amber-50 text-amber-700' if pri >= 40 else 'bg-gray-100 text-gray-500') }}">{{ pri_label }}</span>
          {% endif %}
        </td>
      </tr>
      {% endmacro %}
```

- [ ] **Step 3: Update group header colspan**

Change `colspan="9"` to `colspan="8"` in the group header row.

- [ ] **Step 4: Run all table-related tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsListPartial tests/test_sightings_router.py::TestDashboardCounters tests/test_sightings_router.py::TestCoverageMap tests/test_sightings_router.py::TestHeatmapRows -v
```

Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/sightings/table.html
git commit -m "feat(sightings): final 8-column layout with coverage bar, heatmap rows, stale indicator"
```

---

### Task 6: Requirement Constraints Sub-Partial

**Files:** `app/templates/htmx/partials/sightings/_constraints.html`

- [ ] **Step 1: Create constraints sub-partial**

Create `app/templates/htmx/partials/sightings/_constraints.html`:

```html
{# _constraints.html — Requirement constraints collapsible section.
   Called by: detail.html (include)
   Depends on: _macros.html (urgency_badge)
   Context: requirement, requisition
#}
{% import "htmx/partials/shared/_macros.html" as m %}

{% set has_constraints = requirement.need_by_date or requirement.condition or requirement.date_codes
    or requirement.firmware or requirement.packaging or requirement.sale_notes or requirement.substitutes %}
{% set req_urgency = requisition.urgency|default('normal') %}
{% set has_urgency = req_urgency in ('critical', 'hot') %}

{% if has_constraints or has_urgency %}
<div class="border-b border-gray-100 pb-3 mb-3"
     x-data="{ open: {{ 'true' if has_constraints or has_urgency else 'false' }} }">
  <button @click="open = !open"
          class="flex items-center justify-between w-full text-left">
    <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider">
      Constraints
    </h4>
    <svg :class="open ? 'rotate-180' : ''" class="h-3.5 w-3.5 text-gray-400 transition-transform" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/>
    </svg>
  </button>

  <div x-show="open" x-collapse class="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
    {% if requirement.need_by_date %}
    <div>
      <span class="text-gray-400">Need By:</span>
      {% set days_until = (requirement.need_by_date - today).days if today else none %}
      <span class="font-medium {{ 'text-rose-600' if days_until is not none and days_until <= 2 else ('text-amber-600' if days_until is not none and days_until <= 7 else 'text-gray-700') }}">
        {{ requirement.need_by_date.strftime('%Y-%m-%d') }}
        {% if days_until is not none and days_until <= 7 %}
          <span class="text-[10px]">({{ days_until }}d)</span>
        {% endif %}
      </span>
    </div>
    {% endif %}

    {% if has_urgency %}
    <div>
      <span class="text-gray-400">Urgency:</span>
      {{ m.urgency_badge(req_urgency) }}
    </div>
    {% endif %}

    {% if requirement.condition %}
    <div>
      <span class="text-gray-400">Condition:</span>
      <span class="text-gray-700">{{ requirement.condition }}</span>
    </div>
    {% endif %}

    {% if requirement.date_codes %}
    <div>
      <span class="text-gray-400">Date Codes:</span>
      <span class="text-gray-700 font-mono">{{ requirement.date_codes }}</span>
    </div>
    {% endif %}

    {% if requirement.firmware %}
    <div>
      <span class="text-gray-400">Firmware:</span>
      <span class="text-gray-700 font-mono">{{ requirement.firmware }}</span>
    </div>
    {% endif %}

    {% if requirement.packaging %}
    <div>
      <span class="text-gray-400">Packaging:</span>
      <span class="text-gray-700">{{ requirement.packaging }}</span>
    </div>
    {% endif %}

    {% if requirement.sale_notes %}
    <div class="col-span-2">
      <span class="text-gray-400">Sale Notes:</span>
      <span class="text-gray-700">{{ requirement.sale_notes }}</span>
    </div>
    {% endif %}

    {% if requirement.substitutes %}
    <div class="col-span-2">
      <span class="text-gray-400">Substitutes:</span>
      <span class="text-gray-700 font-mono">
        {% if requirement.substitutes is iterable and requirement.substitutes is not string %}
          {{ requirement.substitutes|join(', ') }}
        {% else %}
          {{ requirement.substitutes }}
        {% endif %}
      </span>
    </div>
    {% endif %}
  </div>
</div>
{% endif %}
```

- [ ] **Step 2: Write test for constraints rendering**

Add to `tests/test_sightings_router.py`:

```python
class TestConstraintsSection:
    """Phase 3: Requirement constraints section in detail panel."""

    def test_constraints_shown_when_present(self, client, db_session):
        req, r, _ = _seed_data(db_session)
        r.condition = "New Original"
        r.date_codes = "2024+"
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert "Constraints" in resp.text
        assert "New Original" in resp.text
        assert "2024+" in resp.text

    def test_constraints_hidden_when_empty(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        # No constraints set in _seed_data, section should not render
        assert "Constraints" not in resp.text
```

- [ ] **Step 3: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestConstraintsSection -v
```

Note: Will fail until Task 10 includes it in detail.html. Tracked there.

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/sightings/_constraints.html tests/test_sightings_router.py
git commit -m "feat(sightings): create constraints sub-partial for detail panel"
```

---

### Task 7: Vendor Intelligence — Backend

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write failing tests for vendor intelligence context**

Add to `tests/test_sightings_router.py`:

```python
from app.models.vendors import VendorCard, VendorContact


class TestVendorIntelligence:
    """Phase 3: Vendor intelligence data in detail panel context."""

    def test_vendor_card_data_in_response(self, client, db_session):
        """VendorCard intelligence fields appear in detail panel."""
        req, r, s = _seed_data(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
            response_rate=0.85,
            ghost_rate=0.05,
            vendor_score=72.0,
            engagement_score=65.0,
        )
        db_session.add(vc)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "85%" in resp.text or "response" in resp.text.lower()

    def test_explain_lead_in_response(self, client, db_session):
        """explain_lead() output rendered for each vendor."""
        req, r, s = _seed_data(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
            vendor_score=72.0,
        )
        db_session.add(vc)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        # explain_lead returns a string with vendor name
        assert "Good Vendor" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestVendorIntelligence -v
```

- [ ] **Step 3: Upgrade VendorCard query in `sightings_detail()`**

In `app/routers/sightings.py`, in the `sightings_detail()` function, replace the vendor phone lookup block (lines 212-224) with an expanded query that also fetches intelligence fields:

```python
    # ── Vendor Intelligence (Phase 3) ─────────────────────────────
    from ..scoring import explain_lead
    from ..vendor_utils import normalize_vendor_name

    normalized_names = [normalize_vendor_name(s.vendor_name) for s in summaries]

    # Single batch query for VendorCards — piggybacks phone + intelligence
    cards = (
        db.query(VendorCard)
        .filter(VendorCard.normalized_name.in_(normalized_names))
        .all()
    ) if normalized_names else []
    card_map = {c.normalized_name: c for c in cards}

    # Build vendor_phones (backward compat) + vendor_intel map
    vendor_phones = {s.vendor_name: s.vendor_phone for s in summaries if s.vendor_phone}
    vendor_intel: dict[str, dict] = {}

    for s in summaries:
        norm = normalize_vendor_name(s.vendor_name)
        card = card_map.get(norm)

        # Phone fallback from card
        if s.vendor_name not in vendor_phones and card and card.phones:
            phone = card.phones[0] if isinstance(card.phones, list) else card.phones
            if phone:
                vendor_phones[s.vendor_name] = phone

        # Intelligence fields
        age_days = None
        if hasattr(s, 'newest_sighting_at') and s.newest_sighting_at:
            from datetime import datetime, timezone
            age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - s.newest_sighting_at).days

        lead_explanation = explain_lead(
            vendor_name=s.vendor_name,
            is_authorized=False,
            vendor_score=card.vendor_score if card else None,
            unit_price=s.best_price,
            median_price=None,
            qty_available=s.estimated_qty,
            target_qty=requirement.target_qty,
            has_contact=getattr(s, 'has_contact_info', False) or bool(vendor_phones.get(s.vendor_name)),
            evidence_tier=s.tier,
            source_type=(s.source_types[0] if s.source_types and isinstance(s.source_types, list) else None),
            age_days=age_days,
        )

        vendor_intel[s.vendor_name] = {
            "response_rate": card.response_rate if card else None,
            "ghost_rate": card.ghost_rate if card else None,
            "vendor_score": card.vendor_score if card else None,
            "engagement_score": card.engagement_score if card else None,
            "avg_response_hours": card.avg_response_hours if card else None,
            "explain_lead": lead_explanation,
            "listing_count": s.listing_count,
            "source_types": s.source_types or [],
            "tier": s.tier,
            "best_lead_time_days": getattr(s, 'best_lead_time_days', None),
            "min_moq": getattr(s, 'min_moq', None),
            "newest_sighting_at": getattr(s, 'newest_sighting_at', None),
            "age_days": age_days,
        }
```

Add `vendor_intel` to the context dict:

```python
        "vendor_intel": vendor_intel,
```

Remove the old `vendor_names_needing_phone` block that was replaced.

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestVendorIntelligence -v
```

Note: Full pass requires the template updates in Tasks 8-10.

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add vendor intelligence and explain_lead to detail context"
```

---

### Task 8: Vendor Row Sub-Partial

**Files:** `app/templates/htmx/partials/sightings/_vendor_row.html`

- [ ] **Step 1: Create vendor row sub-partial with expandable intelligence**

Create `app/templates/htmx/partials/sightings/_vendor_row.html`:

```html
{# _vendor_row.html — Single vendor row with expandable intelligence.
   Called by: detail.html (loop include)
   Depends on: source_badge.html, _macros.html
   Context: s (VendorSightingSummary), vs (vendor status string),
            intel (vendor_intel dict for this vendor),
            vendor_phone (str or None), requirement,
            ooo_vendor (OOO contact or None),
            stale_days (int, from config)
#}

{% set intel = vendor_intel.get(s.vendor_name, {}) %}
{% set vendor_phone = vendor_phones.get(s.vendor_name) %}
{% set ooo = ooo_map.get(s.vendor_name|lower|trim) if ooo_map is defined else none %}

<tr class="group border-b border-gray-50 last:border-0"
    x-data="{ expanded: false }">
  <td colspan="7" class="p-0">
    {# ── Always-visible row ─────────────────────────────── #}
    <div class="flex items-center px-2 py-1.5 cursor-pointer hover:bg-gray-50/50 transition-colors"
         @click="expanded = !expanded">
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2">
          <span class="font-medium text-gray-900 text-sm">{{ s.vendor_name }}</span>
          {% set vs_styles = {
            'sighting': 'bg-gray-100 text-gray-600',
            'contacted': 'bg-blue-50 text-blue-700',
            'offer-in': 'bg-emerald-50 text-emerald-700',
            'unavailable': 'bg-gray-100 text-gray-500',
            'blacklisted': 'bg-red-50 text-red-700',
          } %}
          <span class="inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded-full {{ vs_styles.get(vs, 'bg-gray-100 text-gray-600') }}">
            {{ vs|replace('-', ' ')|capitalize }}
          </span>
          {% if ooo %}
          <span class="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium rounded-full bg-amber-50 text-amber-700">
            OOO{% if ooo.ooo_return_date %} until {{ ooo.ooo_return_date.strftime('%m/%d') }}{% endif %}
          </span>
          {% endif %}
        </div>
        {# Inline metrics: response rate + best price with freshness #}
        <div class="flex items-center gap-3 mt-0.5 text-[11px] text-gray-400">
          {% if intel.get('response_rate') is not none %}
          <span>{{ (intel.response_rate * 100)|int }}% response</span>
          {% endif %}
          {% if s.best_price %}
          <span class="font-mono">${{ '%.2f'|format(s.best_price) }}
            {% if intel.get('age_days') is not none %}
            <span class="text-{{ 'amber-500' if intel.age_days > stale_days|default(3) else 'gray-300' }}">— {{ intel.age_days }}d ago</span>
            {% endif %}
          </span>
          {% endif %}
        </div>
      </div>

      <div class="flex items-center gap-3 shrink-0">
        <span class="font-mono text-xs text-gray-600">{{ s.estimated_qty or '—' }} pcs</span>
        {% set score_color = 'text-emerald-600' if s.score >= 70 else ('text-amber-600' if s.score >= 40 else 'text-gray-500') %}
        <span class="font-mono text-xs {{ score_color }}">{{ s.score|round|int }}%</span>
        <svg :class="expanded ? 'rotate-180' : ''" class="h-3.5 w-3.5 text-gray-300 transition-transform" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/>
        </svg>
      </div>
    </div>

    {# ── Expandable detail ──────────────────────────────── #}
    <div x-show="expanded" x-collapse class="px-3 pb-2 bg-gray-50/50 border-t border-gray-100">
      <div class="grid grid-cols-2 gap-x-4 gap-y-1.5 py-2 text-xs">
        {% if intel.get('listing_count') %}
        <div>
          <span class="text-gray-400">Sources:</span>
          <span class="text-gray-700">Found on {{ intel.listing_count }} source{{ 's' if intel.listing_count != 1 }}</span>
        </div>
        {% endif %}

        {% if intel.get('source_types') %}
        <div>
          <span class="text-gray-400">Via:</span>
          {% for src in intel.source_types[:3] %}
            {% set source_name = src %}
            {% include "htmx/partials/shared/source_badge.html" %}
          {% endfor %}
          {% if intel.source_types|length > 3 %}
          <span class="text-gray-400 text-[10px]">+{{ intel.source_types|length - 3 }}</span>
          {% endif %}
        </div>
        {% endif %}

        {% if intel.get('tier') %}
        <div>
          <span class="text-gray-400">Evidence:</span>
          <span class="text-gray-700">{{ intel.tier }}</span>
        </div>
        {% endif %}

        {% if s.avg_price and s.best_price %}
        <div>
          <span class="text-gray-400">Price Range:</span>
          <span class="font-mono text-gray-700">${{ '%.2f'|format(s.best_price) }} — ${{ '%.2f'|format(s.avg_price) }}</span>
        </div>
        {% endif %}

        {% if intel.get('response_rate') is not none %}
        <div>
          <span class="text-gray-400">Response Rate:</span>
          <span class="text-gray-700">{{ (intel.response_rate * 100)|int }}%</span>
        </div>
        {% endif %}

        {% if intel.get('ghost_rate') is not none %}
        <div>
          <span class="text-gray-400">Ghost Rate:</span>
          <span class="{{ 'text-rose-600' if intel.ghost_rate > 0.3 else 'text-gray-700' }}">{{ (intel.ghost_rate * 100)|int }}%</span>
        </div>
        {% endif %}

        {% if intel.get('best_lead_time_days') %}
        <div>
          <span class="text-gray-400">Lead Time:</span>
          <span class="text-gray-700">{{ intel.best_lead_time_days }} day{{ 's' if intel.best_lead_time_days != 1 }}</span>
        </div>
        {% endif %}

        {% if intel.get('min_moq') %}
        <div>
          <span class="text-gray-400">MOQ:</span>
          <span class="font-mono text-gray-700">{{ intel.min_moq }}</span>
        </div>
        {% endif %}

        {% if vendor_phone %}
        <div>
          <span class="text-gray-400">Phone:</span>
          <a href="tel:{{ vendor_phone }}" class="text-brand-500 hover:text-brand-700">{{ vendor_phone }}</a>
        </div>
        {% endif %}
      </div>

      {# explain_lead one-liner #}
      {% if intel.get('explain_lead') %}
      <p class="text-[11px] text-gray-500 italic border-t border-gray-100 pt-1.5 mt-1">
        {{ intel.explain_lead }}
      </p>
      {% endif %}

      {# Actions row #}
      <div class="flex items-center gap-2 mt-2 pt-1.5 border-t border-gray-100">
        {% if vs != 'blacklisted' and vs != 'unavailable' %}
        <button @click.stop="$dispatch('open-modal', {url: '/v2/partials/sightings/vendor-modal?requirement_ids={{ requirement.id }}&preselect={{ s.vendor_name|urlencode }}'})"
                class="text-[10px] text-brand-600 hover:text-brand-800 font-medium">
          Send RFQ
        </button>
        <button hx-post="/v2/partials/sightings/{{ requirement.id }}/mark-unavailable"
                hx-vals='{"vendor_name": "{{ s.vendor_name }}"}'
                hx-target="#sightings-detail"
                hx-swap="innerHTML"
                hx-confirm="Mark {{ s.vendor_name }} as unavailable for this part?"
                data-loading-disable
                @click.stop
                class="text-[10px] text-gray-400 hover:text-rose-500">
          Mark Unavail
        </button>
        {% endif %}
      </div>
    </div>
  </td>
</tr>
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/htmx/partials/sightings/_vendor_row.html
git commit -m "feat(sightings): create vendor row sub-partial with expandable intelligence"
```

---

### Task 9: OOO Contact Detection — Backend

**Files:** `app/routers/sightings.py`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write failing test for OOO detection**

Add to `tests/test_sightings_router.py`:

```python
class TestOOODetection:
    """Phase 3: OOO contact detection in detail panel."""

    def test_ooo_badge_shown(self, client, db_session):
        req, r, s = _seed_data(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
        )
        db_session.add(vc)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=vc.id,
            contact_type="sales",
            email="test@good.com",
            source="email",
            is_ooo=True,
            ooo_return_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "OOO" in resp.text

    def test_no_ooo_badge_when_not_ooo(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert "OOO" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestOOODetection -v
```

- [ ] **Step 3: Add OOO batch query to `sightings_detail()`**

In `app/routers/sightings.py`, in `sightings_detail()`, after the vendor intelligence block, add:

```python
    # ── OOO Contact Detection (Phase 3) ──────────────────────────
    ooo_map: dict[str, VendorContact] = {}
    if normalized_names:
        contacts_with_ooo = (
            db.query(VendorContact)
            .join(VendorCard, VendorContact.vendor_card_id == VendorCard.id)
            .filter(
                VendorCard.normalized_name.in_(normalized_names),
                VendorContact.is_ooo.is_(True),
            )
            .all()
        )
        # Build id-keyed map for contact→card resolution
        card_id_map = {c.id: c for c in cards} if cards else {}
        for c in contacts_with_ooo:
            # Map by normalized vendor name for template lookup
            vc = card_id_map.get(c.vendor_card_id)
            if vc:
                ooo_map[vc.normalized_name] = c
            else:
                ooo_map[card.normalized_name] = c
```

Add `ooo_map` to the context dict:

```python
        "ooo_map": ooo_map,
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestOOODetection -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat(sightings): add OOO contact detection batch query to detail"
```

---

### Task 10: Suggested Action — Backend + Sub-Partial

**Files:** `app/routers/sightings.py`, `app/templates/htmx/partials/sightings/_suggested_action.html`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write failing tests for suggested action**

Add to `tests/test_sightings_router.py`:

```python
class TestSuggestedAction:
    """Phase 3: State-machine-driven suggested next action."""

    def test_open_with_sightings(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert "send RFQ" in resp.text.lower() or "vendor" in resp.text.lower()

    def test_open_no_sightings(self, client, db_session):
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id, primary_mpn="EMPTY-001",
            manufacturer="TestMfr", target_qty=100, sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert "search" in resp.text.lower() or "no vendor" in resp.text.lower()

    def test_quoted_status(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        r.sourcing_status = "quoted"
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert "customer" in resp.text.lower() or "awaiting" in resp.text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSuggestedAction -v
```

- [ ] **Step 3: Compute suggested action in `sightings_detail()`**

In `app/routers/sightings.py`, in `sightings_detail()`, after the OOO block, add:

```python
    # ── Suggested Next Action (Phase 3) ──────────────────────────
    status = requirement.sourcing_status or "open"
    vendor_count = len(summaries)
    pending_count_detail = len(pending_offers)

    if status == "open" and vendor_count > 0:
        suggested_action = f"{vendor_count} vendor{'s' if vendor_count != 1 else ''} available — send RFQs"
    elif status == "open" and vendor_count == 0:
        suggested_action = "No vendors found — run search"
    elif status == "sourcing":
        # Check days since last RFQ activity
        last_rfq = (
            db.query(sqlfunc.max(ActivityLog.created_at))
            .filter(
                ActivityLog.requirement_id == requirement_id,
                ActivityLog.activity_type == "rfq_sent",
            )
            .scalar()
        )
        if last_rfq:
            days_since = (datetime.now(timezone.utc).replace(tzinfo=None) - last_rfq).days
            if days_since > 3:
                suggested_action = f"RFQs pending for {days_since} days — follow up"
            else:
                suggested_action = "RFQs sent — awaiting vendor responses"
        else:
            suggested_action = "Status is sourcing but no RFQs sent — send RFQs"
    elif status == "offered" and pending_count_detail > 0:
        suggested_action = f"{pending_count_detail} offer{'s' if pending_count_detail != 1 else ''} received — review and accept/reject"
    elif status == "offered":
        suggested_action = "Offers reviewed — advance to quoted when ready"
    elif status == "quoted":
        suggested_action = "Quote sent — awaiting customer response"
    elif status == "won":
        suggested_action = "Order won — proceed to fulfillment"
    else:
        suggested_action = None
```

Add `suggested_action` to the context dict:

```python
        "suggested_action": suggested_action,
```

- [ ] **Step 4: Create suggested action sub-partial**

Create `app/templates/htmx/partials/sightings/_suggested_action.html`:

```html
{# _suggested_action.html — State-machine-driven next action prompt.
   Called by: detail.html (include)
   Depends on: nothing
   Context: suggested_action (str or None)
#}
{% if suggested_action %}
<div class="flex items-center gap-2 px-3 py-1.5 bg-brand-50/50 rounded-lg border border-brand-100 mb-3">
  <svg class="h-3.5 w-3.5 text-brand-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
    <path stroke-linecap="round" stroke-linejoin="round" d="M13 7l5 5m0 0l-5 5m5-5H6"/>
  </svg>
  <span class="text-xs text-brand-700 font-medium">{{ suggested_action }}</span>
</div>
{% endif %}
```

- [ ] **Step 5: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSuggestedAction -v
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/sightings.py app/templates/htmx/partials/sightings/_suggested_action.html tests/test_sightings_router.py
git commit -m "feat(sightings): add suggested next action to detail panel"
```

---

### Task 11: MaterialCard Enrichment Bar

**Files:** `app/routers/sightings.py`, `app/templates/htmx/partials/sightings/_enrichment_bar.html`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write failing test for enrichment bar**

Add to `tests/test_sightings_router.py`:

```python
from app.models.intelligence import MaterialCard


class TestEnrichmentBar:
    """Phase 3: MaterialCard enrichment bar in detail panel."""

    def test_enrichment_bar_shown(self, client, db_session):
        req, r, _ = _seed_data(db_session)
        mc = MaterialCard(
            normalized_mpn="test-mpn-001",
            display_mpn="TEST-MPN-001",
            lifecycle_status="active",
            category="Microcontroller",
            rohs_status="compliant",
        )
        db_session.add(mc)
        db_session.flush()
        r.material_card_id = mc.id
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Microcontroller" in resp.text or "active" in resp.text.lower()

    def test_enrichment_bar_hidden_no_card(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        # No material_card_id set, bar should not render
        assert "lifecycle" not in resp.text.lower()

    def test_eol_warning_shown(self, client, db_session):
        req, r, _ = _seed_data(db_session)
        mc = MaterialCard(
            normalized_mpn="test-mpn-001",
            display_mpn="TEST-MPN-001",
            lifecycle_status="eol",
            category="Memory",
        )
        db_session.add(mc)
        db_session.flush()
        r.material_card_id = mc.id
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert "eol" in resp.text.lower() or "end of life" in resp.text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestEnrichmentBar -v
```

- [ ] **Step 3: Fetch MaterialCard in `sightings_detail()`**

In `app/routers/sightings.py`, in `sightings_detail()`, after the suggested action block, add:

```python
    # ── MaterialCard Enrichment (Phase 3) ─────────────────────────
    from ..models.intelligence import MaterialCard

    material_card = None
    if requirement.material_card_id:
        material_card = db.get(MaterialCard, requirement.material_card_id)
```

Add `material_card` to the context dict:

```python
        "material_card": material_card,
```

- [ ] **Step 4: Create enrichment bar sub-partial**

Create `app/templates/htmx/partials/sightings/_enrichment_bar.html`:

```html
{# _enrichment_bar.html — MaterialCard enrichment info bar.
   Called by: detail.html (include)
   Depends on: nothing
   Context: material_card (MaterialCard or None)
#}
{% if material_card %}
{% set is_eol = material_card.lifecycle_status in ('eol', 'obsolete', 'ltb') %}
<div class="border-b border-gray-100 pb-3 mb-3"
     x-data="{ open: {{ 'true' if is_eol else 'false' }} }">
  <button @click="open = !open"
          class="flex items-center justify-between w-full text-left">
    <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider flex items-center gap-1.5">
      Material Intelligence
      {% if is_eol %}
      <span class="inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded-full bg-rose-50 text-rose-700">
        {{ material_card.lifecycle_status|upper }}
      </span>
      {% endif %}
    </h4>
    <svg :class="open ? 'rotate-180' : ''" class="h-3.5 w-3.5 text-gray-400 transition-transform" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/>
    </svg>
  </button>

  <div x-show="open" x-collapse class="mt-2 flex items-center gap-3 flex-wrap text-xs">
    {% if material_card.lifecycle_status %}
    <div class="flex items-center gap-1">
      <span class="text-gray-400">Lifecycle:</span>
      {% set lc = material_card.lifecycle_status|lower %}
      {% set lc_color = 'text-rose-600' if lc in ('eol', 'obsolete') else ('text-amber-600' if lc == 'ltb' else 'text-emerald-600' if lc == 'active' else 'text-gray-600') %}
      <span class="font-medium {{ lc_color }}">{{ material_card.lifecycle_status|upper }}</span>
    </div>
    {% endif %}

    {% if material_card.category %}
    <div class="flex items-center gap-1">
      <span class="text-gray-400">Category:</span>
      <span class="text-gray-700">{{ material_card.category }}</span>
    </div>
    {% endif %}

    {% if material_card.rohs_status %}
    <div class="flex items-center gap-1">
      <span class="text-gray-400">RoHS:</span>
      {% set rohs_color = 'text-emerald-600' if material_card.rohs_status == 'compliant' else 'text-rose-600' %}
      <span class="{{ rohs_color }}">{{ material_card.rohs_status|capitalize }}</span>
    </div>
    {% endif %}

    {% if material_card.datasheet_url %}
    <a href="{{ material_card.datasheet_url }}" target="_blank" rel="noopener"
       class="inline-flex items-center gap-0.5 text-brand-500 hover:text-brand-700">
      <svg class="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/>
      </svg>
      Datasheet
    </a>
    {% endif %}
  </div>
</div>
{% endif %}
```

- [ ] **Step 5: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestEnrichmentBar -v
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/sightings.py app/templates/htmx/partials/sightings/_enrichment_bar.html tests/test_sightings_router.py
git commit -m "feat(sightings): add MaterialCard enrichment bar to detail panel"
```

---

### Task 12: Better Empty States

**Files:** `app/templates/htmx/partials/sightings/table.html`, `app/templates/htmx/partials/sightings/detail.html`, `tests/test_sightings_router.py`

- [ ] **Step 1: Write test for empty state rendering**

Add to `tests/test_sightings_router.py`:

```python
class TestEmptyStates:
    """Phase 3: Better empty states with contextual CTAs."""

    def test_empty_table_shows_cta(self, client, db_session):
        """Filtered table with no results shows clear-filters CTA."""
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?status=won")
        assert resp.status_code == 200
        assert "no requirements" in resp.text.lower() or "clear" in resp.text.lower()

    def test_empty_sightings_in_detail(self, client, db_session):
        """Requirement with no sightings shows Run Search CTA."""
        req = Requisition(name="Test RFQ", status="active", customer_name="Acme Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id, primary_mpn="EMPTY-001",
            manufacturer="TestMfr", target_qty=100, sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert "no vendor" in resp.text.lower() or "run search" in resp.text.lower() or "no sighting" in resp.text.lower()
```

- [ ] **Step 2: Update table empty state in `table.html`**

In `app/templates/htmx/partials/sightings/table.html`, replace the empty state block (lines 65-68, the `{% if not requirements %}` block) with:

```html
  {% if not requirements %}
  <div class="p-8">
    {% with message="No requirements match your filters", action_label="Clear Filters" %}
    {% include "htmx/partials/shared/empty_state.html" %}
    {% endwith %}
  </div>
  {% else %}
```

- [ ] **Step 3: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestEmptyStates -v
```

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/sightings/table.html tests/test_sightings_router.py
git commit -m "feat(sightings): better empty states with contextual CTAs"
```

---

### Task 13: Detail Panel Template Decomposition

**Files:** `app/templates/htmx/partials/sightings/detail.html`

This is the integration task that wires all sub-partials into the detail panel. It also adds the `today` and `stale_days` context variables needed by the templates.

- [ ] **Step 1: Add `today` and `stale_days` to detail context**

In `app/routers/sightings.py`, in `sightings_detail()`, add to the context dict:

```python
        "today": datetime.now(timezone.utc).date(),
        "stale_days": settings.sighting_stale_days,
```

- [ ] **Step 2: Rewrite `detail.html` to use sub-partials**

Replace the entire contents of `app/templates/htmx/partials/sightings/detail.html` with:

```html
{# Sightings detail panel — requirement info + vendor breakdown + activity.
   Called by: GET /v2/partials/sightings/{id}/detail (sightings router)
   Depends on: _macros.html, _constraints.html, _vendor_row.html,
               _suggested_action.html, _enrichment_bar.html,
               activity_timeline.html, source_badge.html
   Context: requirement, requisition, summaries, vendor_statuses,
            pending_offers, vendor_phones, vendor_intel, ooo_map,
            activities, all_buyers, user, suggested_action,
            material_card, today, stale_days
#}

{% import "htmx/partials/shared/_macros.html" as m %}

{# ── Part Header ─────────────────────────────────────────────── #}
<div class="border-b border-gray-100 pb-3 mb-3">
  <div class="flex items-start justify-between">
    <div>
      <h3 class="text-base font-semibold text-gray-900 font-mono">{{ requirement.primary_mpn }}</h3>
      {% if requirement.manufacturer %}
      <p class="text-xs text-gray-500">{{ requirement.manufacturer }}</p>
      {% endif %}
    </div>
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
  </div>

  <div class="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
    <div>
      <span class="text-gray-400">Qty:</span>
      <span class="font-mono text-gray-700">{{ requirement.target_qty or '—' }}</span>
    </div>
    <div>
      <span class="text-gray-400">Target:</span>
      <span class="font-mono text-gray-700">{{ '$%.2f'|format(requirement.target_price) if requirement.target_price else '—' }}</span>
    </div>
    <div>
      <span class="text-gray-400">Customer:</span>
      <a href="/v2/requisitions/{{ requisition.id }}" class="text-brand-600 hover:underline">
        {{ requisition.customer_name or '—' }}
      </a>
    </div>
    <div>
      <span class="text-gray-400">Buyer:</span>
      <select hx-patch="/v2/partials/sightings/{{ requirement.id }}/assign"
              hx-target="#sightings-detail"
              hx-swap="innerHTML"
              data-loading-disable
              name="assigned_buyer_id"
              class="inline-block border-0 border-b border-dashed border-gray-300 bg-transparent text-xs text-gray-700 p-0 pr-4 focus:ring-0 cursor-pointer hover:border-brand-400">
        <option value="">Unassigned</option>
        {% for b in all_buyers %}
        <option value="{{ b.id }}" {{ 'selected' if requirement.assigned_buyer_id == b.id }}>
          {{ b.name }}
        </option>
        {% endfor %}
      </select>
    </div>
  </div>
</div>

{# ── Suggested Action ────────────────────────────────────────── #}
{% include "htmx/partials/sightings/_suggested_action.html" %}

{# ── MaterialCard Enrichment Bar ─────────────────────────────── #}
{% include "htmx/partials/sightings/_enrichment_bar.html" %}

{# ── Requirement Constraints ─────────────────────────────────── #}
{% include "htmx/partials/sightings/_constraints.html" %}

{# ── Vendor Breakdown ────────────────────────────────────────── #}
<div class="mb-4 border-t border-gray-100 pt-3">
  <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
    Vendors ({{ summaries|length }})
  </h4>

  {% if not summaries %}
  <div class="py-4 text-center border border-dashed border-gray-200 rounded-lg">
    <p class="text-xs text-gray-400 mb-2">No vendors found yet</p>
    <button hx-post="/v2/partials/sightings/{{ requirement.id }}/refresh"
            hx-target="#sightings-detail"
            hx-swap="innerHTML"
            data-loading-disable
            class="text-xs text-brand-500 hover:text-brand-700 font-medium">
      Run Search Now
    </button>
  </div>
  {% else %}
  <div x-data="{ showAll: false }">
  <table class="compact-table w-full">
    <tbody>
      {% for s in summaries %}
      {% set vs = vendor_statuses.get(s.vendor_name, 'sighting') %}
      {# Show first 5 always, rest gated by showAll #}
      {% if loop.index <= 5 %}
      {% include "htmx/partials/sightings/_vendor_row.html" %}
      {% endif %}
      {% endfor %}
      {# Remaining vendors, hidden by default #}
      {% if summaries|length > 5 %}
      <template x-if="showAll">
        <tbody>
        {% for s in summaries %}
        {% if loop.index > 5 %}
        {% set vs = vendor_statuses.get(s.vendor_name, 'sighting') %}
        {% include "htmx/partials/sightings/_vendor_row.html" %}
        {% endif %}
        {% endfor %}
        </tbody>
      </template>
      {% endif %}
    </tbody>
  </table>

  {# Collapse toggle at 5 vendors #}
  {% if summaries|length > 5 %}
    <button x-show="!showAll" @click="showAll = true"
            class="mt-1 text-[11px] text-brand-500 hover:text-brand-700" x-collapse>
      Show all {{ summaries|length }} vendors
    </button>
    <button x-show="showAll" @click="showAll = false"
            class="mt-1 text-[11px] text-gray-400 hover:text-gray-600">
      Show top 5 only
    </button>
  {% endif %}
  </div>
  {% endif %}

  {# ── Pending Offers (AI-parsed, need approval) ──────────── #}
  {% if pending_offers %}
  <div class="mt-3 border-t border-gray-100 pt-3">
    <h4 class="text-xs font-semibold text-amber-600 uppercase tracking-wider mb-2">
      Pending Review ({{ pending_offers|length }})
    </h4>
    {% for o in pending_offers %}
    <div class="flex items-center justify-between py-1.5 border-b border-gray-50 last:border-0">
      <div class="text-xs">
        <span class="font-medium text-gray-700">{{ o.vendor_name }}</span>
        <span class="text-gray-400 ml-2">{{ o.qty_available or '?' }} pcs @ ${{ '%.2f'|format(o.unit_price) if o.unit_price else '?' }}</span>
      </div>
      <div class="flex gap-1">
        <button hx-put="/api/offers/{{ o.id }}/approve"
                hx-target="#sightings-detail"
                hx-swap="innerHTML"
                data-loading-disable
                class="px-2 py-0.5 text-[10px] font-medium rounded bg-emerald-50 text-emerald-700 hover:bg-emerald-100">
          Approve
        </button>
        <button hx-put="/api/offers/{{ o.id }}/reject"
                hx-target="#sightings-detail"
                hx-swap="innerHTML"
                data-loading-disable
                class="px-2 py-0.5 text-[10px] font-medium rounded bg-gray-50 text-gray-500 hover:bg-gray-100">
          Reject
        </button>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

{# ── Activity Timeline ──────────────────────────────────────── #}
<div>
  <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Activity</h4>
  {% if activities %}
  {% include "htmx/partials/shared/activity_timeline.html" %}
  {% else %}
  <div class="py-4 text-center border border-dashed border-gray-200 rounded-lg">
    <p class="text-xs text-gray-400">No activity recorded yet</p>
    <p class="text-[10px] text-gray-300 mt-1">Log a note to start tracking</p>
  </div>
  {% endif %}
</div>
```

- [ ] **Step 3: Run full sightings test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v
```

Expected: All existing + new tests PASS.

- [ ] **Step 4: Commit**

```bash
git add app/routers/sightings.py app/templates/htmx/partials/sightings/detail.html
git commit -m "feat(sightings): decompose detail panel into sub-partials with full intelligence"
```

---

### Task 14: Final Integration Test

**Files:** `tests/test_sightings_router.py`

- [ ] **Step 1: Write full integration test**

Add to `tests/test_sightings_router.py`:

```python
class TestPhaseBIntegration:
    """End-to-end test that all Phase 2+3 features work together."""

    def test_full_page_with_all_features(self, client, db_session):
        """Sightings page renders with dashboard, coverage, heatmap, intelligence."""
        req, r, s = _seed_data(db_session)
        r.priority_score = 85.0
        r.condition = "New Original"
        r.need_by_date = None
        db_session.commit()

        # Table load
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        text = resp.text.lower()
        assert "urgent" in text  # dashboard
        assert "coverage" in text or "%" in resp.text  # coverage bar
        assert "bg-rose-50/30" in resp.text  # heatmap (high priority)

        # Detail load
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200
        assert "Good Vendor" in resp.text  # vendor row
        assert "Constraints" in resp.text  # constraints section
        assert "send RFQ" in resp.text.lower() or "vendor" in resp.text.lower()  # suggested action
```

- [ ] **Step 2: Run integration test**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestPhaseBIntegration -v
```

- [ ] **Step 3: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v
```

Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_sightings_router.py
git commit -m "test(sightings): add Phase B integration test for visual triage + intelligence"
```
