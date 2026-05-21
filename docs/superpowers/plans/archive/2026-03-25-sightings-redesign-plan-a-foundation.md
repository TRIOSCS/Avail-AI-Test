# Sightings Redesign Plan A: Foundation + Performance

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 55-query-per-page performance problem, add SourcingStatus transitions, run Alembic migrations for new VendorSightingSummary columns, and replace hand-rolled UI with shared components.

**Architecture:** This plan addresses Phases 0-1 from the spec at `docs/superpowers/specs/2026-03-25-sightings-page-redesign.md`. It creates the foundation that Plans B (visual triage + intelligence) and C (workflow actions + real-time) build on. All changes are backward-compatible — the page works the same after this plan, just faster and using shared components.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Alembic, Jinja2, Alpine.js, HTMX

**Spec:** `docs/superpowers/specs/2026-03-25-sightings-page-redesign.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/services/status_machine.py` | Modify | Add SourcingStatus transitions |
| `app/constants.py` | Read-only | SourcingStatus enum (already exists) |
| `app/services/activity_service.py` | Modify | Add `requirement_id` param to `log_rfq_activity` |
| `app/models/vendor_sighting_summary.py` | Modify | Add 5 new columns |
| `app/services/sighting_aggregation.py` | Modify | Compute new columns in rebuild |
| `app/static/htmx_app.js` | Modify | Register `splitPanel` + `sightingSelection` store |
| `app/routers/sightings.py` | Modify | Eager loading, caching, phone fallback removal, batch limit |
| `app/services/sighting_status.py` | Modify | Merge 4 queries into 1 |
| `app/templates/htmx/partials/sightings/list.html` | Modify | Use shared split_panel |
| `app/templates/htmx/partials/sightings/table.html` | Modify | Use shared macros |
| `app/templates/htmx/partials/sightings/detail.html` | Modify | Use shared macros |
| `app/templates/htmx/partials/shared/pagination.html` | Modify | Add configurable hx_target |
| `app/templates/htmx/partials/shared/split_panel.html` | Modify | Remove inline script |
| `alembic/versions/XXX_add_vss_preaggregated_fields.py` | Create | Schema migration |
| `alembic/versions/XXX_backfill_vss_preaggregated_fields.py` | Create | Data backfill |
| `tests/test_workflow_state_clarity.py` | Modify | Add SourcingStatus transition tests |
| `tests/test_sightings_router.py` | Modify | Update for eager loading, batch limit |
| `tests/test_sighting_aggregation.py` | Modify | Update phone tests, add new column tests |

---

### Task 1: Add SourcingStatus Transition Map

**Files:**
- Modify: `app/services/status_machine.py`
- Modify: `tests/test_workflow_state_clarity.py`

- [ ] **Step 1: Write failing tests for SourcingStatus transitions**

Add a new test class in `tests/test_workflow_state_clarity.py`:

```python
from app.services.status_machine import validate_transition


class TestSourcingStatusTransitions:
    """Verify SourcingStatus transitions in status_machine.py."""

    def test_open_to_sourcing_valid(self):
        assert validate_transition("requirement", "open", "sourcing") is True

    def test_sourcing_to_offered_valid(self):
        assert validate_transition("requirement", "sourcing", "offered") is True

    def test_offered_to_quoted_valid(self):
        assert validate_transition("requirement", "offered", "quoted") is True

    def test_open_to_won_invalid(self):
        """Skipping states should be rejected."""
        assert validate_transition("requirement", "open", "won", raise_on_invalid=False) is False

    def test_archived_is_terminal(self):
        """No transitions from archived."""
        assert validate_transition("requirement", "archived", "open", raise_on_invalid=False) is False

    def test_noop_same_status_valid(self):
        assert validate_transition("requirement", "open", "open") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py::TestSourcingStatusTransitions -v`

Expected: `test_open_to_won_invalid` and `test_archived_is_terminal` FAIL (currently returns True for all transitions due to missing map).

- [ ] **Step 3: Add SOURCING_TRANSITIONS to status_machine.py**

Add after line 100 (after `REQUISITION_TRANSITIONS`), before `require_valid_transition`:

