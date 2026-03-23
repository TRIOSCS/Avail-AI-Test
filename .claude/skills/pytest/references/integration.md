# Integration Test Reference

## Contents
- Router Tests with TestClient
- List Response Format
- Auth Boundary Tests
- WARNING: Checking "detail" Instead of "error"
- WARNING: Testing Without DB Data

## Router Tests with TestClient

The `client` fixture from `conftest.py` overrides auth and DB simultaneously. Always use it for router tests:

```python
def test_create_requisition(client, test_user):
    resp = client.post("/api/requisitions/", json={
        "name": "REQ-INTEGRATION-001",
        "customer_name": "Acme Electronics",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "REQ-INTEGRATION-001"
    assert data["status"] == "open"
```

## List Response Format

AvailAI list endpoints return `{"items": [...], "total": N, "limit": N, "offset": N}` — NOT a plain array:

```python
def test_companies_list_shape(client):
    resp = client.get("/api/companies/")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)
```

## Auth Boundary Tests

Use `unauthenticated_client` to verify 401 paths aren't accidentally open:

```python
def test_vendor_endpoint_requires_auth(unauthenticated_client):
    resp = unauthenticated_client.get("/api/vendors/")
    assert resp.status_code == 401
```

Use role-specific overrides to test permission levels:

```python
def test_admin_only_endpoint_rejects_buyer(client, test_user, db_session):
    # client fixture returns test_user with role="buyer"
    resp = client.delete("/api/admin/purge-cache")
    assert resp.status_code == 403
```

## Testing Pagination

```python
def test_vendors_pagination(client, db_session):
    from app.models import VendorCard
    from datetime import datetime, timezone

    # Create 5 vendors
    for i in range(5):
        db_session.add(VendorCard(
            normalized_name=f"vendor {i}",
            display_name=f"Vendor {i}",
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    resp = client.get("/api/vendors/?limit=2&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["total"] >= 5
```

## WARNING: Checking "detail" Instead of "error"

**The Problem:**

```python
# BAD — FastAPI default error key is "detail" but AvailAI uses "error"
def test_not_found(client):
    resp = client.get("/api/vendors/99999")
    assert resp.json()["detail"] == "Vendor not found"  # KeyError in production!
```

**Why This Breaks:**
AvailAI error responses follow `{"error": "...", "status_code": N, "request_id": "..."}` — not FastAPI's default `{"detail": "..."}`. Tests checking `["detail"]` pass at the wrong layer or mask a missing error handler.

**The Fix:**

```python
# GOOD
def test_not_found(client):
    resp = client.get("/api/vendors/99999")
    assert resp.status_code == 404
    assert "error" in resp.json()
```

## WARNING: Testing Without DB Data

**The Problem:**

```python
# BAD — assumes data exists from a previous test (order-dependent!)
def test_first_vendor_has_score(client):
    resp = client.get("/api/vendors/1")
    assert resp.status_code == 200
```

**Why This Breaks:**
`db_session` deletes all rows after each test. IDs are not stable. Test ordering via `-n auto` (xdist) makes the failure non-deterministic.

**The Fix:**

```python
# GOOD — create what you need, use the returned ID
def test_vendor_has_score(client, test_vendor_card):
    resp = client.get(f"/api/vendors/{test_vendor_card.id}")
    assert resp.status_code == 200
```

See the **sqlalchemy** skill and [fixtures](fixtures.md) for factory patterns.
