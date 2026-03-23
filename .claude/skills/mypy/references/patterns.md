# Mypy Patterns Reference

## Contents
- SQLAlchemy 2.0 typing
- FastAPI async dependencies
- Pydantic v2 models
- Common suppressions
- Anti-patterns

---

## SQLAlchemy 2.0 Typing

SQLAlchemy 2.0's `CursorResult` is not generic in older stubs — mypy frequently fails on `.fetchall()` and `.execute()` results.

```python
# GOOD — use typed accessors
vendor: Vendor | None = db.scalar(select(Vendor).where(Vendor.id == vid))
vendors: list[Vendor] = list(db.scalars(select(Vendor)).all())

# GOOD — explicit cast when needed
from sqlalchemy import cast as sa_cast, CursorResult
result: CursorResult[tuple[int, str]] = db.execute(stmt)  # type: ignore[assignment]
```

```python
# DON'T — db.get() returns T | None, mypy needs None check before use
vendor = db.get(Vendor, vendor_id)
vendor.name  # ERROR: "Vendor | None" has no attribute "name"

# DO — guard first
vendor = db.get(Vendor, vendor_id)
if vendor is None:
    raise HTTPException(status_code=404)
vendor.name  # OK
```

**Why this matters:** SQLAlchemy 2.0 stubs are incomplete. Accessing attributes on `None` causes runtime `AttributeError` — mypy's None-check enforcement prevents this class of bug.

---

## FastAPI Async Dependencies

```python
# GOOD — annotate dependency return types explicitly
from sqlalchemy.orm import Session
from app.models.user import User

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def require_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401)
    return user
```

```python
# DON'T — untyped Depends breaks inference downstream
async def my_route(user = Depends(require_user)):  # user is Any
    user.email  # mypy can't validate this

# DO — annotate the parameter
async def my_route(user: User = Depends(require_user)):
    user.email  # mypy validates against User model
```

---

## Pydantic v2 Models

```python
from pydantic import BaseModel, Field
from typing import Annotated

class VendorCreate(BaseModel):
    name: str
    email: str | None = None
    reliability_score: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5

# GOOD — use model_validate for dict input
vendor = VendorCreate.model_validate(request_data)

# GOOD — typed response model
class VendorResponse(BaseModel):
    model_config = {"extra": "allow"}  # matches app/schemas/responses.py pattern
    id: int
    name: str
```

---

## Common Suppressions — With Required Comments

NEVER add `# type: ignore` without an inline explanation. Pre-commit will catch bare suppressions.

```python
# GOOD — explains why suppression is needed
rows = db.execute(stmt).fetchall()  # type: ignore[assignment]  # CursorResult stubs incomplete

# GOOD — third-party library missing stubs
import some_lib  # type: ignore[import-untyped]

# BAD — gives no context, creates tech debt
result = complex_orm_query()  # type: ignore
```

---

## Anti-Patterns

### WARNING: `Any` Proliferation

```python
# BAD — Any spreads like a virus through the codebase
from typing import Any

def process_search_result(result: Any) -> Any:
    return result["price"]
```

**Why This Breaks:** Once `Any` enters a call chain, mypy stops checking all downstream code. A typo in `result["prcie"]` passes silently.

```python
# GOOD — define the shape
from typing import TypedDict

class SearchResult(TypedDict):
    price: float
    quantity: int
    vendor: str

def process_search_result(result: SearchResult) -> float:
    return result["price"]
```

### WARNING: Ignoring `bool` vs `int` Distinction

```python
# BAD — mypy strict mode distinguishes bool from int
count: int = True  # error: Incompatible types in assignment

# GOOD
found: bool = len(sightings) > 0
count: int = len(sightings)
```

### WARNING: Mutable Default in Type Annotations

```python
# BAD — list default is shared across calls AND wrong type annotation
def get_vendors(ids: list[int] = []) -> list[Vendor]: ...

# GOOD
def get_vendors(ids: list[int] | None = None) -> list[Vendor]:
    if ids is None:
        ids = []
```

---

## StrEnum Constants (Project-Specific)

```python
from app.constants import RequisitionStatus, RequirementStatus

# GOOD — mypy validates enum usage
def set_status(req: Requisition, status: RequisitionStatus) -> None:
    req.status = status

# BAD — raw string bypasses both mypy and enum validation
req.status = "open"  # mypy won't catch typo "opne"
```

See the **sqlalchemy** skill for ORM model typing patterns.
See the **fastapi** skill for complete route typing examples.
