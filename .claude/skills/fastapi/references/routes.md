# Routes Reference

## Contents
- Route Structure
- Router Registration
- Query Parameter Handling
- HTMX Detection
- Anti-Patterns

## Route Structure

Every router file follows this structure:

```python
"""router_name.py — One-line description.

Called by: main.py (router mount)
Depends on: dependencies, models, database
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from ..database import get_db
from ..dependencies import require_user
from ..models import User

router = APIRouter(tags=["tag_name"])
```

Mount in `app/main.py` — every new router must be registered there. Missing this = 404 for all routes.

## Router Registration

```python
# app/main.py — add after existing includes
from .routers import my_feature
app.include_router(my_feature.router)
```

Routers with a prefix go in the include call:
```python
app.include_router(my_feature.router, prefix="/api/my-feature")
```

## Query Parameter Handling

Always validate and clamp pagination params — never trust raw user input:

```python
@router.get("/api/materials")
async def list_materials(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = request.query_params.get("q", "").strip().lower()
    try:
        limit = min(int(request.query_params.get("limit", "200")), 1000)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        raise HTTPException(400, "limit and offset must be integers")
```

Use `parse_positive_int()` from `app/services/requisition_service.py` for validated int params.

## HTMX Detection

```python
from ..dependencies import wants_html, is_htmx_boosted

# Detect HTMX partial requests
if wants_html(request):   # HX-Request: true
    return templates.TemplateResponse(...)

# Detect hx-boost navigations (needs full shell)
if is_htmx_boosted(request):
    return full_page_response(...)
```

Use `wants_html()` for routes that serve both JSON APIs and HTMX partials. See the **htmx** skill for HTMX patterns.

## Caching Responses

```python
from ..cache.decorators import cached_endpoint, invalidate_prefix

@router.get("/api/vendors")
async def list_vendors(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    @cached_endpoint(prefix="vendor_list", ttl_hours=24, key_params=["limit", "offset"])
    def _fetch(limit, offset, user, db):
        return db.query(VendorCard).offset(offset).limit(limit).all()

    return _fetch(limit=50, offset=0, user=user, db=db)

# Invalidate on write
invalidate_prefix("vendor_list")
```

## WARNING: Anti-Patterns

### Business Logic in Routes

**The Problem:**

```python
# BAD — business logic in the router
@router.post("/api/vendors/{id}/merge")
async def merge_vendors(id: int, target_id: int, db: Session = Depends(get_db)):
    vendor = db.get(VendorCard, id)
    target = db.get(VendorCard, target_id)
    # 50 lines of merge logic here...
    db.commit()
```

**Why This Breaks:**
1. Cannot unit-test the merge logic without spinning up an HTTP server
2. Logic gets duplicated when a background job needs the same operation
3. Router is now responsible for HTTP AND business rules — impossible to reason about

**The Fix:**

```python
# GOOD — router delegates to service
from ..services.vendor_merge_service import merge_vendor_cards

@router.post("/api/vendors/{id}/merge")
async def merge_vendors(id: int, target_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    result = merge_vendor_cards(db, source_id=id, target_id=target_id)
    if not result:
        raise HTTPException(404, "One or both vendors not found")
    return {"merged_into": target_id}
```

### Raw String Status Values

```python
# BAD — raw string
req.status = "open"

# GOOD — always use StrEnum constants from app/constants.py
from ..constants import RequisitionStatus
req.status = RequisitionStatus.OPEN
```

Raw strings break when the value changes and leave no IDE navigation trail.
```

---