```python
# ── Sourcing Status Transitions (Requirement-level) ────────────────────
SOURCING_TRANSITIONS: dict[str, set[str]] = {
    SourcingStatus.OPEN: {SourcingStatus.SOURCING, SourcingStatus.ARCHIVED},
    SourcingStatus.SOURCING: {SourcingStatus.OFFERED, SourcingStatus.OPEN, SourcingStatus.ARCHIVED},
    SourcingStatus.OFFERED: {SourcingStatus.QUOTED, SourcingStatus.SOURCING, SourcingStatus.ARCHIVED},
    SourcingStatus.QUOTED: {SourcingStatus.WON, SourcingStatus.LOST, SourcingStatus.OFFERED, SourcingStatus.ARCHIVED},
    SourcingStatus.WON: {SourcingStatus.ARCHIVED},
    SourcingStatus.LOST: {SourcingStatus.OPEN, SourcingStatus.ARCHIVED},
    SourcingStatus.ARCHIVED: set(),  # terminal
}
```

Add `SourcingStatus` to the import at line 14:

```python
from ..constants import (
    BuyPlanStatus,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
)
```

Add `"requirement": SOURCING_TRANSITIONS` to the `transition_map` dict inside `validate_transition()` at line 123-128:

```python
    transition_map = {
        "offer": OFFER_TRANSITIONS,
        "quote": QUOTE_TRANSITIONS,
        "buy_plan": BUY_PLAN_TRANSITIONS,
        "requisition": REQUISITION_TRANSITIONS,
        "requirement": SOURCING_TRANSITIONS,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_workflow_state_clarity.py::TestSourcingStatusTransitions -v`

Expected: All 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/status_machine.py tests/test_workflow_state_clarity.py
git commit -m "feat: add SourcingStatus transitions to status machine"
```

---

### Task 2: Extend `log_rfq_activity` with `requirement_id`

**Files:**
- Modify: `app/services/activity_service.py:663-683`

- [ ] **Step 1: Add `requirement_id` parameter**

Change the function signature and body at line 663-683:

```python
def log_rfq_activity(
    db: Session,
    rfq_id: int,
    activity_type: str,
    description: str,
    metadata: dict | None = None,
    user_id: int | None = None,
    requirement_id: int | None = None,
) -> ActivityLog:
    """Log an activity entry on the RFQ activity timeline."""
    record = ActivityLog(
        user_id=user_id,
        activity_type=activity_type,
        channel="system",
        requisition_id=rfq_id,
        requirement_id=requirement_id,
        notes=description,
        details=metadata,
    )
    db.add(record)
    db.flush()
    logger.info(f"RFQ activity logged: {activity_type} -> rfq {rfq_id}")
    return record
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "activity" -v --timeout=30`

Expected: All existing activity tests pass (new param is optional with default None).

- [ ] **Step 3: Commit**

```bash
git add app/services/activity_service.py
git commit -m "feat: add requirement_id param to log_rfq_activity"
```

---

### Task 3: Alembic Migration — New VendorSightingSummary Columns

**Files:**
- Modify: `app/models/vendor_sighting_summary.py`
- Create: `alembic/versions/XXX_add_vss_preaggregated_fields.py` (autogenerated)
- Create: `alembic/versions/XXX_backfill_vss_preaggregated_fields.py` (manual)

- [ ] **Step 1: Add columns to VendorSightingSummary model**

In `app/models/vendor_sighting_summary.py`, add after `updated_at` (line 47):

```python
    # Pre-aggregated fields (rebuilt by sighting_aggregation service)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True)
    newest_sighting_at = Column(DateTime, nullable=True)
    best_lead_time_days = Column(Integer, nullable=True)
    min_moq = Column(Integer, nullable=True)
    has_contact_info = Column(Boolean, default=False, server_default="false")
```

Add `Boolean` to the import:

```python
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
```

Add new indexes to `__table_args__`:

```python
    __table_args__ = (
        UniqueConstraint("requirement_id", "vendor_name", name="uq_vss_req_vendor"),
        Index("ix_vss_requirement", "requirement_id"),
        Index("ix_vss_vendor", "vendor_name"),
        Index("ix_vss_score", "score"),
        Index("ix_vss_vendor_card", "vendor_card_id"),
        Index("ix_vss_vendor_req", "vendor_name", "requirement_id"),
    )
