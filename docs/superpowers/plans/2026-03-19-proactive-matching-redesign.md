# Proactive Part Match Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the proactive matching system with backend cleanup, batch selection UI, full-page prepare/send workflow, and improved sent tab.

**Architecture:** Phase 1 cleans up the backend (remove legacy/sighting matching, fix N+1, persist watermark, add enums, extract helpers, fix dedup). Phase 2 rebuilds the UI with table-based match display and per-group batch selection. Phase 3 adds the full-page prepare/send workflow. Phase 4 improves the sent tab.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, Alembic, HTMX 2.x, Alpine.js 3.x, Tailwind CSS, Jinja2

**Spec:** `docs/superpowers/specs/2026-03-19-proactive-matching-redesign.md`

---

## File Structure

### Modified Files
| File | Responsibility |
|------|---------------|
| `app/models/intelligence.py` | Make `requirement_id`/`requisition_id` nullable, use status enum |
| `app/enums.py` | Add `ProactiveMatchStatus`, `ProactiveOfferStatus` |
| `app/config.py` | Remove `proactive_archive_age_days` |
| `app/services/proactive_matching.py` | Remove sighting matching, fix N+1, persist watermark, fix dedup, fix expire |
| `app/services/proactive_service.py` | Remove legacy engine, remove duplicate email builder, fix scorecard, update `get_matches_for_user` |
| `app/services/proactive_email.py` | No changes (already the single source for email HTML) |
| `app/routers/proactive.py` | Simplify refresh, update send endpoint |
| `app/routers/htmx_views.py` | Add prepare page route, update list partial, fix DNO dedup |
| `app/schemas/proactive.py` | Add `PrepareProactive` schema |
| `app/templates/htmx/partials/proactive/list.html` | Complete rewrite (table layout with Alpine.js per-group state) |

### New Files
| File | Responsibility |
|------|---------------|
| `app/services/proactive_helpers.py` | Shared helpers: `is_do_not_offer()`, `is_throttled()`, `build_batch_dno_set()`, `build_batch_throttle_set()` |
| `app/templates/htmx/partials/proactive/_match_row.html` | Table row partial for a single match |
| `app/templates/htmx/partials/proactive/prepare.html` | Full-page prepare/send workflow |
| `tests/test_proactive_helpers.py` | Tests for shared helpers |
| `tests/test_proactive_matching_v2.py` | Tests for redesigned matching engine |
| `tests/test_proactive_prepare.py` | Tests for prepare/send workflow |
| Alembic migration | Make `requirement_id`/`requisition_id` nullable |

### Deleted Files
| File | Reason |
|------|--------|
| `app/templates/htmx/partials/proactive/_match_card.html` | Replaced by `_match_row.html` |
| `app/templates/htmx/partials/proactive/draft_form.html` | Replaced by prepare page |
| `app/templates/htmx/partials/proactive/send_success.html` | Replaced by inline banner |

---

## Task 1: Add Status Enums

**Files:**
- Modify: `app/enums.py`
- Test: `tests/test_proactive_matching.py` (verify imports work)

- [ ] **Step 1: Add enums to `app/enums.py`**

Append after the last enum class:

```python
class ProactiveMatchStatus(str, enum.Enum):
    new = "new"
    sent = "sent"
    dismissed = "dismissed"
    converted = "converted"
    expired = "expired"


class ProactiveOfferStatus(str, enum.Enum):
    sent = "sent"
    converted = "converted"
```

- [ ] **Step 2: Verify import works**

Run: `cd /root/availai && python -c "from app.enums import ProactiveMatchStatus, ProactiveOfferStatus; print(ProactiveMatchStatus.new == 'new')"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add app/enums.py
git commit -m "feat: add ProactiveMatchStatus and ProactiveOfferStatus enums"
```

---

## Task 2: Extract Shared Helpers

**Files:**
- Create: `app/services/proactive_helpers.py`
- Create: `tests/test_proactive_helpers.py`

- [ ] **Step 1: Write tests for shared helpers**

Create `tests/test_proactive_helpers.py`:

