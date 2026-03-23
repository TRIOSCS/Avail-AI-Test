---
name: fastapi
description: |
  Builds FastAPI routes, dependency injection, and middleware for the AvailAI backend.
  Use when: adding new API endpoints, writing dependency functions, handling errors, building middleware, or structuring service layers in app/routers/ or app/services/.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__plugin_context7_context7__resolve-library-id, mcp__plugin_context7_context7__query-docs
---

# FastAPI Skill

AvailAI uses FastAPI with thin routers (HTTP only), a service layer for business logic, and shared dependency functions in `app/dependencies.py`. All errors return `{"error": "...", "status_code": N, "request_id": "..."}` — never `{"detail": "..."}`. Routes serve both JSON APIs and HTMX HTML partials from the same codebase.

## Quick Start

### Adding a New Route

```python
# app/routers/my_feature.py
"""my_feature.py — Brief description.

Called by: main.py (router mount)
Depends on: dependencies, models, database
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..dependencies import require_user
from ..models import User

router = APIRouter(tags=["my_feature"])

@router.get("/api/my-feature/{item_id}")
async def get_item(item_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    item = db.get(MyModel, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    return item.to_dict()
```

### Using Auth Dependencies

```python
from ..dependencies import require_user, require_buyer, require_admin

@router.get("/api/data")
async def get_data(user: User = Depends(require_user), db: Session = Depends(get_db)):
    ...

@router.post("/api/rfq")
async def send_rfq(user: User = Depends(require_buyer), db: Session = Depends(get_db)):
    ...

@router.delete("/api/admin/user/{id}")
async def delete_user(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    ...
```

### HTMX + JSON Dual Response

```python
from ..dependencies import wants_html

@router.get("/api/vendors/{id}")
async def vendor_detail(id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    vendor = db.get(VendorCard, id)
    if not vendor:
        raise HTTPException(404, "Vendor not found")
    if wants_html(request):
        return templates.TemplateResponse("htmx/partials/vendor_detail.html", {"request": request, "vendor": vendor})
    return JSONResponse(vendor.to_dict())
```

## Key Concepts

| Concept | Location | Usage |
|---------|----------|-------|
| Auth dependencies | `app/dependencies.py` | `require_user`, `require_buyer`, `require_admin` |
| DB session | `app/database.py` | `Depends(get_db)` — always inject, never import directly |
| Status enums | `app/constants.py` | `RequisitionStatus.OPEN` — never raw strings |
| Error format | `app/main.py` | `{"error": "msg", "status_code": N, "request_id": "..."}` |
| Caching | `app/cache/decorators.py` | `@cached_endpoint(prefix, ttl_hours, key_params)` |
| Logging | loguru | `from loguru import logger` — never `print()` |

## Common Patterns

### Safe DB Commit (maps IntegrityError to 409)

```python
from ..services.requisition_service import safe_commit
safe_commit(db, entity="vendor")
```

### Standard List Response

```python
return JSONResponse({
    "items": [item.to_dict() for item in results],
    "total": total,
    "limit": limit,
    "offset": offset,
})
```

## See Also

- [routes](references/routes.md)
- [services](references/services.md)
- [database](references/database.md)
- [auth](references/auth.md)
- [errors](references/errors.md)

## Related Skills

- See the **sqlalchemy** skill for ORM query patterns
- See the **alembic** skill for schema migrations
- See the **pytest** skill for testing routes
- See the **htmx** skill for HTMX partial responses
- See the **jinja2** skill for template rendering

## Documentation Resources

Use Context7 for up-to-date FastAPI docs:
1. `mcp__plugin_context7_context7__resolve-library-id` — search "fastapi"
2. Prefer `/websites/` IDs over source repos
3. `mcp__plugin_context7_context7__query-docs` with resolved ID

Recommended queries: "fastapi dependency injection", "fastapi middleware", "fastapi exception handlers"
```

---