```

Add relationship:

```python
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
```

- [ ] **Step 2: Generate Alembic migration**

Run: `cd /root/availai && alembic revision --autogenerate -m "add vendor_card_id and pre-aggregated fields to vendor_sighting_summary"`

Review the generated file — it should add 5 columns and 2 indexes.

- [ ] **Step 3: Create backfill migration**

Run: `cd /root/availai && alembic revision -m "backfill vendor_sighting_summary pre-aggregated fields"`

Edit the generated file with:

```python
from alembic import op
import sqlalchemy as sa


def upgrade():
    conn = op.get_bind()
    # Backfill vendor_card_id
    conn.execute(sa.text("""
        UPDATE vendor_sighting_summary vss
        SET vendor_card_id = vc.id
        FROM vendor_cards vc
        WHERE vss.vendor_name = vc.normalized_name
    """))
    # Backfill vendor_phone from VendorCard where NULL
    conn.execute(sa.text("""
        UPDATE vendor_sighting_summary vss
        SET vendor_phone = (vc.phones->>0)
        FROM vendor_cards vc
        WHERE vss.vendor_name = vc.normalized_name
          AND vss.vendor_phone IS NULL
          AND vc.phones IS NOT NULL
          AND jsonb_array_length(vc.phones) > 0
    """))
    # Backfill aggregated fields from sightings
    conn.execute(sa.text("""
        UPDATE vendor_sighting_summary vss SET
          newest_sighting_at = sub.newest,
          best_lead_time_days = sub.best_lt,
          min_moq = sub.min_moq,
          has_contact_info = sub.has_contact
        FROM (
          SELECT requirement_id, LOWER(TRIM(vendor_name)) as vn,
            MAX(created_at) AS newest,
            MIN(lead_time_days) FILTER (WHERE lead_time_days IS NOT NULL) AS best_lt,
            MIN(moq) FILTER (WHERE moq IS NOT NULL) AS min_moq,
            BOOL_OR(vendor_email IS NOT NULL OR vendor_phone IS NOT NULL) AS has_contact
          FROM sightings WHERE NOT is_unavailable
          GROUP BY requirement_id, LOWER(TRIM(vendor_name))
        ) sub
        WHERE sub.vn = vss.vendor_name
          AND sub.requirement_id = vss.requirement_id
    """))


def downgrade():
    pass  # data-only migration
```

- [ ] **Step 4: Test migrations (upgrade → downgrade → upgrade)**

Run: `cd /root/availai && alembic upgrade head && alembic downgrade -1 && alembic downgrade -1 && alembic upgrade head`

Expected: No errors.

- [ ] **Step 5: Commit**

```bash
git add app/models/vendor_sighting_summary.py alembic/versions/
git commit -m "feat: add pre-aggregated fields to VendorSightingSummary with backfill"
```

---

### Task 4: Update `rebuild_vendor_summaries` for New Columns

**Files:**
- Modify: `app/services/sighting_aggregation.py:78-165`
- Modify: `tests/test_sighting_aggregation.py`

- [ ] **Step 1: Write failing test for new columns**

Add to `tests/test_sighting_aggregation.py`:

```python
class TestVendorSummaryNewColumns:
    """Verify new pre-aggregated columns are populated."""

    def test_newest_sighting_at_populated(self, db_session):
        req, sightings = _seed_data_with_timestamps(db_session)
        results = rebuild_vendor_summaries(db_session, req.id)
        assert results[0].newest_sighting_at is not None

    def test_best_lead_time_days_populated(self, db_session):
        req, sightings = _seed_data_with_lead_time(db_session)
        results = rebuild_vendor_summaries(db_session, req.id)
        assert results[0].best_lead_time_days == 3  # min of group

    def test_min_moq_populated(self, db_session):
        req, sightings = _seed_data_with_moq(db_session)
        results = rebuild_vendor_summaries(db_session, req.id)
        assert results[0].min_moq == 10  # min of group

    def test_vendor_card_id_set(self, db_session):
        req, sightings, card = _seed_data_with_vendor_card(db_session)
        results = rebuild_vendor_summaries(db_session, req.id)
        assert results[0].vendor_card_id == card.id