```python
"""Tests for proactive matching shared helpers."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Company, User
from app.models.intelligence import ProactiveDoNotOffer, ProactiveThrottle
from app.models.crm import CustomerSite
from app.services.proactive_helpers import (
    is_do_not_offer,
    is_throttled,
    build_batch_dno_set,
    build_batch_throttle_set,
)
from tests.conftest import engine  # noqa: F401


def _make_company_and_site(db):
    owner = User(email="test@trioscs.com", name="Test", role="sales", azure_id="t-001",
                 created_at=datetime.now(timezone.utc))
    db.add(owner)
    db.flush()
    company = Company(name="Test Co", is_active=True, account_owner_id=owner.id)
    db.add(company)
    db.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()
    return company, site


def test_is_do_not_offer_true(db_session):
    company, _ = _make_company_and_site(db_session)
    db_session.add(ProactiveDoNotOffer(
        mpn="LM358N", company_id=company.id, created_by_id=company.account_owner_id,
    ))
    db_session.commit()
    assert is_do_not_offer(db_session, "LM358N", company.id) is True


def test_is_do_not_offer_false(db_session):
    company, _ = _make_company_and_site(db_session)
    db_session.commit()
    assert is_do_not_offer(db_session, "LM358N", company.id) is False


def test_is_do_not_offer_normalizes_mpn(db_session):
    company, _ = _make_company_and_site(db_session)
    db_session.add(ProactiveDoNotOffer(
        mpn="LM358N", company_id=company.id, created_by_id=company.account_owner_id,
    ))
    db_session.commit()
    assert is_do_not_offer(db_session, "  lm358n  ", company.id) is True


def test_is_throttled_true(db_session):
    _, site = _make_company_and_site(db_session)
    db_session.add(ProactiveThrottle(
        mpn="LM358N", customer_site_id=site.id,
        last_offered_at=datetime.now(timezone.utc) - timedelta(days=5),
    ))
    db_session.commit()
    assert is_throttled(db_session, "LM358N", site.id) is True


def test_is_throttled_expired(db_session):
    _, site = _make_company_and_site(db_session)
    db_session.add(ProactiveThrottle(
        mpn="LM358N", customer_site_id=site.id,
        last_offered_at=datetime.now(timezone.utc) - timedelta(days=30),
    ))
    db_session.commit()
    assert is_throttled(db_session, "LM358N", site.id) is False


def test_build_batch_dno_set(db_session):
    company, _ = _make_company_and_site(db_session)
    db_session.add(ProactiveDoNotOffer(
        mpn="LM358N", company_id=company.id, created_by_id=company.account_owner_id,
    ))
    db_session.commit()
    result = build_batch_dno_set(db_session, "LM358N", {company.id})
    assert company.id in result


def test_build_batch_throttle_set(db_session):
    _, site = _make_company_and_site(db_session)
    db_session.add(ProactiveThrottle(
        mpn="LM358N", customer_site_id=site.id,
        last_offered_at=datetime.now(timezone.utc) - timedelta(days=5),
    ))
    db_session.commit()
    result = build_batch_throttle_set(db_session, "LM358N", {site.id})
    assert site.id in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_helpers.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement shared helpers**

Create `app/services/proactive_helpers.py`:

```python
"""proactive_helpers.py — Shared helpers for proactive matching.

Deduplicates do-not-offer checks, throttle checks, and batch query patterns
used across proactive_matching.py, proactive_service.py, and htmx_views.py.

Called by: services/proactive_matching.py, services/proactive_service.py, routers/htmx_views.py
Depends on: models/intelligence.py, config.py
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models.intelligence import ProactiveDoNotOffer, ProactiveThrottle


def is_do_not_offer(db: Session, mpn: str, company_id: int) -> bool:
    """Check if MPN is permanently suppressed for a company."""
    mpn_upper = mpn.strip().upper()
    return (
        db.query(ProactiveDoNotOffer.id)
        .filter(
            ProactiveDoNotOffer.mpn == mpn_upper,
            ProactiveDoNotOffer.company_id == company_id,
        )
        .first()
        is not None
    )


def is_throttled(db: Session, mpn: str, site_id: int, days: int | None = None) -> bool:
    """Check if MPN was recently offered to a customer site."""
    mpn_upper = mpn.strip().upper()
    throttle_days = days or settings.proactive_throttle_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=throttle_days)
    return (
        db.query(ProactiveThrottle.id)
        .filter(
            ProactiveThrottle.mpn == mpn_upper,
            ProactiveThrottle.customer_site_id == site_id,
            ProactiveThrottle.last_offered_at > cutoff,
        )
        .first()
        is not None
    )


def build_batch_dno_set(db: Session, mpn: str, company_ids: set[int]) -> set[int]:
    """Batch-load do-not-offer company IDs for a given MPN.

    Returns set of company_ids that have this MPN suppressed.
    """
    if not company_ids:
        return set()
    mpn_upper = mpn.strip().upper()
    return {
        row[0]
        for row in db.query(ProactiveDoNotOffer.company_id)
        .filter(
            ProactiveDoNotOffer.mpn == mpn_upper,
            ProactiveDoNotOffer.company_id.in_(company_ids),
        )
        .all()
    }


def build_batch_throttle_set(
    db: Session, mpn: str, site_ids: set[int], days: int | None = None
) -> set[int]:
    """Batch-load throttled site IDs for a given MPN.

    Returns set of customer_site_ids where this MPN was recently offered.
    """
    if not site_ids:
        return set()
    mpn_upper = mpn.strip().upper()
    throttle_days = days or settings.proactive_throttle_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=throttle_days)
    return {
        row[0]
        for row in db.query(ProactiveThrottle.customer_site_id)
        .filter(
            ProactiveThrottle.mpn == mpn_upper,
            ProactiveThrottle.customer_site_id.in_(site_ids),
            ProactiveThrottle.last_offered_at > cutoff,
        )
        .all()
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_helpers.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/proactive_helpers.py tests/test_proactive_helpers.py
git commit -m "feat: extract shared proactive helpers (DNO, throttle, batch queries)"
```

---

## Task 3: Alembic Migration — Nullable FKs on ProactiveMatch

**Files:**
- Modify: `app/models/intelligence.py:127-129`
- Create: Alembic migration

- [ ] **Step 1: Update model to make requirement_id and requisition_id nullable**

In `app/models/intelligence.py`, change lines 128-129:

```python
# Before:
requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False)
requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)

# After:
requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True)
requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="SET NULL"), nullable=True)
```

- [ ] **Step 2: Generate Alembic migration**

Run inside Docker:
```bash
docker compose exec app alembic revision --autogenerate -m "make proactive_match requirement_id requisition_id nullable"
```

- [ ] **Step 3: Review generated migration**

Read the generated file. Verify it contains:
- `op.alter_column('proactive_matches', 'requirement_id', nullable=True)`
- `op.alter_column('proactive_matches', 'requisition_id', nullable=True)`
- Downgrade reverses to `nullable=False`
- No unrelated changes

- [ ] **Step 4: Test migration round-trip**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

- [ ] **Step 5: Commit**

```bash
git add app/models/intelligence.py alembic/versions/*nullable*.py
git commit -m "migration: make proactive_match requirement_id/requisition_id nullable"
```

---

## Task 4: Remove Legacy & Sighting Matching + Persist Watermark

**Files:**
- Modify: `app/services/proactive_matching.py`
- Modify: `app/services/proactive_service.py`
- Modify: `app/routers/proactive.py:42-63`
- Modify: `app/config.py:154`
- Modify: `tests/test_proactive_matching.py`

- [ ] **Step 1: Update tests — remove sighting tests, update scan test to use watermark**

In `tests/test_proactive_matching.py`:
- Remove `find_matches_for_sighting` from imports (line 30)
- Remove `test_find_matches_for_sighting` test (lines 278-310)
- Update `test_run_proactive_scan` to not manipulate module-level `_last_scan_at` — instead the watermark should be read from SystemConfig

- [ ] **Step 2: Remove `find_matches_for_sighting()` from `app/services/proactive_matching.py`**

Delete lines 113-123 (the `find_matches_for_sighting` function).

- [ ] **Step 3: Remove sighting scan from `run_proactive_scan()`**

In `app/services/proactive_matching.py`, remove:
- The sighting query block (lines 296-305)
- The sighting loop (lines 319-325)
- The `Sighting` import from the top
- The module-level `_last_scan_at` global (line 29)

Replace with `SystemConfig`-based watermark using `_get_watermark` / `_set_watermark` helpers:

```python
from ..models.config import SystemConfig

def _get_watermark(db: Session, key: str = "proactive_last_scan") -> datetime:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row and row.value:
        return datetime.fromisoformat(row.value)
    return datetime.now(timezone.utc) - timedelta(hours=settings.proactive_scan_interval_hours)

def _set_watermark(db: Session, ts: datetime, key: str = "proactive_last_scan"):
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row:
        row.value = ts.isoformat()
    else:
        db.add(SystemConfig(key=key, value=ts.isoformat(), description="Proactive scan watermark"))
    db.flush()
```

Update `run_proactive_scan()` to use these instead of the global.

- [ ] **Step 4: Remove legacy engine from `app/services/proactive_service.py`**

Delete:
- `scan_new_offers_for_matches()` function (lines 42-164)
- Module-level `_last_proactive_scan` global (line 36)

- [ ] **Step 5: Simplify refresh endpoint in `app/routers/proactive.py`**

Replace the dual-scan refresh endpoint (lines 42-63) with a single call:

```python
@router.post("/api/proactive/refresh")
async def refresh_proactive_matches(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger a proactive matching scan."""
    from ..services.proactive_matching import run_proactive_scan
    result = run_proactive_scan(db)
    return result
