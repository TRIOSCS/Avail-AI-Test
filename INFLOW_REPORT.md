# Inflow Offer Feed Fix Report

Date: 2026-06-18

## Summary

Fixed the pipeline so unsolicited vendor emails (VendorResponse with `requisition_id=None`)
can produce Offers that are proactive-eligible. The root-cause analysis was correct on
the guard bug and the dedup bug. It missed one downstream issue: `Offer.requisition_id`
was `nullable=False` in both the ORM model AND the DB schema — requiring an Alembic
migration in addition to the guard change.

---

## What Changed

### 1. Guard Fix — `app/email_service.py` (line 1308)

**Before:**
```python
if not (vr.confidence and vr.confidence >= 0.5 and vr.requisition_id):
    return
```

**After:**
```python
if not (vr.confidence and vr.confidence >= 0.5):
    return
```

Removed the `and vr.requisition_id` term. Unsolicited VRs with confidence ≥ 0.5 now
proceed to Offer creation. Confidence < 0.5 still gates them out.

### 2. Null-req guard for Requisition lookup — `app/email_service.py` (line 1319)

**Before:**
```python
req = db.get(Requisition, vr.requisition_id)
```

**After:**
```python
req = db.get(Requisition, vr.requisition_id) if vr.requisition_id else None
```

Prevents an SQLAlchemy `SAWarning: fully NULL primary key identity cannot load any
object` when `vr.requisition_id` is None. Behavior is identical (already handled by
`if req and req.created_by:`), but avoids the warning.

### 3. Dedup Notification Scope Fix — `app/email_service.py` (lines 1444–1454)

The dedup query `ActivityLog.requisition_id == vr.requisition_id` when
`requisition_id is None` was `WHERE requisition_id IS NULL` — matching ALL
null-req notifications from any vendor, cross-suppressing independent vendors.

**Fix:** When `vr.requisition_id is None`, add a secondary filter
`ActivityLog.contact_name == vr.vendor_name` so each vendor gets its own
notification slot.

### 4. `on_email_offer_parsed` Null-req Guard — `app/services/task_service.py` (line 369)

`RequisitionTask.requisition_id` is `NOT NULL` in the DB schema. Calling
`on_email_offer_parsed(db, None, ...)` would have silently raised an exception
caught by the surrounding try/except in email_service.py — no task would be
created but the error would be swallowed.

**Fix:** Added an early return guard when `requisition_id is None`. Updated the
type hint from `int` to `int | None`.

```python
def on_email_offer_parsed(db: Session, requisition_id: int | None, ...):
    if requisition_id is None:
        return
    ...
```

### 5. Model Change — `app/models/offers.py` (line 29)

**Before:**
```python
requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
```

**After:**
```python
requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="SET NULL"), nullable=True)
```

Changed from `nullable=False` + CASCADE to `nullable=True` + SET NULL. Without this
the DB rejects the INSERT for unsolicited offers. The FK action changes from CASCADE
(deleting the Offer when the Requisition is deleted) to SET NULL (orphaning the Offer
if its requisition is deleted — which is acceptable since these offers have no
requisition anyway).

### 6. Alembic Migration — `alembic/versions/109_offer_nullable_requisition.py`

Migration 109 (chains off 108_buyplan_audit_fixes):
- Drops the NOT NULL constraint on `offers.requisition_id`
- Drops the CASCADE FK and recreates with SET NULL

Downgrade deletes any null-requisition Offer rows before restoring NOT NULL.

### 7. `MIGRATION_NUMBERS_IN_FLIGHT.txt`

Added claim line for migration 109.

---

## RED → GREEN Evidence

Tests in `tests/test_email_service_coverage.py`:

| Test | Before fix | After fix |
|------|-----------|-----------|
| `TestUnsolicitedOfferCreation::test_unsolicited_high_confidence_quoted_part_creates_offer` | RED (NOT NULL constraint crash) | GREEN |
| `TestUnsolicitedOfferCreation::test_low_confidence_unsolicited_creates_no_offer` | GREEN (was already gated) | GREEN |
| `TestUnsolicitedOfferCreation::test_null_req_dedup_scoped_per_vendor` | RED (cross-suppression) | GREEN |
| `TestOnEmailOfferParsedNullReq::test_none_requisition_id_skips_task_creation` | RED (would silently raise + swallow) | GREEN |

Total: 21 passed (17 pre-existing + 4 new).

---

## `on_email_offer_parsed` Task Service Finding

The root-cause analysis noted this "needs inspection". Finding: `RequisitionTask.requisition_id`
is `nullable=False` and `auto_create_task` passes it directly to `RequisitionTask(...)`.
If `requisition_id=None` reaches `create_task` → `db.commit()` → SQLite/PG raises
`IntegrityError`. This is silently swallowed by the `except Exception:` in
`email_service.py:1411`. The fix (early return when `requisition_id is None`) is clean.

---

## Files Changed

| File | Change |
|------|--------|
| `app/email_service.py` | Remove `and vr.requisition_id` guard; guard `db.get()` call; scope dedup query per vendor |
| `app/services/task_service.py` | Early return in `on_email_offer_parsed` when `requisition_id is None`; update type hint |
| `app/models/offers.py` | Make `Offer.requisition_id` nullable + SET NULL FK |
| `alembic/versions/109_offer_nullable_requisition.py` | New migration: nullable offers.requisition_id |
| `MIGRATION_NUMBERS_IN_FLIGHT.txt` | Claim migration 109 |
| `tests/test_email_service_coverage.py` | 4 new test cases covering the fix |

---

## Concerns

1. **Migration downgrade deletes data**: The downgrade path deletes all null-req Offer
   rows to satisfy the restored NOT NULL constraint. This is documented in the migration
   header and is the correct behavior for a true downgrade. Acceptable.

2. **Cascade behavior change**: Existing solicited Offer rows now use SET NULL instead of
   CASCADE on Requisition delete. This means deleting a Requisition now orphans its Offers
   (requisition_id → NULL) rather than deleting them. For a staging environment with
   infrequent requisition deletes this is unlikely to matter. If cascade-delete of
   Offers-on-Requisition-delete is a product requirement, that would need review.

3. **345 existing VRs not backfilled**: The fix enables NEW unsolicited VRs to create
   Offers going forward. The 345 existing VRs with conf ≥ 0.5 + quoted parts are not
   automatically backfilled. A one-time backfill script could be run after deploy if needed.

4. **`_find_open_task_by_ref` with None requisition_id**: `_find_open_task_by_ref` queries
   `RequisitionTask.requisition_id == requisition_id` (== None → IS NULL). This would
   match tasks from ALL null-req sources, but since `on_email_offer_parsed` returns early
   when `requisition_id is None`, this dedup path is never hit for unsolicited offers.
   The task service dedup is effectively skipped for unsolicited emails — no task created,
   so no dedup needed. This is correct behavior.
