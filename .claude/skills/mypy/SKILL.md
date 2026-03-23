---
name: mypy
description: |
  Enforces mypy strict type checking on Python code in the AvailAI FastAPI + SQLAlchemy stack.
  Use when: fixing mypy errors from pre-commit hooks, adding type annotations to new code,
  resolving CursorResult/Row type issues, typing async FastAPI dependencies, or running
  mypy in CI before commits.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash
---

# Mypy

Mypy runs in strict mode via pre-commit on every commit. The AvailAI codebase uses SQLAlchemy 2.0, FastAPI async routes, and Pydantic v2 — each has specific typing gotchas that cause most failures.

## Quick Start

```bash
# Check entire app
mypy app/

# Check single file
mypy app/routers/vendors.py

# Check with explicit config
mypy --config-file mypy.ini app/
```

## Common Patterns

### FastAPI Route Return Types

```python
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi import APIRouter

router = APIRouter()

@router.get("/vendors", response_model=None)
async def list_vendors(db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse("vendors.html", {"request": request})
```

### SQLAlchemy 2.0 Query Results

```python
from sqlalchemy import select
from sqlalchemy.engine import Row

# CursorResult requires explicit cast — mypy can't infer row type
result = db.execute(select(Vendor).where(Vendor.id == vendor_id))
vendor: Vendor | None = result.scalar_one_or_none()

# For fetchall() results, annotate explicitly
rows: list[Row[tuple[int, str]]] = db.execute(stmt).fetchall()  # type: ignore[assignment]
```

### Optional vs Union

```python
# GOOD — Python 3.10+ syntax, mypy handles both
def get_vendor(vendor_id: int | None = None) -> Vendor | None: ...

# GOOD — older syntax also fine
from typing import Optional
def get_vendor(vendor_id: Optional[int] = None) -> Optional[Vendor]: ...
```

### TypedDict for Template Context

```python
from typing import TypedDict

class VendorContext(TypedDict):
    vendor: Vendor
    request: Request
    total_sightings: int

ctx: VendorContext = {"vendor": vendor, "request": request, "total_sightings": count}
return templates.TemplateResponse("vendor.html", ctx)
```

## Key Concepts

| Issue | Cause | Fix |
|-------|-------|-----|
| `CursorResult` type error | SQLAlchemy raw execute | Use `.scalar_one_or_none()` or cast |
| `bool` not `int` error | `len(list)` used as bool | Use `bool(len(...))` or `if list:` |
| Missing return type | Untyped function | Add `-> ReturnType` annotation |
| `type: ignore` without comment | Bare suppression | Always add reason: `# type: ignore[assignment]` |
| Pydantic model fields | Missing type stubs | Use `model_validate()` not `__init__` directly |

## See Also

- [patterns](references/patterns.md) — SQLAlchemy, FastAPI, Pydantic typing patterns
- [workflows](references/workflows.md) — pre-commit, CI, fixing errors iteratively

## Related Skills

- See the **ruff** skill for linting (runs alongside mypy in pre-commit)
- See the **fastapi** skill for route typing patterns
- See the **sqlalchemy** skill for ORM query result types
- See the **pytest** skill for typing test fixtures