```

- [ ] **Step 6: Remove `proactive_archive_age_days` from `app/config.py`**

Delete line 154: `proactive_archive_age_days: int = 30`

- [ ] **Step 7: Add `.limit(5000)` safety cap to offer scan query in `run_proactive_scan()`**

- [ ] **Step 8: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_matching.py tests/test_proactive_service.py tests/test_routers_proactive.py -v`
Expected: All pass (with sighting test removed)

- [ ] **Step 9: Commit**

```bash
git add app/services/proactive_matching.py app/services/proactive_service.py app/routers/proactive.py app/config.py tests/test_proactive_matching.py
git commit -m "refactor: remove legacy/sighting matching, persist scan watermark in SystemConfig"
```

---

## Task 5: Fix N+1 Queries + Dedup + Expire

**Files:**
- Modify: `app/services/proactive_matching.py` (`_find_matches()`, `expire_old_matches()`)

- [ ] **Step 1: Write test for batch-loaded matching (no N+1)**

Add to `tests/test_proactive_matching.py`:

```python
def test_find_matches_batch_dedup_across_offers(db_session):
    """Two offers for same part+company should not create duplicate matches."""
    data = _setup_scenario(db_session)

    offer1 = Offer(
        material_card_id=data["card"].id, vendor_name="Arrow", mpn="STM32F407",
        unit_price=Decimal("8.00"), status="active",
    )
    offer2 = Offer(
        material_card_id=data["card"].id, vendor_name="DigiKey", mpn="STM32F407",
        unit_price=Decimal("7.50"), status="active",
    )
    db_session.add_all([offer1, offer2])
    db_session.commit()

    matches1 = find_matches_for_offer(offer1.id, db_session)
    db_session.commit()
    assert len(matches1) == 1

    # Second offer for same part+company should be deduped
    matches2 = find_matches_for_offer(offer2.id, db_session)
    db_session.commit()
    assert len(matches2) == 0
```

