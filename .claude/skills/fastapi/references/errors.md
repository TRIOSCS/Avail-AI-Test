# Errors Reference

## Contents
- Error Response Format
- Exception Handlers
- HTTP Status Codes
- Service Layer Error Mapping
- Validation Errors
- Anti-Patterns

## Error Response Format

ALL JSON errors in AvailAI use this envelope — no exceptions:

```python
{
    "error": "Human-readable message",
    "status_code": 404,
    "request_id": "abc-123-def"
}
```

Tests check `response.json()["error"]`, NEVER `response.json()["detail"]`. FastAPI's default `{"detail": "..."}` format is overridden globally in `app/main.py`.

## Exception Handlers

The global handlers in `app/main.py` cover all HTTP exceptions and validation errors automatically:

```python
# app/main.py — already registered, do not re-implement
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    req_id = getattr(request.state, "request_id", "unknown")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "status_code": exc.status_code, "request_id": req_id},
    )
```

To raise an error in a router or service:

```python
from fastapi import HTTPException
raise HTTPException(404, "Vendor not found")
raise HTTPException(400, "Invalid quantity — must be a positive integer")
raise HTTPException(409, "Duplicate record — vendor already exists")
raise HTTPException(403, "Admin access required")
```

## HTTP Status Codes

| Code | When to Use |
|------|-------------|
| 400 | Invalid input (bad format, missing required field) |
| 401 | Not authenticated (no session, expired token) |
| 403 | Authenticated but not authorized |
| 404 | Resource not found |
| 409 | Conflict (IntegrityError, duplicate key) |
| 422 | Pydantic validation failure (automatic) |
| 500 | Unhandled exception (automatic) |

## Service Layer Error Mapping

Services can raise HTTPException directly — FastAPI propagates them correctly:

```python
# app/services/requisition_service.py
def safe_commit(db: Session, *, entity: str = "record") -> None:
    """Commit, mapping IntegrityError to HTTP 409."""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("IntegrityError on {}: {}", entity, exc)
        raise HTTPException(409, f"Duplicate or conflicting {entity}") from exc

def parse_date_field(value: str, field_name: str = "date") -> datetime:
    """Parse ISO date, raising HTTP 400 on failure."""
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Invalid {field_name}: {value!r} — expected ISO 8601") from exc
```

Reuse these helpers instead of writing new try/except blocks.

## Validation Errors

Pydantic schemas provide automatic validation at route boundaries:

```python
# app/schemas/vendors.py
from pydantic import BaseModel, field_validator

class MaterialCardUpdate(BaseModel):
    model_config = {"extra": "allow"}  # All schemas use extra="allow"

    mpn: str | None = None
    quantity: int | None = None

    @field_validator("quantity")
    @classmethod
    def quantity_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("quantity must be positive")
        return v
```

Validation errors automatically return HTTP 422 with the structured error envelope via the global handler.

## Logging Errors

```python
from loguru import logger

# Service-level error (will be logged, not re-raised)
logger.warning("Vendor not found, skipping enrichment (id={})", vendor_id)

# Unexpected error with stack trace
logger.error("Unexpected error in merge service", exc_info=True)

# With structured context
logger.info("RFQ sent", extra={"requisition_id": 123, "vendor_count": 5})
```

## WARNING: Anti-Patterns

### Returning Internal Errors to Clients

```python
# BAD — leaks DB structure and internal paths
except Exception as e:
    return JSONResponse({"error": str(e)}, status_code=500)
# Output: "UNIQUE constraint failed: vendor_cards.mpn_key"
```

**Why This Breaks:** Leaks schema info, file paths, and stack details to potential attackers. Exception messages are for logs, not responses.

```python
# GOOD — log internally, return safe message
except IntegrityError as exc:
    db.rollback()
    logger.warning("IntegrityError creating vendor: {}", exc)
    raise HTTPException(409, "Vendor already exists")
```

### Silent Failures

```python
# BAD — error swallowed, caller gets no signal
try:
    result = call_external_api(vendor_id)
except Exception:
    pass
return {}

# GOOD — log, and either re-raise or return a meaningful error signal
try:
    result = call_external_api(vendor_id)
except httpx.TimeoutException:
    logger.warning("External API timeout for vendor {}", vendor_id)
    raise HTTPException(503, "External API unavailable — try again")
```

### Using `{"detail": ...}` Format

```python
# BAD — tests will break, inconsistent with all other errors
return JSONResponse({"detail": "Not found"}, status_code=404)

# GOOD — always use the raise HTTPException pattern (auto-formatted by global handler)
raise HTTPException(404, "Vendor not found")
```