```

(Seed helpers will need to be created following the existing `_seed_data` pattern in the file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sighting_aggregation.py::TestVendorSummaryNewColumns -v`

Expected: FAIL — new columns not yet computed.

- [ ] **Step 3: Update `rebuild_vendor_summaries` to compute new columns**

In `app/services/sighting_aggregation.py`, modify the VendorCard query (lines 104-112) to also fetch `id`:

```python
        cards = (
            db.query(VendorCard.id, VendorCard.normalized_name, VendorCard.phones)
            .filter(VendorCard.normalized_name.in_(list(groups.keys())))
            .all()
        )
        vendor_card_ids: dict[str, int] = {}
        for card in cards:
            phones = card.phones or []
            vendor_phones[card.normalized_name] = phones[0] if phones else None
            vendor_card_ids[card.normalized_name] = card.id
```

In the per-group loop (after line 127), add:

```python
        # New pre-aggregated fields
        lead_times = [s.lead_time_days for s in group if s.lead_time_days is not None]
        moqs = [s.moq for s in group if s.moq is not None]
        newest = max((s.created_at for s in group if s.created_at), default=None)
        has_contact = any(s.vendor_email or s.vendor_phone for s in group) or bool(vendor_phones.get(vn))
```

In both the `if existing:` block (around line 131) and the `else:` block (around line 143), add:

```python
            existing.vendor_card_id = vendor_card_ids.get(vn)
            existing.newest_sighting_at = newest
            existing.best_lead_time_days = min(lead_times) if lead_times else None
            existing.min_moq = min(moqs) if moqs else None
            existing.has_contact_info = has_contact
```