- [ ] **Step 2: Run test to verify it fails (current dedup uses offer_id)**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_matching.py::test_find_matches_batch_dedup_across_offers -v`
Expected: FAIL (current code creates a second match because offer_id differs)

- [ ] **Step 3: Rewrite `_find_matches()` with batch-loading and tightened dedup**

Replace the inner loop with batch pre-loading per the spec (Section 1b). Key changes:
- Use `build_batch_dno_set()` and `build_batch_throttle_set()` from `proactive_helpers.py`
- Batch-load companies, sites, existing matches, and requisition history
- Tighten dedup to `material_card_id + company_id` only (remove `offer_id` filter)
- Skip companies without `account_owner_id`
- `requirement_id` and `requisition_id` are now optional on the match (nullable from Task 3)
- Remove the fallback requisition query — matches without historical requisitions are valid

- [ ] **Step 4: Replace `expire_old_matches()` with single UPDATE**

```python
def expire_old_matches(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.proactive_match_expiry_days)
    count = (
        db.query(ProactiveMatch)
        .filter(ProactiveMatch.status == "new", ProactiveMatch.created_at < cutoff)
        .update({"status": "expired"}, synchronize_session=False)
    )
    if count:
        db.commit()
    return count
```

- [ ] **Step 5: Run all proactive matching tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_matching.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add app/services/proactive_matching.py tests/test_proactive_matching.py
git commit -m "perf: fix N+1 queries in proactive matching, tighten dedup, single UPDATE expire"
```

---

## Task 6: Remove Duplicate Email Builder + Fix DNO in htmx_views

**Files:**
- Modify: `app/services/proactive_service.py` (remove fallback HTML builder, lines ~405-447)
- Modify: `app/routers/htmx_views.py` (fix DNO creation dedup)

- [ ] **Step 1: Remove fallback HTML email builder from `proactive_service.py`**

In `send_proactive_offer()`, replace the fallback HTML construction (lines ~405-447) with a call to `proactive_email._build_html()`. The `_build_html()` function expects `parts: list[dict]` with keys `mpn`, `manufacturer`, `qty`, `sell_price`, `condition`, `lead_time`. The `line_items` built in `send_proactive_offer` use different keys (`unit_price` instead of `sell_price`), so transform before calling:

```python
from ..services.proactive_email import _build_html

if not email_html:
    greeting_name = contacts[0].full_name.split()[0] if len(contacts) == 1 and contacts[0].full_name else None
    # Transform line_items to the format _build_html expects
    parts_for_html = [
        {
            "mpn": item["mpn"],
            "manufacturer": item.get("manufacturer", ""),
            "qty": item.get("qty", 0),
            "sell_price": item.get("sell_price", item.get("unit_price", 0)),
            "condition": item.get("condition", ""),
            "lead_time": item.get("lead_time", ""),
        }
        for item in line_items
    ]
    body_text = "We have the following parts available that may be of interest based on your previous requirements."
    html_body = _build_html(body_text, greeting_name, parts_for_html, salesperson_name, notes)
```

- [ ] **Step 2: Fix htmx_views DNO creation to check for existing**

In `app/routers/htmx_views.py`, find the `proactive_do_not_offer` POST handler. Add dedup check using the shared helper:

```python
from ..services.proactive_helpers import is_do_not_offer

# Before creating:
if not is_do_not_offer(db, mpn, company_id):
    db.add(ProactiveDoNotOffer(...))
```

- [ ] **Step 3: Run related tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_service.py tests/test_proactive_email.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add app/services/proactive_service.py app/routers/htmx_views.py
git commit -m "refactor: remove duplicate email builder, fix DNO dedup in htmx_views"
```

---

## Task 7: Fix Scorecard — SQL Aggregation

**Files:**
- Modify: `app/services/proactive_service.py` (`get_scorecard()`)

- [ ] **Step 1: Run existing scorecard test to establish baseline**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_service.py -k scorecard -v`

- [ ] **Step 2: Rewrite `get_scorecard()` with SQL aggregation**

Replace the Python-side `query.all()` + list comprehension with SQL `func.count`, `func.sum`, `case`:

