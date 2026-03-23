---
name: pytest
description: |
  Writes pytest tests with fixtures, mocking, and async support for the AvailAI FastAPI stack.
  Use when: writing new tests, adding fixtures, mocking external APIs (Anthropic, Graph API,
  connectors), testing FastAPI routes with TestClient, or debugging failing tests.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash
---

# Pytest

AvailAI uses pytest with SQLite in-memory DB, FastAPI `TestClient`, and `TESTING=1` env guard.
All tests share a single schema created once at import; each test function gets row-level cleanup
via FK-safe `DELETE` (not `drop_all`). `asyncio_mode = auto` means async test functions work
without `@pytest.mark.asyncio`.

## Quick Start

### Running Tests

```bash
# Single module (during development)
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_cache_decorator.py -v

# Full suite (before commit)
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v

# Coverage (before PR only — slow)
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

### Minimal Test File

```python
"""tests/test_my_service.py — Tests for app/services/my_service.py."""
from unittest.mock import patch
import pytest
from sqlalchemy.orm import Session

from app.services.my_service import do_thing


def test_do_thing_returns_expected(db_session: Session):
    result = do_thing(db_session, value="lm317t")
    assert result["mpn"] == "LM317T"
```

### Router Test with TestClient

```python
def test_get_vendor_returns_200(client, test_vendor_card):
    resp = client.get(f"/api/vendors/{test_vendor_card.id}")
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Arrow Electronics"
```

## Key Concepts

| Concept | Usage | Example |
|---------|-------|---------|
| `db_session` | Autouse fixture — fresh rows per test | `def test_foo(db_session)` |
| `client` | TestClient with auth + DB overrides | `client.get("/api/...")` |
| `TESTING=1` | Disables scheduler, real API calls | Set in `conftest.py` |
| `asyncio_mode = auto` | Async tests need no decorator | `async def test_foo():` |
| patch at source | Mock where defined, not imported | `patch("app.services.ai_service.claude_json")` |

## Common Patterns

### Test Class Grouping

Group related tests in classes — no `__init__`, no inheritance needed:

```python
class TestVendorScoring:
    def test_high_sighting_count_boosts_score(self, db_session):
        ...

    def test_zero_sightings_returns_baseline(self, db_session):
        ...
```

### Async Service Test

```python
async def test_enrich_contacts_returns_list(db_session):
    with patch("app.services.ai_service.claude_json") as mock:
        mock.return_value = {"contacts": [{"full_name": "Jane"}]}
        result = await enrich_contacts_websearch("Acme", db_session)
    assert len(result) == 1
```

### Error Response Check

```python
def test_missing_vendor_returns_404(client):
    resp = client.get("/api/vendors/99999")
    assert resp.status_code == 404
    assert "error" in resp.json()   # NOT "detail" — see response format standards
```

## See Also

- [fixtures](references/fixtures.md)
- [mocking](references/mocking.md)
- [unit](references/unit.md)
- [integration](references/integration.md)

## Related Skills

- See the **fastapi** skill for route testing patterns
- See the **sqlalchemy** skill for DB fixture and ORM patterns
- See the **playwright** skill for E2E browser tests
