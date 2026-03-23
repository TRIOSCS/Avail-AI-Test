# Database Reference

## Contents
- SQLAlchemy 2.0 Query Style
- N+1 Prevention
- Transaction Handling
- Soft Deletes
- Migration Workflow
- Anti-Patterns

## SQLAlchemy 2.0 Query Style

Always use `db.get()` for primary key lookups — it uses the identity cache and avoids extra queries:

```python
# GOOD — SQLAlchemy 2.0 style
vendor = db.get(VendorCard, vendor_id)
if not vendor:
    raise HTTPException(404, "Vendor not found")

# BAD — deprecated SQLAlchemy 1.x style
vendor = db.query(VendorCard).get(vendor_id)  # Deprecated
```

Use `db.query()` only for filtered queries:
```python
vendors = db.query(VendorCard).filter(VendorCard.deleted_at.is_(None)).order_by(VendorCard.name).all()
```

## N+1 Prevention

Load relationships eagerly when you know you'll access them:

```python
from sqlalchemy.orm import selectinload, joinedload

# selectinload: separate IN query — good for collections
req = db.query(Requisition).options(
    selectinload(Requisition.requirements)
).filter_by(id=req_id).first()

# joinedload: single JOIN — good for single related objects
req = db.query(Requisition).options(
    joinedload(Requisition.created_by_user)
).filter_by(id=req_id).first()
```

Never access `req.requirements` in a loop without eager loading — each access fires a new query.

## Transaction Handling

```python
from sqlalchemy.exc import IntegrityError

# Pattern 1: use safe_commit helper (maps IntegrityError -> HTTP 409)
from ..services.requisition_service import safe_commit
safe_commit(db, entity="vendor")

# Pattern 2: manual rollback on known error paths
try:
    db.add(new_record)
    db.commit()
    db.refresh(new_record)
except IntegrityError:
    db.rollback()
    raise HTTPException(409, "Duplicate record")
```

Always rollback before raising an exception — an uncommitted session in error state causes cascading failures.

## Soft Deletes

Models with soft-delete have a `deleted_at` column. Always filter it out:

```python
# GOOD — exclude soft-deleted records
materials = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None)).all()

# Soft delete — never hard delete unless explicitly required
card.deleted_at = datetime.now(timezone.utc)
db.commit()
```

## Status Values

ALWAYS use StrEnum constants from `app/constants.py` — never raw strings:

```python
from ..constants import RequisitionStatus, RequirementStatus

# GOOD
req.status = RequisitionStatus.OPEN
requirement.status = RequirementStatus.FOUND

# BAD — raw strings break silently when enum values change
req.status = "open"
```

## Migration Workflow

See the **alembic** skill for full details. Quick reference:

```bash
# 1. Edit app/models/
# 2. Generate migration
alembic revision --autogenerate -m "add_field_to_vendor"
# 3. Review the generated file in alembic/versions/
# 4. Test round-trip
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

NEVER use `Base.metadata.create_all()` or raw DDL outside a migration.

## WARNING: Anti-Patterns

### N+1 in Serialization

```python
# BAD — fires one query per vendor
vendors = db.query(VendorCard).all()
return [{"id": v.id, "contact_count": len(v.contacts)} for v in vendors]  # N+1!

# GOOD — use subquery or eager load
from sqlalchemy import func
vendors = db.query(VendorCard, func.count(Contact.id)).outerjoin(Contact).group_by(VendorCard.id).all()
```

### Using UTC Datetime Incorrectly

```python
# BAD — naive datetime, no timezone
from datetime import datetime
record.created_at = datetime.now()

# GOOD — always UTC-aware
from datetime import datetime, timezone
record.created_at = datetime.now(timezone.utc)
```

The DB uses `UTCDateTime` type from `app/database.py`. Naive datetimes cause comparison bugs across DST boundaries.
```

---
