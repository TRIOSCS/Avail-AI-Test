# Services Reference

## Contents
- Service Layer Rules
- Service File Structure
- Common Patterns
- Background Tasks
- Anti-Patterns

## Service Layer Rules

Services live in `app/services/` and contain ALL business logic. Routers are HTTP-only — they validate inputs, call services, and return responses. Services never import from routers.

**Decision rule:** "Could a background job call this?" → it belongs in a service.

## Service File Structure

```python
"""vendor_merge_service.py — Merges duplicate vendor cards.

Called by: routers/vendors_crud.py, jobs/dedup_job.py
Depends on: models (VendorCard, Offer, Sighting), database
"""
from loguru import logger
from sqlalchemy.orm import Session
from ..models import VendorCard

def merge_vendor_cards(db: Session, source_id: int, target_id: int) -> VendorCard | None:
    """Merge source into target, re-parenting all related records."""
    source = db.get(VendorCard, source_id)
    target = db.get(VendorCard, target_id)
    if not source or not target:
        return None
    logger.info("Merging vendor {} into {}", source_id, target_id)
    # ... business logic
    db.delete(source)
    db.commit()
    return target
```

## Input Validation Helpers

These live in `app/services/requisition_service.py` and are reused across services:

```python
from ..services.requisition_service import parse_date_field, parse_positive_int, safe_commit

# Parse and validate an ISO date string — raises HTTP 400 on bad input
target_date = parse_date_field(request_body.target_date, "target_date")

# Parse and validate a positive integer
qty = parse_positive_int(raw_qty, "quantity")

# Commit with IntegrityError -> HTTP 409 mapping
safe_commit(db, entity="requisition")
```

## Vendor and MPN Matching

NEVER inline fuzzy matching. Always use the shared utilities:

```python
# Vendor name deduplication
from ..vendor_utils import fuzzy_score_vendor
score = fuzzy_score_vendor("Arrow Electronics", "Arrow Elec.")  # Returns 0.0–1.0

# MPN normalization (strips packaging suffixes like -T, -ND, REEL)
from ..services.search_worker_base.mpn_normalizer import strip_packaging_suffixes
key = strip_packaging_suffixes("LM358DR2G-T")  # -> "LM358DR2G"
```

## Background Tasks

Use `safe_background_task` for fire-and-forget operations that shouldn't block the response:

```python
from ..utils.async_helpers import safe_background_task
from fastapi import BackgroundTasks

@router.post("/api/vendors/{id}/enrich")
async def enrich_vendor(id: int, background_tasks: BackgroundTasks, user: User = Depends(require_buyer), db: Session = Depends(get_db)):
    vendor = db.get(VendorCard, id)
    if not vendor:
        raise HTTPException(404, "Vendor not found")
    background_tasks.add_task(safe_background_task, _background_enrich_vendor, vendor.id)
    return {"status": "enrichment_queued"}
```

## WARNING: Anti-Patterns

### Importing DB Session Outside Dependency Injection

```python
# BAD — creating a session directly in a service
from ..database import SessionLocal
def some_service_fn():
    db = SessionLocal()
    # ...
    db.close()
```

**Why This Breaks:** No transaction boundary management, no cleanup on exceptions, leaks connections under load.

```python
# GOOD — always accept db as a parameter
def some_service_fn(db: Session, ...):
    ...
```

Services receive `db` from the caller (router or job). Background jobs use their own `with SessionLocal() as db:` context manager.

### Using print() for Logging

```python
# BAD
print(f"Processing vendor {vendor_id}")

# GOOD — structured, queryable, request_id context auto-injected
from loguru import logger
logger.info("Processing vendor {}", vendor_id)
logger.warning("Vendor not found, skipping", extra={"vendor_id": vendor_id})
```

Loguru output appears in `docker compose logs app` with timestamps and level filtering. `print()` does not.
```

---
