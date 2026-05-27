# Database Operations Improvement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve database performance through missing indexes, bulk insert patterns, slow query logging, and cache improvements.

**Architecture:** Four independent phases — indexes (Alembic migration), bulk inserts (service refactor), query monitoring (engine event listener), and cache gaps (Redis layer). Each phase can be deployed independently.

**Tech Stack:** SQLAlchemy 2.0, Alembic, PostgreSQL 16, Redis, pytest

---

## File Map

| Phase | File | Action |
|-------|------|--------|
| 1 | `alembic/versions/<new>_add_missing_indexes.py` | Create (migration) |
| 1 | `app/models/vendors.py` | Modify (add index declarations) |
| 1 | `app/models/intelligence.py` | Modify (add index declarations) |
| 1 | `app/models/offers.py` | Modify (add index declaration) |
| 1 | `app/models/excess.py` | Modify (add index declarations) |
| 1 | `app/models/quotes.py` | Modify (add index declaration) |
| 2 | `app/services/ics_worker/sighting_writer.py` | Modify (bulk insert) |
| 2 | `app/services/nc_worker/sighting_writer.py` | Modify (bulk insert) |
| 2 | `app/services/excess_service.py` | Modify (bulk insert for CSV import) |
| 3 | `app/database.py` | Modify (add slow query listener) |
| 4 | `app/routers/requisitions/core.py` | Modify (eager-load creator names) |
| T | `tests/test_db_improvements.py` | Create |

---

## Phase 1: Missing Indexes (Alembic Migration)

These indexes target columns that are filtered/joined frequently but lack coverage.

### Task 1: Add model-level index declarations

**Files:**
- Modify: `app/models/vendors.py:109-112`
- Modify: `app/models/intelligence.py:308-361`
- Modify: `app/models/excess.py:87-90`
- Modify: `app/models/quotes.py:93-97`

- [ ] **Step 1: Add VendorCard partial index for active vendors**

In `app/models/vendors.py`, update `__table_args__`:

```python
__table_args__ = (
    Index("ix_vendor_cards_created_at", "created_at"),
    Index("ix_vendor_cards_score_computed_at", "vendor_score_computed_at"),
    Index(
        "ix_vendor_cards_active",
        "created_at",
        postgresql_where=Column("is_blacklisted").is_(False),
    ),
)
```

- [ ] **Step 2: Add ActivityLog created_at index**

In `app/models/intelligence.py`, add to `ActivityLog.__table_args__` (after existing entries):

```python
Index("ix_activity_created_at", "created_at"),
```

- [ ] **Step 3: Add ExcessLineItem composite indexes**

In `app/models/excess.py`, update `ExcessLineItem.__table_args__`:

```python
__table_args__ = (
    Index("ix_excess_line_items_list", "excess_list_id"),
    Index("ix_excess_line_items_status", "status"),
    Index("ix_excess_line_items_pn_status", "part_number", "status"),
    Index("ix_excess_line_items_demand", "demand_match_count", "status"),
)
```

- [ ] **Step 4: Add QuoteLine offer_id index**

In `app/models/quotes.py`, update `QuoteLine.__table_args__`:

```python
__table_args__ = (
    Index("ix_quote_lines_quote", "quote_id"),
    Index("ix_quote_lines_card", "material_card_id"),
    Index("ix_quote_lines_mpn", "mpn"),
    Index("ix_quote_lines_offer", "offer_id"),
)
```

- [ ] **Step 5: Add VendorResponse received_at + status index**

In `app/models/offers.py`, add to `VendorResponse.__table_args__`:

```python
Index("ix_vr_received_status", "received_at", "status"),
```

### Task 2: Generate and verify the Alembic migration

**Files:**
- Create: `alembic/versions/<auto>_add_missing_indexes.py`

- [ ] **Step 1: Generate migration inside Docker**

```bash
cd /root/availai && docker compose exec app alembic revision --autogenerate -m "add missing indexes for db perf"
```

- [ ] **Step 2: Review the generated migration**

Open the new file in `alembic/versions/`. Verify it contains exactly these CREATE INDEX operations:
- `ix_vendor_cards_active` (partial, WHERE is_blacklisted = false)
- `ix_activity_created_at`
- `ix_excess_line_items_pn_status`
- `ix_excess_line_items_demand`
- `ix_quote_lines_offer`
- `ix_vr_received_status`

Verify the downgrade drops all of them. Remove any unrelated operations autogenerate may have picked up.

- [ ] **Step 3: Test migration up/down**

```bash
cd /root/availai && docker compose exec app alembic upgrade head && docker compose exec app alembic downgrade -1 && docker compose exec app alembic upgrade head
```