```python
from sqlalchemy import func, case

def get_scorecard(db: Session, salesperson_id: int | None = None) -> dict:
    cap = 500_000
    base = db.query(ProactiveOffer)
    if salesperson_id:
        base = base.filter(ProactiveOffer.salesperson_id == salesperson_id)

    # Use case() for capping instead of func.least() — portable across PostgreSQL and SQLite
    def _capped(col):
        return case((col > cap, cap), else_=col)

    stats = base.with_entities(
        func.count(ProactiveOffer.id).label("sent"),
        func.count(case((ProactiveOffer.status == "converted", 1))).label("converted"),
        func.sum(case(
            (ProactiveOffer.status == "converted", _capped(ProactiveOffer.total_sell)),
            else_=0,
        )).label("conv_rev"),
        func.sum(case(
            (ProactiveOffer.status == "converted", _capped(ProactiveOffer.total_cost)),
            else_=0,
        )).label("conv_cost"),
        func.sum(case(
            (ProactiveOffer.status == "sent", _capped(ProactiveOffer.total_sell)),
            else_=0,
        )).label("pending"),
    ).one()

    sent = stats.sent or 0
    converted = stats.converted or 0
    conv_rev = float(stats.conv_rev or 0)
    conv_cost = float(stats.conv_cost or 0)
    pending = float(stats.pending or 0)

    return {
        "total_sent": sent,
        "total_converted": converted,
        "conversion_rate": round(converted / sent * 100, 1) if sent > 0 else 0,
        "anticipated_revenue": round(pending, 2),
        "converted_revenue": round(conv_rev, 2),
        "gross_profit": round(conv_rev - conv_cost, 2),
    }
```

The `_capped()` helper uses portable `case()` syntax that works on both PostgreSQL and SQLite (avoids `func.least` which is PostgreSQL-only).

