# Unit Test Reference

## Contents
- Test File Structure
- Service Layer Tests
- Constants & Schema Tests
- WARNING: Testing Implementation Details
- WARNING: Raw Strings Instead of StrEnum

## Test File Structure

Every test file needs a header comment and must be self-contained:

```python
"""tests/test_scoring.py — Tests for app/scoring.py.

Called by: pytest
Depends on: app/scoring.py, app/models/
"""
from sqlalchemy.orm import Session

from app.scoring import score_sighting
from app.constants import SightingStatus
```

## Service Layer Tests

Services are the primary target for unit tests — keep routers thin and test business logic here:

```python
from app.services.normalization import strip_packaging_suffixes


def test_strip_packaging_suffix_removes_reel():
    assert strip_packaging_suffixes("LM317T-TR") == "LM317T"


def test_strip_packaging_suffix_no_change_for_clean_mpn():
    assert strip_packaging_suffixes("LM317T") == "LM317T"


def test_strip_packaging_suffix_case_insensitive():
    assert strip_packaging_suffixes("lm317t-tr") == "lm317t"
```

## Constants & Schema Tests

Verify StrEnum values and Pydantic schema shapes — these are load-bearing in routing logic:

```python
from app.constants import RequisitionStatus, RequirementStatus


class TestStatusEnums:
    def test_requisition_open_value(self):
        assert RequisitionStatus.OPEN == "open"

    def test_requirement_found_value(self):
        assert RequirementStatus.FOUND == "found"

    def test_all_statuses_are_strings(self):
        for status in RequisitionStatus:
            assert isinstance(status.value, str)
```

## Scoring Logic Tests

```python
from app.scoring import score_sighting
from datetime import datetime, timezone, timedelta


def test_recent_sighting_scores_higher():
    now = datetime.now(timezone.utc)
    recent = {"last_seen": now, "qty": 1000, "price": 0.50}
    old = {"last_seen": now - timedelta(days=90), "qty": 1000, "price": 0.50}
    assert score_sighting(recent) > score_sighting(old)
```

## WARNING: Testing Implementation Details

**The Problem:**

```python
# BAD — tests private internals, breaks on refactor
def test_internal_cache_dict():
    from app.services.ai_service import _cache
    assert "_last_result" in _cache
```

**Why This Breaks:**
1. Private attributes are implementation details — rename or restructure and the test breaks for no functional reason
2. You end up testing the code's structure, not its behaviour
3. Refactors that preserve correctness will still fail your test suite

**The Fix:**

```python
# GOOD — tests observable behaviour
async def test_intel_returns_cached_on_second_call(db_session):
    with patch("app.services.ai_service.claude_json") as mock:
        mock.return_value = {"summary": "Acme makes widgets"}
        await company_intel("Acme Electronics", db_session)
        await company_intel("Acme Electronics", db_session)
    assert mock.call_count == 1  # second call hit cache
```

## WARNING: Raw Strings Instead of StrEnum

**The Problem:**

```python
# BAD — raw string bypasses enum validation
def test_status_filter(db_session):
    reqs = get_requisitions(db_session, status="open")
```

**The Fix:**

```python
# GOOD — use StrEnum constants from app/constants.py
from app.constants import RequisitionStatus

def test_status_filter(db_session):
    reqs = get_requisitions(db_session, status=RequisitionStatus.OPEN)
```

Raw strings in tests mask bugs where the app uses `RequisitionStatus.OPEN` and a future rename breaks silently.