(Same for the `VendorSightingSummary(...)` constructor in the else block.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sighting_aggregation.py -v`

Expected: All pass including the new tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/sighting_aggregation.py tests/test_sighting_aggregation.py
git commit -m "feat: compute pre-aggregated fields in rebuild_vendor_summaries"
```

---

### Task 5: Move `splitPanel` to htmx_app.js + Add `sightingSelection` Store

**Files:**
- Modify: `app/static/htmx_app.js`
- Modify: `app/templates/htmx/partials/shared/split_panel.html`

- [ ] **Step 1: Copy `splitPanel` registration to htmx_app.js**

In `app/static/htmx_app.js`, add after the existing `Alpine.store(...)` calls (around line 130, before `Alpine.start()`):

```javascript
// Split panel component (moved from shared/split_panel.html for HTMX swap compat)
Alpine.data('splitPanel', (panelId, defaultPct) => ({
    splitRatio: parseFloat(localStorage.getItem('avail_split_' + panelId) || (defaultPct / 100).toFixed(3)),
    dragging: false,
    startDrag(e) { this.dragging = true; e.preventDefault(); },
    onDrag(e) {
        if (!this.dragging) return;
        const rect = this.$refs.container.getBoundingClientRect();
        this.splitRatio = Math.max(0.25, Math.min(0.75, (e.clientX - rect.left) / rect.width));
    },
    stopDrag() {
        if (this.dragging) {
            this.dragging = false;
            localStorage.setItem('avail_split_' + panelId, this.splitRatio.toFixed(3));
        }
    },
    startTouchResize(e) { this.dragging = true; },
    onTouchResize(e) {
        if (!this.dragging || !e.touches.length) return;
        const rect = this.$refs.container.getBoundingClientRect();
        this.splitRatio = Math.max(0.25, Math.min(0.75, (e.touches[0].clientX - rect.left) / rect.width));
    },
}));

// Sightings multi-select store (reactive object, not Set)
Alpine.store('sightingSelection', {
    _map: {},
    toggle(id) {
        if (this._map[id]) { delete this._map[id]; }
        else { this._map[id] = true; }
    },
    has(id) { return !!this._map[id]; },
    clear() { this._map = {}; },
    get count() { return Object.keys(this._map).length; },
    get array() { return Object.keys(this._map).map(Number); },
});
```

- [ ] **Step 2: Remove inline script from split_panel.html and update HTML to match new API**

In `app/templates/htmx/partials/shared/split_panel.html`:
1. Delete the entire `<script>` block (the block that starts with `document.addEventListener('alpine:init', ...` and contains the `splitPanel` registration)
2. Update the template HTML to match the `splitRatio` API used in the moved JS. The existing template uses `leftWidth` (integer percentage). Update any `:style="'width:' + leftWidth + '%'"` to `:style="'width:' + (splitRatio * 100) + '%'"` and any `leftWidth` references to `splitRatio * 100`. Alternatively, keep the `leftWidth` property name in the moved JS code to avoid template changes — read the existing template first and match its API exactly.

- [ ] **Step 3: Verify the app still works by running existing tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add app/static/htmx_app.js app/templates/htmx/partials/shared/split_panel.html
git commit -m "refactor: move splitPanel to htmx_app.js, add sightingSelection store"
```

---

### Task 6: Fix Eager Loading (Eliminates 50 Queries)

**Files:**
- Modify: `app/routers/sightings.py:77-82`

- [ ] **Step 1: Chain the joinedload for Requisition.creator**

In `app/routers/sightings.py`, in the `sightings_list` function, change:

```python
        .options(joinedload(Requirement.requisition))
```

to:

```python
        .options(joinedload(Requirement.requisition).joinedload(Requisition.creator))
```

Add `Requisition` to the import from `..models.sourcing` if not already there.

- [ ] **Step 2: Run existing sightings tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`

Expected: All pass. This is a pure performance fix with no behavior change.

- [ ] **Step 3: Commit**

```bash
git add app/routers/sightings.py
git commit -m "perf: add eager load for requisition.creator, eliminates 50 lazy queries"
```

---

### Task 7: Add In-Process TTL Cache for `all_buyers` and `stat_counts`

**Files:**
- Modify: `app/routers/sightings.py`

- [ ] **Step 1: Add TTL cache helper at the top of the router module**

Add after the imports in `app/routers/sightings.py`:

```python
import time

_cache: dict[str, tuple[float, Any]] = {}


def _get_cached(key: str, ttl: float, factory):
    """Simple in-process TTL cache. For value tuples/dicts only (not ORM objects).
    Safe because cached results are detached column tuples, not session-bound objects."""
    now = time.monotonic()
    entry = _cache.get(key)
    if entry and now - entry[0] < ttl:
        return entry[1]
    result = factory()
    _cache[key] = (now, result)
    return result


def _invalidate_cache(key: str):
    """Remove a cached entry (call after mutations that change the data)."""
    _cache.pop(key, None)
```

- [ ] **Step 2: Use cache for `all_buyers` in `sightings_detail`**

In the `sightings_detail` function, replace:

```python
    all_buyers = db.query(User.id, User.name).filter(User.is_active.is_(True)).all()
```

with:

```python
    all_buyers = _get_cached(
        "all_buyers", 300,
        lambda: db.query(User.id, User.name).filter(User.is_active.is_(True)).all()
    )
```

- [ ] **Step 3: Use cache for `stat_counts` in `sightings_list`**

In the `sightings_list` function, replace the stat_counts query block with:

```python
    stat_counts = _get_cached(
        "sightings_stat_counts", 30,
        lambda: dict(
            db.query(Requirement.sourcing_status, sqlfunc.count())
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(Requisition.status == RequisitionStatus.ACTIVE)
            .group_by(Requirement.sourcing_status)
            .all()
        )
    )
```

- [ ] **Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py
git commit -m "perf: add TTL cache for all_buyers (5min) and stat_counts (30s)"
```

---

### Task 8: Remove Vendor Phone Fallback

**Files:**
- Modify: `app/routers/sightings.py` — in `sightings_detail()`
- Modify: `tests/test_sighting_aggregation.py` — update phone tests

- [ ] **Step 1: Replace the fallback block in sightings_detail**

In `app/routers/sightings.py`, in the `sightings_detail` function, replace the entire vendor phone lookup block (lines 212-224):

```python
    # Batch vendor phone lookup — single query instead of N+1
    vendor_names_needing_phone = [s.vendor_name for s in summaries if not s.vendor_phone]
    vendor_phones = {s.vendor_name: s.vendor_phone for s in summaries if s.vendor_phone}
    if vendor_names_needing_phone:
        normalized_names = [normalize_vendor_name(vn) for vn in vendor_names_needing_phone]
        cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all()
        card_phones = {}
        for card in cards:
            if card.phones:
                card_phones[card.normalized_name] = card.phones[0] if isinstance(card.phones, list) else card.phones
        for vn in vendor_names_needing_phone:
            phone = card_phones.get(normalize_vendor_name(vn))
            if phone:
                vendor_phones[vn] = phone
```

with:

```python
    vendor_phones = {s.vendor_name: s.vendor_phone for s in summaries if s.vendor_phone}
```

- [ ] **Step 2: Update phone tests in test_sighting_aggregation.py**

Update `TestVendorPhoneLookup` tests to test the `rebuild_vendor_summaries` phone population path (which now includes the backfilled data), rather than the removed fallback in the router.

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py tests/test_sighting_aggregation.py -v`

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add app/routers/sightings.py tests/test_sighting_aggregation.py
git commit -m "refactor: remove vendor phone fallback, use backfilled VSS data"
```

---

### Task 9: Add Batch Size Limit to batch-refresh

**Files:**
- Modify: `app/routers/sightings.py` — in `sightings_batch_refresh()`
- Modify: `tests/test_sightings_router.py`

- [ ] **Step 1: Write test for batch size limit**

Add to `tests/test_sightings_router.py`:

```python
class TestSightingsBatchLimit:
    def test_batch_refresh_over_limit_returns_400(self, client, db_session):
        ids = list(range(1, 52))  # 51 items, over the 50 limit
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps(ids)},
        )
        assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsBatchLimit -v`

Expected: FAIL (currently no limit check, returns 200).

- [ ] **Step 3: Add the limit check**

In `app/routers/sightings.py`, at the top of the file, add:

```python
MAX_BATCH_SIZE = 50
```

In `sightings_batch_refresh`, after `requirement_ids = json.loads(req_ids_raw)`, add:

```python
    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")