- [ ] **Step 3: Run scorecard tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_service.py -k scorecard -v`
Expected: Pass

- [ ] **Step 4: Commit**

```bash
git add app/services/proactive_service.py
git commit -m "perf: scorecard uses SQL aggregation instead of Python-side"
```

---

## Task 8: Extend Existing `timesince` Filter for Compact Format

**Files:**
- Modify: `app/routers/htmx_views.py:86-111` (extend existing `_timesince_filter`)

A `_timesince_filter` already exists at `app/routers/htmx_views.py:86-111` and is registered as `timesince`. Rather than creating a duplicate, extend it with an optional `compact` parameter for shorter output ("2h ago" vs "2 hours ago"), or just use the existing filter as-is (its output is already readable). If compact format is needed, add a second filter alongside:

- [ ] **Step 1: Add compact variant alongside existing filter**

In `app/routers/htmx_views.py`, after the existing `_timesince_filter` (line 111), add:

```python
def _timeago_filter(dt):
    """Compact relative time: '2h ago', '3d ago', '2w ago'."""
    if not dt:
        return "--"
    from datetime import datetime, timezone
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            return "--"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    seconds = int((now - dt).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w ago"
    months = days // 30
    return f"{months}mo ago"


templates.env.filters["timeago"] = _timeago_filter
```

- [ ] **Step 2: Commit**

```bash
git add app/routers/htmx_views.py
git commit -m "feat: add compact timeago Jinja filter alongside existing timesince"
```

---

## Task 9: Rewrite Matches List Template (Table + Alpine.js)

**Files:**
- Rewrite: `app/templates/htmx/partials/proactive/list.html`
- Create: `app/templates/htmx/partials/proactive/_match_row.html`
- Delete: `app/templates/htmx/partials/proactive/_match_card.html`
- Modify: `app/routers/htmx_views.py` (update context data for list partial)

- [ ] **Step 1: Update `get_matches_for_user()` to sort groups by opportunity**

In `app/services/proactive_service.py`, before returning groups, sort:

```python
groups_list = sorted(
    groups.values(),
    key=lambda g: sum(m.get("margin_pct") or 0 for m in g["matches"]),
    reverse=True,
)
```

Also sort matches within each group by `match_score` descending:

```python
for g in groups_list:
    g["matches"].sort(key=lambda m: m.get("match_score", 0), reverse=True)
```

- [ ] **Step 2: Create `_match_row.html` table row partial**

Create `app/templates/htmx/partials/proactive/_match_row.html`:

```html
{#
  proactive/_match_row.html — Table row for a single proactive match.
  Receives: match (dict), group (dict with customer_site_id).
  Called by: proactive/list.html loop.
  Depends on: Alpine.js (parent proactiveGroup scope), Tailwind CSS.
#}
<tr id="match-row-{{ match.id }}"
    class="hover:bg-gray-50 transition-colors"
    :class="selected[{{ match.id }}] && 'bg-brand-50'">
  {# Checkbox #}
  <td class="w-10 px-3 py-2.5">
    <input type="checkbox"
           :checked="selected[{{ match.id }}]"
           @change="toggle({{ match.id }})"
           class="rounded border-gray-300 text-brand-500 focus:ring-brand-500">
  </td>
  {# MPN + Manufacturer #}
  <td class="px-3 py-2.5">
    <p class="text-sm font-medium text-gray-900 font-mono">{{ match.mpn | default('--') }}</p>
    {% if match.manufacturer %}
    <p class="text-xs text-gray-400 mt-0.5">{{ match.manufacturer }}</p>
    {% endif %}
    {% if match.customer_purchase_count %}
    <span class="inline-flex items-center gap-0.5 text-[10px] text-gray-400 mt-0.5"
          title="Customer bought {{ match.customer_purchase_count }}x{% if match.customer_last_purchased_at %}, last {{ match.customer_last_purchased_at }}{% endif %}">
      <svg class="h-2.5 w-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
      {{ match.customer_purchase_count }}×
    </span>
    {% endif %}
  </td>
  {# Vendor + reliability #}
  <td class="px-3 py-2.5 text-sm text-gray-700">
    {{ match.vendor_name | default('--') }}
    {% if match.ghost_rate is not none and match.ghost_rate > 30 %}
    <span class="ml-1 text-[10px] text-rose-500 font-medium">unreliable</span>
    {% elif match.vendor_score is not none and match.vendor_score >= 70 %}
    <span class="ml-1 text-[10px] text-emerald-500 font-medium">trusted</span>
    {% endif %}
  </td>
  {# Qty #}
  <td class="px-3 py-2.5 text-sm text-gray-700 text-right tabular-nums">
    {{ "{:,}".format(match.qty_available|int) if match.qty_available else '--' }}
  </td>
  {# Unit Price #}
  <td class="px-3 py-2.5 text-sm text-gray-700 text-right tabular-nums">
    {% if match.unit_price %}${{ "%.4f"|format(match.unit_price) }}{% else %}--{% endif %}
  </td>
  {# Margin pill #}
  <td class="px-3 py-2.5">
    {% if match.margin_pct is not none %}
    <span class="inline-flex px-1.5 py-0.5 text-[10px] font-semibold rounded
      {{ 'bg-emerald-50 text-emerald-700' if match.margin_pct >= 20 else
         'bg-amber-50 text-amber-700' if match.margin_pct >= 10 else
         'bg-rose-50 text-rose-700' }}">
      {{ "%.0f"|format(match.margin_pct) }}%
    </span>
    {% else %}
    <span class="inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded bg-gray-100 text-gray-400">N/A</span>
    {% endif %}
  </td>
  {# Score pill #}
  <td class="px-3 py-2.5">
    {% set score = match.match_score|default(0) %}
    <span class="inline-flex px-1.5 py-0.5 text-[10px] font-bold rounded
      {{ 'bg-emerald-50 text-emerald-700' if score >= 75 else
         'bg-amber-50 text-amber-700' if score >= 50 else
         'bg-gray-100 text-gray-500' }}">
      {{ score }}
    </span>
  </td>
</tr>
```

- [ ] **Step 3: Rewrite `list.html` with table layout and Alpine.js per-group state**

Complete rewrite of `app/templates/htmx/partials/proactive/list.html`. Key elements:
- Sort dropdown above groups
- Each group wrapped in `x-data="proactiveGroup({...})"` with Alpine state
- Group header with collapse toggle, select-all, Prepare/Dismiss buttons
- Table with `_match_row.html` includes
- Prepare button uses `hx-post` with hidden form inputs for match_ids
- Dismiss button uses `hx-post` with match_ids
- Success banner slot for post-send messages
- Empty states for no matches

- [ ] **Step 4: Delete old card template**

Delete `app/templates/htmx/partials/proactive/_match_card.html`

- [ ] **Step 5: Update htmx_views.py list partial to pass required context**

Update the `proactive_list_partial` endpoint to pass:
- `match_count` for the badge
- `sort` parameter support (opportunity/name/count)

- [ ] **Step 6: Manual test in browser**

Visit `/v2/proactive` and verify:
- Matches display in tables grouped by customer
- Checkboxes work (select individual, select all)
- Prepare/Dismiss buttons enable when items selected
- Collapse/expand works
- Sort dropdown changes group order
- Empty state shows when no matches

- [ ] **Step 7: Commit**

```bash
git add app/templates/htmx/partials/proactive/ app/routers/htmx_views.py app/services/proactive_service.py
git commit -m "feat: rewrite proactive matches UI with table layout and batch selection"
```

---

## Task 10: Prepare Page — Route + Template

**Files:**
- Modify: `app/routers/htmx_views.py` (add prepare route)
- Modify: `app/schemas/proactive.py` (add PrepareProactive schema)
- Create: `app/templates/htmx/partials/proactive/prepare.html`
- Delete: `app/templates/htmx/partials/proactive/draft_form.html`

- [ ] **Step 1: Add PrepareProactive schema**

In `app/schemas/proactive.py`, add:

```python
class PrepareProactive(BaseModel):
    match_ids: list[int]
```

- [ ] **Step 2: Add prepare page route in htmx_views.py**

Add `import json` at the top of `htmx_views.py` if not already present. Also ensure `RedirectResponse` is imported from `starlette.responses`. Then add the route:

```python
@router.post("/v2/proactive/prepare/{site_id}", response_class=HTMLResponse)
async def proactive_prepare_page(
    site_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full-page prepare/send workflow for proactive offers."""
    form = await request.form()
    match_ids_raw = form.getlist("match_ids") or form.get("match_ids", "").split(",")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]

    if not match_ids:
        return RedirectResponse("/v2/proactive", status_code=303)

    matches = (
        db.query(ProactiveMatch)
        .filter(ProactiveMatch.id.in_(match_ids), ProactiveMatch.salesperson_id == user.id)
        .options(joinedload(ProactiveMatch.offer))
        .all()
    )
    if not matches:
        return RedirectResponse("/v2/proactive", status_code=303)

    site = db.get(CustomerSite, site_id)
    company = site.company if site else None
    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id)
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )

    match_data = []
    for m in matches:
        offer = m.offer
        match_data.append({
            "id": m.id,
            "mpn": m.mpn,
            "vendor_name": offer.vendor_name if offer else "",
            "manufacturer": offer.manufacturer if offer else "",
            "qty_available": offer.qty_available if offer else 0,
            "unit_price": float(offer.unit_price) if offer and offer.unit_price else None,
            "margin_pct": m.margin_pct,
            "match_score": m.match_score or 0,
        })

    contact_data = [
        {
            "id": c.id,
            "full_name": c.full_name,
            "email": c.email,
            "title": c.title,
            "is_primary": c.is_primary,
            "contact_role": c.contact_role,
            "has_email": bool(c.email),
        }
        for c in contacts
    ]

    ctx = _base_ctx(request, user, "proactive")
    ctx.update({
        "site_id": site_id,
        "company_name": company.name if company else "Customer",
        "site_name": site.site_name if site else "",
        "matches": match_data,
        "match_ids_json": json.dumps([m["id"] for m in match_data]),
        "contacts": contact_data,
    })
    return templates.TemplateResponse("htmx/partials/proactive/prepare.html", ctx)
```

- [ ] **Step 3: Create `prepare.html` template**

Create `app/templates/htmx/partials/proactive/prepare.html` with:
- Back to Matches link
- "Prepare Offer — {company_name} ({site_name})" header
- Selected Parts table (read-only: MPN, Vendor, Qty, Price, Margin)
- Contact picker with checkboxes (primary pre-selected, no-email disabled)
- Email compose section (subject + body textarea)
- "Generate AI Draft" button with HTMX post + loading skeleton
- Cancel + "Send to N Contact(s)" buttons
- Alpine.js state for contact selection and send button label

- [ ] **Step 4: Delete old draft_form.html**

Delete `app/templates/htmx/partials/proactive/draft_form.html`

- [ ] **Step 5: Manual test**

1. Go to `/v2/proactive`, select 2-3 matches in a group
2. Click "Prepare (N)"
3. Verify prepare page loads with correct parts, contacts
4. Verify primary contact is pre-selected
5. Verify "Generate AI Draft" button works (or shows error)
6. Verify "Send" button reflects contact count

- [ ] **Step 6: Commit**

```bash
git add app/routers/htmx_views.py app/schemas/proactive.py app/templates/htmx/partials/proactive/
git commit -m "feat: add full-page prepare/send workflow for proactive offers"
```

---

## Task 11: Send Flow — Backend Updates

**Files:**
- Modify: `app/services/proactive_service.py` (`send_proactive_offer`)
- Modify: `app/routers/htmx_views.py` (add send POST handler)
- Create: `tests/test_proactive_prepare.py`

- [ ] **Step 1: Write test for per-contact email sending**

Create `tests/test_proactive_prepare.py`:

```python
"""Tests for proactive prepare/send workflow."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models import Company, CustomerSite, Offer, ProactiveMatch, SiteContact, User
from app.models.intelligence import ProactiveThrottle
from app.models.purchase_history import CustomerPartHistory
from app.models import MaterialCard
from tests.conftest import engine  # noqa: F401


def _setup_send_scenario(db):
    """Create scenario with matches and contacts ready for sending."""
    owner = User(email="sales@trioscs.com", name="Sales Rep", role="sales",
                 azure_id="s-001", created_at=datetime.now(timezone.utc))
    db.add(owner)
    db.flush()

    company = Company(name="Acme Corp", is_active=True, account_owner_id=owner.id)
    db.add(company)
    db.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()

    contact1 = SiteContact(customer_site_id=site.id, full_name="Jane Doe",
                           email="jane@acme.com", is_primary=True)
    contact2 = SiteContact(customer_site_id=site.id, full_name="Bob Smith",
                           email="bob@acme.com", is_primary=False)
    db.add_all([contact1, contact2])
    db.flush()

    card = MaterialCard(normalized_mpn="lm358n", display_mpn="LM358N")
    db.add(card)
    db.flush()

    offer = Offer(material_card_id=card.id, vendor_name="Arrow", mpn="LM358N",
                  unit_price=Decimal("0.42"), qty_available=5000, status="active")
    db.add(offer)
    db.flush()

    match = ProactiveMatch(
        offer_id=offer.id, customer_site_id=site.id, salesperson_id=owner.id,
        mpn="LM358N", material_card_id=card.id, company_id=company.id,
        match_score=85, margin_pct=23.0, our_cost=0.42, status="new",
    )
    db.add(match)
    db.commit()

    return {
        "owner": owner, "company": company, "site": site,
        "contact1": contact1, "contact2": contact2,
        "card": card, "offer": offer, "match": match,
    }


@pytest.mark.asyncio
async def test_send_creates_throttle_records(db_session):
    """Sending creates throttle records for each MPN+site."""
    data = _setup_send_scenario(db_session)
    from app.services.proactive_service import send_proactive_offer

    with patch("app.services.proactive_service.GraphClient") as MockGC:
        mock_gc = AsyncMock()
        MockGC.return_value = mock_gc

        result = await send_proactive_offer(
            db=db_session, user=data["owner"], token="fake-token",
            match_ids=[data["match"].id],
            contact_ids=[data["contact1"].id],
            sell_prices={}, subject="Test", notes=None,
        )

    assert result is not None
    throttle = db_session.query(ProactiveThrottle).filter(
        ProactiveThrottle.mpn == "LM358N",
        ProactiveThrottle.customer_site_id == data["site"].id,
    ).first()
    assert throttle is not None

    match = db_session.get(ProactiveMatch, data["match"].id)
    assert match.status == "sent"
```

- [ ] **Step 2: Run test**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_prepare.py -v`
Expected: Pass (or adjust based on actual service signature)

- [ ] **Step 3: Add HTMX send handler in htmx_views.py**

Add a POST route that calls `send_proactive_offer` and redirects back to matches list with success banner:

```python
@router.post("/v2/proactive/send", response_class=HTMLResponse)
async def proactive_send_offer(request: Request, user=Depends(require_user), db=Depends(get_db)):
    form = await request.form()
    # Parse match_ids, contact_ids, subject, body from form
    # Call send_proactive_offer
    # On success: redirect with ?success=1&company=...
    # On failure: re-render prepare page with error banner
```

- [ ] **Step 4: Delete `send_success.html`**

Delete `app/templates/htmx/partials/proactive/send_success.html`

- [ ] **Step 5: Run full proactive test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_matching.py tests/test_proactive_service.py tests/test_proactive_prepare.py tests/test_routers_proactive.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add app/routers/htmx_views.py app/services/proactive_service.py app/templates/htmx/partials/proactive/ tests/test_proactive_prepare.py
git commit -m "feat: batch send workflow with per-contact emails and throttle tracking"
```

---

## Task 12: Sent Tab — Group by Customer + Expandable Items + Revenue + Timeago

**Files:**
- Modify: `app/templates/htmx/partials/proactive/list.html` (sent tab section)
- Modify: `app/services/proactive_service.py` (`get_sent_offers` — group by customer)

- [ ] **Step 1: Update `get_sent_offers()` to group by customer**

```python
def get_sent_offers(db: Session, user_id: int) -> list[dict]:
    """Get sent proactive offers grouped by customer."""
    offers = (
        db.query(ProactiveOffer)
        .filter(ProactiveOffer.salesperson_id == user_id)
        .options(joinedload(ProactiveOffer.customer_site).joinedload(CustomerSite.company))
        .order_by(ProactiveOffer.sent_at.desc())
        .all()
    )
    # Group by company
    groups: dict[int, dict] = {}
    for o in offers:
        site = o.customer_site
        company_name = site.company.name if site and site.company else "Unknown"
        company_id = site.company_id if site else 0
        if company_id not in groups:
            groups[company_id] = {
                "company_name": company_name,
                "site_name": site.site_name if site else "",
                "offers": [],
            }
        groups[company_id]["offers"].append(_proactive_offer_to_dict(o))
    return list(groups.values())
```

- [ ] **Step 2: Update sent tab in `list.html`**

Replace the flat table with grouped structure:
- Customer group headers (same style as matches tab)
- Each offer row shows: part count badge (expandable), sent_at (timeago filter), status pill, revenue, convert action
- Expandable line items sub-table using `x-data="{ expanded: false }"`
- Revenue column showing `total_sell`

- [ ] **Step 3: Manual test**

Visit `/v2/proactive?tab=sent` and verify:
- Offers grouped by customer
- Part count expands to show line items
- Timestamps show relative format ("2h ago")
- Revenue column visible
- Convert button works for won offers

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/proactive/list.html app/services/proactive_service.py
git commit -m "feat: sent tab grouped by customer with expandable items and timeago"
```

---

## Task 13: Full Test Suite + Deploy

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```
Expected: All pass, no regressions

- [ ] **Step 2: Run targeted proactive tests with coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_matching.py tests/test_proactive_service.py tests/test_proactive_helpers.py tests/test_proactive_prepare.py tests/test_routers_proactive.py --cov=app/services/proactive_matching --cov=app/services/proactive_service --cov=app/services/proactive_helpers --cov-report=term-missing -v
```

- [ ] **Step 3: Commit any final fixes**

- [ ] **Step 4: Push and deploy**

```bash
cd /root/availai && git push origin main && docker compose up -d --build && sleep 5 && docker compose logs --tail=30 app
```

- [ ] **Step 5: Verify in browser**

1. Navigate to `/v2/proactive` — matches show in table layout
2. Select items, click Prepare — prepare page loads
3. Pick contacts, generate AI draft — email populates
4. Send — success banner, matches move to Sent tab
5. Sent tab — grouped by customer, expandable items, timeago timestamps
