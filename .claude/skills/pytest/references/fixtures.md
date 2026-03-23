# Fixtures Reference

## Contents
- Core Fixtures (conftest.py)
- Fixture Dependency Chain
- Creating Custom Fixtures
- autouse Fixtures
- WARNING: Using db_session After commit()
- WARNING: Hardcoding IDs

## Core Fixtures (conftest.py)

All fixtures live in `tests/conftest.py`. Import `engine` from there if needed in sub-modules.

| Fixture | Scope | What It Provides |
|---------|-------|-----------------|
| `db_session` | function, autouse | Fresh SQLite session; all rows deleted after each test |
| `client` | function | TestClient with buyer auth + DB override |
| `unauthenticated_client` | function | TestClient with DB override, no auth |
| `override_client` | function | TestClient with minimal overrides (require_user + get_db only) |
| `test_user` | function | User(role="buyer") persisted in DB |
| `admin_user` | function | User(role="admin") persisted in DB |
| `test_company` | function | Company("Acme Electronics") |
| `test_requisition` | function | Requisition with one Requirement(LM317T x1000) |
| `test_vendor_card` | function | VendorCard("Arrow Electronics") |
| `test_material_card` | function | MaterialCard(LM317T) |

## Fixture Dependency Chain

Fixtures compose by declaring other fixtures as parameters:

```python
# conftest.py — test_requisition depends on db_session + test_user
@pytest.fixture()
def test_requisition(db_session: Session, test_user: User) -> Requisition:
    req = Requisition(
        name="REQ-TEST-001",
        customer_name="Acme Electronics",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req
```

In tests, declare the top-level fixture — pytest resolves the chain automatically:

```python
def test_requisition_has_requirement(test_requisition):
    assert len(test_requisition.requirements) == 1
    assert test_requisition.requirements[0].primary_mpn == "LM317T"
```

## Creating Custom Fixtures

For domain-specific test data not in `conftest.py`, add local fixtures in the test file:

```python
import pytest
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models import MaterialCard


@pytest.fixture()
def stale_material_card(db_session: Session) -> MaterialCard:
    """A material card with no recent sightings — for cache expiry tests."""
    from datetime import timedelta
    mc = MaterialCard(
        normalized_mpn="stale-part",
        display_mpn="STALE-PART",
        manufacturer="Old Corp",
        search_count=0,
        created_at=datetime.now(timezone.utc) - timedelta(days=180),
    )
    db_session.add(mc)
    db_session.commit()
    db_session.refresh(mc)
    return mc
```

## autouse Fixtures

Two `autouse=True` fixtures run around every test:

1. `db_session` — FK-safe row deletion after each test (fast, no schema rebuild)
2. `_reset_ai_gate_state` — clears NC/ICS worker classification caches between tests

To add your own autouse fixture for module-level setup:

```python
@pytest.fixture(autouse=True)
def disable_rate_limiter(monkeypatch):
    """Ensure rate limiter is off — already set in conftest but reinforce here."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    yield
```

## Event Loop Isolation

Each test gets a fresh event loop via the `event_loop` fixture. `nest_asyncio` is applied globally so async code that internally creates event loops (e.g., some APScheduler paths) doesn't crash:

```python
# conftest.py
@pytest.fixture()
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
```

Never create your own event loop in a test — use `async def test_foo()` and let pytest-asyncio handle it.

## WARNING: Using db_session After commit()

**The Problem:**

```python
# BAD — accessing relationship after commit() loads stale data
@pytest.fixture()
def test_requisition(db_session):
    req = Requisition(name="REQ-001", ...)
    db_session.add(req)
    db_session.commit()
    return req  # req.requirements is an unloaded lazy relationship

def test_requirements_count(test_requisition):
    # DetachedInstanceError or stale data if session is closed
    assert len(test_requisition.requirements) == 1
```

**The Fix:**

```python
# GOOD — always refresh after commit to reload relationships
db_session.commit()
db_session.refresh(req)
return req
```

## WARNING: Hardcoding IDs

**The Problem:**

```python
# BAD — assumes auto-increment starts at 1, breaks with xdist or test ordering
def test_vendor_score(client):
    resp = client.get("/api/vendors/1")
    assert resp.status_code == 200
```

**Why This Breaks:**
SQLite in-memory resets per session but row-level deletes don't reset auto-increment counters within a session. With `pytest-xdist` running tests in parallel, IDs are completely unpredictable.

**The Fix:**

```python
# GOOD — use the fixture's actual ID
def test_vendor_score(client, test_vendor_card):
    resp = client.get(f"/api/vendors/{test_vendor_card.id}")
    assert resp.status_code == 200
```