```

- [ ] **Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py::TestSightingsBatchLimit -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "feat: add MAX_BATCH_SIZE=50 limit to batch-refresh endpoint"
```

---

### Task 10: Merge `compute_vendor_statuses` from 4 Queries to 1

**Files:**
- Modify: `app/services/sighting_status.py`
- Modify: `tests/test_sightings_router.py`

- [ ] **Step 1: Write regression test**

Add to `tests/test_sightings_router.py`:

```python
class TestVendorStatusesMergedQuery:
    """Ensure merged query returns same results as the old 4-query approach."""

    def test_blacklisted_vendor_returns_blacklisted(self, client, db_session):
        # Seed: vendor card with is_blacklisted=True, sightings for it
        _seed_blacklisted_vendor(db_session)
        resp = client.get("/v2/partials/sightings/1/detail")
        assert resp.status_code == 200
        assert "Blacklisted" in resp.text

    def test_contacted_vendor_shows_contacted(self, client, db_session):
        _seed_contacted_vendor(db_session)
        resp = client.get("/v2/partials/sightings/1/detail")
        assert resp.status_code == 200
        assert "Contacted" in resp.text

    def test_unavailable_vendor_shows_unavailable(self, client, db_session):
        _seed_unavailable_vendor(db_session)
        resp = client.get("/v2/partials/sightings/1/detail")
        assert resp.status_code == 200
        assert "Unavailable" in resp.text

    def test_empty_requirement_returns_empty(self, client, db_session):
        _seed_empty_requirement(db_session)
        resp = client.get("/v2/partials/sightings/1/detail")
        assert resp.status_code == 200
```

- [ ] **Step 2: Implement merged query in sighting_status.py**

Replace the body of `compute_vendor_statuses` in `app/services/sighting_status.py` with a single batch query approach. Normalize all vendor names first, then use subqueries for each status dimension:

```python
def compute_vendor_statuses(
    requirement_id: int,
    requisition_id: int,
    db: Session,
    vendor_names: list[str] | None = None,
) -> dict[str, str]:
    """Compute vendor status for each vendor on a requirement.

    Priority: blacklisted > offer-in > contacted > unavailable > sighting
    Merged into a single pass with batched lookups.
    """
    if not vendor_names:
        return {}

    # Normalize all names for consistent matching
    normalized = {vn: normalize_vendor_name(vn) for vn in vendor_names}
    norm_set = set(normalized.values())

    # Batch 1: Blacklisted vendor cards
    bl_cards = set(
        row[0] for row in
        db.query(VendorCard.normalized_name)
        .filter(VendorCard.normalized_name.in_(norm_set), VendorCard.is_blacklisted.is_(True))
        .all()
    )

    # Batch 2: Vendors with offers on THIS requirement
    offer_vendors = set(
        row[0] for row in
        db.query(Offer.vendor_name)
        .filter(Offer.requirement_id == requirement_id)
        .all()
    )

    # Batch 3: Vendors contacted on THIS requisition
    contacted_vendors = set(
        row[0] for row in
        db.query(Contact.vendor_name)
        .filter(
            Contact.requisition_id == requisition_id,
            Contact.status.in_([ContactStatus.SENT, ContactStatus.OPENED, ContactStatus.RESPONDED]),
        )
        .all()
    )

    # Batch 4: Vendors with ALL sightings unavailable on THIS requirement
    unavail_vendors = set()
    sight_rows = (
        db.query(Sighting.vendor_name, Sighting.is_unavailable)
        .filter(Sighting.requirement_id == requirement_id)
        .all()
    )
    vendor_sights: dict[str, list[bool]] = {}
    for vn, is_u in sight_rows:
        vendor_sights.setdefault(vn, []).append(bool(is_u))
    for vn, flags in vendor_sights.items():
        if all(flags):
            unavail_vendors.add(vn)

    # Resolve statuses with priority: blacklisted > offer-in > contacted > unavailable > sighting
    result = {}
    for vn in vendor_names:
        norm = normalized[vn]
        if norm in bl_cards:
            result[vn] = "blacklisted"
        elif vn in offer_vendors or norm in {normalize_vendor_name(ov) for ov in offer_vendors}:
            result[vn] = "offer-in"
        elif vn in contacted_vendors or norm in {normalize_vendor_name(cv) for cv in contacted_vendors}:
            result[vn] = "contacted"
        elif vn in unavail_vendors:
            result[vn] = "unavailable"
        else:
            result[vn] = "sighting"
    return result
```

Add necessary imports at top: `from ..vendor_utils import normalize_vendor_name` and `from ..constants import ContactStatus`

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add app/services/sighting_status.py tests/test_sightings_router.py
git commit -m "perf: merge compute_vendor_statuses from 4 queries to batched lookups"
```

---

### Task 11: Fix Pagination hx-target

**Files:**
- Modify: `app/templates/htmx/partials/shared/pagination.html`

- [ ] **Step 1: Make hx-target configurable**

In `app/templates/htmx/partials/shared/pagination.html`, replace all instances of `hx-target="#main-content"` with `hx-target="{{ hx_target|default('#main-content') }}"`.

There are 3 occurrences (Prev button, Next button, page input).

- [ ] **Step 2: Pass `hx_target` from sightings table template**

In `app/templates/htmx/partials/sightings/table.html`, where the pagination include is:

```jinja2
    {% include "htmx/partials/shared/pagination.html" %}
```

Change to pass the target:

```jinja2
    {% set hx_target = "#sightings-table" %}
    {% include "htmx/partials/shared/pagination.html" %}
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`

Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/shared/pagination.html app/templates/htmx/partials/sightings/table.html
git commit -m "fix: make pagination hx-target configurable, fix sightings pagination"
```

---

### Task 12: Replace Hand-Rolled Components with Shared Partials

**Files:**
- Modify: `app/templates/htmx/partials/sightings/list.html`
- Modify: `app/templates/htmx/partials/sightings/table.html`
- Modify: `app/templates/htmx/partials/sightings/detail.html`

- [ ] **Step 1: Replace split panel in list.html**

Replace the inline `x-data` split panel with the shared component. Replace the 70 lines of inline code with:

```jinja2
{% include "htmx/partials/shared/split_panel.html" %}
```

With `panel_id="sightings"`, `left_url="/v2/partials/sightings"`, and override blocks for left/right panel content.

**Note:** The exact template syntax depends on how `split_panel.html` accepts parameters. Read it carefully and match its interface. The key change is removing the inline `x-data` with `splitRatio`, `dragging`, `startDrag`, `onDrag`, `stopDrag`, `selectReq` and instead using the registered `splitPanel` Alpine component from `htmx_app.js`.