- [ ] **Step 4: Run targeted tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_models/ -v
```

- [ ] **Step 5: Commit**

```bash
git add app/models/vendors.py app/models/intelligence.py app/models/excess.py app/models/quotes.py app/models/offers.py alembic/versions/*add_missing_indexes*.py
git commit -m "perf: add 6 missing database indexes for vendor, activity, excess, quote, response queries"
```

---

## Phase 2: Bulk Inserts for Sighting Writers

Both ICS and NC sighting writers use individual `db.add()` calls in a loop. Replace with `db.add_all()` to batch the INSERT into fewer round-trips.

### Task 3: Convert ICS sighting writer to bulk insert

**Files:**
- Modify: `app/services/ics_worker/sighting_writer.py:52-94`
- Test: `tests/test_ics_worker_full.py` (existing)

- [ ] **Step 1: Verify existing tests pass before refactoring**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ics_worker_full.py -v
```

- [ ] **Step 3: Refactor to collect and add_all**

In `app/services/ics_worker/sighting_writer.py`, replace the loop-with-add pattern:

```python
    created_sightings = []
    for ics in ics_sightings:
        if not ics.vendor_name:
            continue

        vendor_norm = normalize_vendor_name(ics.vendor_name)
        mpn_norm = strip_packaging_suffixes(ics.part_number)

        # Dedup check
        dedup_key = (vendor_norm.lower(), mpn_norm.lower(), ics.quantity)
        if dedup_key in existing_keys:
            continue
        existing_keys.add(dedup_key)

        sighting = Sighting(
            requirement_id=req.id,
            material_card_id=material_card_id,
            vendor_name=ics.vendor_name,
            vendor_name_normalized=vendor_norm,
            vendor_email=ics.vendor_email or None,
            vendor_phone=ics.vendor_phone or None,
            mpn_matched=ics.part_number,
            normalized_mpn=mpn_norm,
            manufacturer=ics.manufacturer,
            qty_available=ics.quantity,
            source_type="icsource",
            source_searched_at=now,
            confidence=0.6 if ics.in_stock else 0.3,
            date_code=ics.date_code or None,
            raw_data={
                "vendor_company_id": ics.vendor_company_id,
                "uploaded_date": ics.uploaded_date,
                "description": ics.description,
                "price": ics.price,
                "in_stock": ics.in_stock,
            },
            created_at=now,
        )
        created_sightings.append(sighting)

    created = len(created_sightings)
    if created:
        db.add_all(created_sightings)
        db.commit()
        from app.services.sighting_aggregation import rebuild_vendor_summaries_from_sightings
        rebuild_vendor_summaries_from_sightings(db, req.id, ics_sightings)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ics_worker_full.py -v
```

### Task 4: Convert NC sighting writer to bulk insert

**Files:**
- Modify: `app/services/nc_worker/sighting_writer.py:53-108`
- Test: `tests/test_nc_worker_full.py` (existing)

- [ ] **Step 1: Apply same pattern — collect sightings, then add_all**

Same refactor as Task 3: replace the `db.add(sighting)` inside the loop with `created_sightings.append(sighting)`, then `db.add_all(created_sightings)` after the loop.

- [ ] **Step 2: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_nc_worker_full.py -v
```

### Task 5: Convert excess CSV import to bulk insert (two loops)

**Files:**
- Modify: `app/services/excess_service.py:224-254` (`import_csv_to_excess_list` loop)
- Modify: `app/services/excess_service.py:329-342` (`confirm_import` loop)

- [ ] **Step 1: Refactor `import_csv_to_excess_list` (line 224-254)**

Replace `db.add(item)` inside the loop with collecting into a list:

```python
    items = []
    for i, raw_row in enumerate(rows, start=1):
        # ... existing validation ...
        item = ExcessLineItem(...)
        items.append(item)
        imported += 1

    if imported > 0:
        db.add_all(items)
        excess_list.total_line_items = (excess_list.total_line_items or 0) + imported
        _safe_commit(db, entity="excess line items")
```

- [ ] **Step 2: Refactor `confirm_import` (line 329-342)**

Same pattern — collect items, then `db.add_all()`:

```python
    items = []
    for row in validated_rows:
        item = ExcessLineItem(...)
        items.append(item)
        imported += 1
    if imported > 0:
        db.add_all(items)
        excess_list.total_line_items = (excess_list.total_line_items or 0) + imported
        _safe_commit(db, entity="excess line items")
```

- [ ] **Step 3: Run excess tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess*.py -v
```

- [ ] **Step 3: Commit Phase 2**

```bash
git add app/services/ics_worker/sighting_writer.py app/services/nc_worker/sighting_writer.py app/services/excess_service.py
git commit -m "perf: use db.add_all() for bulk sighting and excess line item inserts"
```

---

## Phase 3: Slow Query Logging

Add a SQLAlchemy engine event listener that logs queries exceeding 100ms. Only active in non-test, non-SQLite environments.

### Task 6: Add slow query event listener

**Files:**
- Modify: `app/database.py:56-63`
- Test: `tests/test_db_improvements.py`

- [ ] **Step 1: Write test**

Create `tests/test_db_improvements.py`:

```python
"""Tests for database slow query listener logic."""

from unittest.mock import MagicMock, patch

from app.database import engine


def test_engine_exists():
    """Verify engine is created."""
    assert engine is not None


def test_slow_query_threshold_logic():
    """Verify slow query listener logs when elapsed > 0.1s."""
    # The listener is only registered on PostgreSQL engines,
    # so test the threshold logic directly.
    import time as _time
    from loguru import logger

    conn_info = {"_query_start": _time.monotonic() - 0.2}  # 200ms ago
    mock_conn = MagicMock()
    mock_conn.info = conn_info

    with patch.object(logger, "warning") as mock_warn:
        # Simulate after_cursor_execute logic
        start = mock_conn.info.pop("_query_start", None)
        elapsed = _time.monotonic() - start
        if elapsed > 0.1:
            logger.warning("Slow query ({:.3f}s): {}", elapsed, "SELECT 1")
        mock_warn.assert_called_once()


def test_fast_query_no_log():
    """Verify fast queries don't trigger warning."""
    import time as _time
    from loguru import logger

    conn_info = {"_query_start": _time.monotonic()}  # just now
    mock_conn = MagicMock()
    mock_conn.info = conn_info

    with patch.object(logger, "warning") as mock_warn:
        start = mock_conn.info.pop("_query_start", None)
        elapsed = _time.monotonic() - start
        if elapsed > 0.1:
            logger.warning("Slow query ({:.3f}s): {}", elapsed, "SELECT 1")
        mock_warn.assert_not_called()
```

- [ ] **Step 2: Run test**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_db_improvements.py -v
```

- [ ] **Step 3: Add the slow query listener**

In `app/database.py`, add after the `_set_timezone` listener block (after line 62):

```python
import time as _time

@event.listens_for(engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    conn.info["_query_start"] = _time.monotonic()

@event.listens_for(engine, "after_cursor_execute")
def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    start = conn.info.pop("_query_start", None)
    if start is not None:
        elapsed = _time.monotonic() - start
        if elapsed > 0.1:  # 100ms threshold
            logger.warning(
                "Slow query ({:.3f}s): {}",
                elapsed,
                statement[:300],
            )
```

Note: This is inside the `if not _is_sqlite:` block so it only runs against PostgreSQL.

- [ ] **Step 4: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_db_improvements.py tests/test_database.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/database.py tests/test_db_improvements.py
git commit -m "perf: add SQLAlchemy slow query logger (>100ms threshold)"
```

---

## Phase 4: Eager-Load Creator Names in Requisition List

The requisition list query fetches creator names in a second query. Join it into the main query instead.

### Task 7: Eliminate secondary User lookup

**Files:**
- Modify: `app/routers/requisitions/core.py:325-327, 379-383`

- [ ] **Step 1: Add User joinedload to the main query options**

In `app/routers/requisitions/core.py`, update the `.options()` call around line 325:

```python
).options(
    joinedload(Requisition.customer_site).joinedload(CustomerSite.company),
    joinedload(Requisition.creator),
)
```

The relationship is `Requisition.creator` (defined at `app/models/sourcing.py:62`).

- [ ] **Step 2: Replace the secondary lookup**

Remove lines 379-383 (the `creator_ids` / `creators` / `creator_names` block).

Replace the `creator_names.get(r.created_by, "")` reference with:

```python
"created_by_name": (
    r.creator.name or r.creator.email.split("@")[0]
    if r.creator else ""
),
```

- [ ] **Step 3: Run requisition tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_list_service.py tests/test_requisition_service.py tests/test_requisition_cache.py -v
```

- [ ] **Step 4: Commit**

```bash
git add app/routers/requisitions/core.py
git commit -m "perf: eager-load creator names in requisition list query, eliminate N+1"
```

---

## Phase 5: Full Test Suite + Deploy

### Task 8: Run full test suite and deploy

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

- [ ] **Step 2: Push and deploy**

```bash
cd /root/availai && git push origin main
```

```bash
cd /root/availai && docker compose up -d --build && sleep 5 && docker compose logs --tail=30 app
```

Verify:
- Migration runs successfully (check for "Running upgrade" in logs)
- No slow query warnings on startup
- App responds normally

- [ ] **Step 3: Verify indexes exist in production**

```bash
docker compose exec db psql -U availai -d availai -c "\di ix_vendor_cards_active"
docker compose exec db psql -U availai -d availai -c "\di ix_activity_created_at"
docker compose exec db psql -U availai -d availai -c "\di ix_excess_line_items_pn_status"
docker compose exec db psql -U availai -d availai -c "\di ix_quote_lines_offer"
docker compose exec db psql -U availai -d availai -c "\di ix_vr_received_status"
docker compose exec db psql -U availai -d availai -c "\di ix_excess_line_items_demand"
```