Keep the `selectReq` method — add it to the component scope or use Alpine `x-data` alongside the split panel for sightings-specific state.

- [ ] **Step 2: Replace stat pills in table.html with `filter_pill` macro**

In `table.html`, replace the hand-rolled pill buttons (lines 19-28) with:

```jinja2
  {% for val, label, count in pills %}
    {{ m.filter_pill(label ~ ' ' ~ count, val, status, {
        'hx-get': '/v2/partials/sightings?status=' ~ val ~ '&q=' ~ q ~ '&group_by=' ~ group_by ~ '&assigned=' ~ assigned,
        'hx-target': '#sightings-table',
        'hx-swap': 'innerHTML'
    }) }}
  {% endfor %}
```

- [ ] **Step 3: Replace vendor status badges in detail.html with `status_badge` macro**

In `detail.html`, replace the inline `vs_styles`/`vs_labels` dicts (lines 96-112) with:

```jinja2
{% set vendor_status_map = {
    'sighting': 'bg-gray-100 text-gray-600',
    'contacted': 'bg-blue-50 text-blue-700',
    'offer-in': 'bg-emerald-50 text-emerald-700',
    'unavailable': 'bg-gray-100 text-gray-500',
    'blacklisted': 'bg-red-50 text-red-700',
} %}
{{ m.status_badge(vs, vendor_status_map) }}
```

- [ ] **Step 4: Replace raw buttons with button macros**

Replace the Refresh button, Send to Vendors button, and Unavail button with `btn_secondary`, `btn_primary`, and `btn_danger` macros respectively. Each macro accepts an `attrs` dict for HTMX attributes.

- [ ] **Step 5: Replace empty states with shared component**

Replace the inline empty states in `table.html` (line 66-68) and `detail.html` (lines 69-76) with:

```jinja2
{% include "htmx/partials/shared/empty_state.html" %}
```

With appropriate `message` and `action_url`/`action_label` context vars.

- [ ] **Step 6: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`

Expected: All pass. These are template-only changes — the HTML output may differ slightly but functionality should be identical.

- [ ] **Step 7: Commit**

```bash
git add app/templates/htmx/partials/sightings/
git commit -m "refactor: replace hand-rolled sightings UI with shared partials and macros"
```

---

### Task 13: Replace Manual ActivityLog with `log_rfq_activity`

**Files:**
- Modify: `app/routers/sightings.py` — in `sightings_send_inquiry()`

- [ ] **Step 1: Replace manual ActivityLog creation**

In `app/routers/sightings.py`, in the `sightings_send_inquiry` function, replace the ActivityLog loop (lines 485-495):

```python
        for r in requirements:
            for vn in vendor_names:
                log = ActivityLog(
                    user_id=user.id,
                    activity_type="rfq_sent",
                    channel="email",
                    requisition_id=r.requisition_id,
                    requirement_id=r.id,
                    notes=f"RFQ sent to {vn}",
                )
                db.add(log)
```

with:

```python
        for r in requirements:
            for vn in vendor_names:
                log_rfq_activity(
                    db=db,
                    rfq_id=r.requisition_id,
                    activity_type="rfq_sent",
                    description=f"RFQ sent to {vn}",
                    user_id=user.id,
                    requirement_id=r.id,
                )
```

Add import at top: `from ..services.activity_service import log_rfq_activity`

Remove `ActivityLog` from the intelligence model import if no longer directly used.

- [ ] **Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`

Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add app/routers/sightings.py
git commit -m "refactor: use log_rfq_activity helper instead of manual ActivityLog creation"
```

---

### Task 14: Run Full Test Suite

- [ ] **Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --timeout=30 -x`

Expected: All tests pass. No regressions from Plan A changes.

- [ ] **Step 2: Run linting**

Run: `cd /root/availai && ruff check app/services/status_machine.py app/services/sighting_status.py app/services/sighting_aggregation.py app/services/activity_service.py app/routers/sightings.py`

Expected: No errors.

- [ ] **Step 3: Final commit if any lint fixes needed**

```bash
git add -u && git commit -m "chore: lint fixes for Plan A"
```
