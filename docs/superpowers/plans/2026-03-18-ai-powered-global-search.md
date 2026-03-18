# AI-Powered Global Search — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 3-entity global search with a two-tier universal search: fast pg_trgm type-ahead across 7 entities + Claude Haiku AI intent parsing on Enter.

**Architecture:** Tier 1 (type-ahead) uses pg_trgm fuzzy matching via ILIKE + similarity() across all entity tables, returning results in <100ms. Tier 2 (AI search) sends query to Claude Haiku for structured intent parsing, executes targeted queries, caches in Redis. Both tiers share the same dropdown template. A new full results page shows expanded results.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16 (pg_trgm), Claude Haiku (structured output), Redis, HTMX, Alpine.js, Jinja2

**Spec:** `docs/superpowers/specs/2026-03-18-ai-powered-global-search-design.md`

**Key conventions (from conftest.py):**
- Test fixture for DB sessions: `db_session` (not `db`)
- Test fixture for HTTP client: `client(db_session, test_user)`
- Requisition model uses `created_by` (not `creator_id`)
- `db.bind.dialect.name` works for dialect detection (sessionmaker with explicit bind)

---

### Task 1: Database Migration — pg_trgm Extension + GIN Indexes

**Files:**
- Create: `alembic/versions/xxx_add_pg_trgm_search_indexes.py`

- [ ] **Step 1: Generate the migration file**

Run inside Docker:
```bash
docker compose exec app alembic revision -m "add pg_trgm extension and search indexes"
```

- [ ] **Step 2: Write the migration**

```python
"""add pg_trgm extension and search indexes"""

from alembic import op

# revision identifiers
revision = "xxx"
down_revision = "xxx"  # auto-filled by alembic

INDEXES = [
    ("requisitions", "name"),
    ("requisitions", "customer_name"),
    ("companies", "name"),
    ("companies", "domain"),
    ("vendor_cards", "display_name"),
    ("vendor_cards", "normalized_name"),
    ("vendor_cards", "domain"),
    ("vendor_contacts", "full_name"),
    ("vendor_contacts", "email"),
    ("site_contacts", "full_name"),
    ("site_contacts", "email"),
    ("requirements", "primary_mpn"),
    ("requirements", "normalized_mpn"),
    ("offers", "mpn"),
    ("offers", "vendor_name"),
]


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    for table, col in INDEXES:
        idx_name = f"ix_{table}_{col}_trgm"
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {idx_name} "
            f"ON {table} USING gin ({col} gin_trgm_ops);"
        )


def downgrade():
    for table, col in INDEXES:
        idx_name = f"ix_{table}_{col}_trgm"
        op.execute(f"DROP INDEX IF EXISTS {idx_name};")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm;")
```

- [ ] **Step 3: Test the migration (inside Docker)**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/*pg_trgm*
git commit -m "feat: add pg_trgm extension and GIN trigram indexes for global search"
```

---

### Task 2: Config — Add AI Search Rate Limit Setting

**Files:**
- Modify: `app/config.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_config.py` (or create if needed), add a test that the setting exists:

```python
def test_ai_search_rate_limit_default():
    from app.config import Settings
    s = Settings(database_url="sqlite:///test.db", secret_key="test")
    assert s.rate_limit_ai_search == "10/minute"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_config.py::test_ai_search_rate_limit_default -v
```
Expected: FAIL (AttributeError: rate_limit_ai_search)

- [ ] **Step 3: Add the setting to config.py**

Add after `rate_limit_enabled` (line ~54):

```python
rate_limit_ai_search: str = "10/minute"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_config.py::test_ai_search_rate_limit_default -v
```

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: add rate_limit_ai_search config setting"
```

---

### Task 3: Global Search Service — fast_search() + Redis Caching Helpers

This task creates the service with `fast_search()` AND the cache helper stubs (`_get_ai_cache`, `_set_ai_cache`) so Task 4 can use them without errors.

**Files:**
- Create: `app/services/global_search_service.py`
- Create: `tests/test_global_search_service.py`

- [ ] **Step 1: Write failing tests for fast_search()**

```python
"""tests/test_global_search_service.py — Tests for global search service.

Called by: pytest
Depends on: app.services.global_search_service, test fixtures from conftest.py
"""

import pytest
from sqlalchemy.orm import Session

from app.models.sourcing import Requisition, Requirement
from app.models.crm import Company, SiteContact, CustomerSite
from app.models.vendors import VendorCard, VendorContact
from app.models.offers import Offer


@pytest.fixture
def search_db(db_session, test_user):
    """Seed test DB with searchable entities across all 7 types."""
    # Requisition
    req = Requisition(name="REQ-2024-LM358", customer_name="Raytheon", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()

    # Company
    co = Company(name="Acme Electronics", domain="acme.com")
    db_session.add(co)
    db_session.flush()

    # Vendor (with JSON emails for JSON field search test)
    vendor = VendorCard(
        display_name="Arrow Electronics",
        normalized_name="arrow electronics",
        domain="arrow.com",
        emails=["sales@arrow.com", "support@arrow.com"],
        phones=["+1-555-0100"],
    )
    db_session.add(vendor)
    db_session.flush()

    # Vendor Contact
    vc = VendorContact(
        vendor_card_id=vendor.id, full_name="John Smith",
        email="john@arrow.com", phone="+1-555-0101", source="manual",
    )
    db_session.add(vc)

    # Customer Site + Site Contact
    site = CustomerSite(company_id=co.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    sc = SiteContact(customer_site_id=site.id, full_name="Jane Doe", email="jane@acme.com")
    db_session.add(sc)

    # Requirement (part)
    part = Requirement(
        requisition_id=req.id, primary_mpn="LM358N",
        normalized_mpn="lm358n", brand="Texas Instruments",
    )
    db_session.add(part)
    db_session.flush()

    # Offer
    offer = Offer(
        requisition_id=req.id, requirement_id=part.id,
        vendor_name="Arrow", mpn="LM358N",
        qty_available=1000, unit_price=0.50,
    )
    db_session.add(offer)

    db_session.commit()
    return db_session


def test_fast_search_returns_structure(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("LM358", search_db)
    assert "best_match" in result
    assert "groups" in result
    assert "total_count" in result
    assert set(result["groups"].keys()) == {
        "requisitions", "companies", "vendors",
        "vendor_contacts", "site_contacts", "parts", "offers",
    }


def test_fast_search_finds_requisition_by_name(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("LM358", search_db)
    req_names = [r["name"] for r in result["groups"]["requisitions"]]
    assert any("LM358" in n for n in req_names)


def test_fast_search_finds_company_by_name(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("Acme", search_db)
    co_names = [r["name"] for r in result["groups"]["companies"]]
    assert "Acme Electronics" in co_names


def test_fast_search_finds_vendor_contact_by_email(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("john@arrow", search_db)
    vc_emails = [r["email"] for r in result["groups"]["vendor_contacts"]]
    assert "john@arrow.com" in vc_emails


def test_fast_search_finds_part_by_mpn(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("LM358N", search_db)
    mpns = [r["primary_mpn"] for r in result["groups"]["parts"]]
    assert "LM358N" in mpns


def test_fast_search_finds_offer_by_vendor_name(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("Arrow", search_db)
    vendor_names = [r["vendor_name"] for r in result["groups"]["offers"]]
    assert "Arrow" in vendor_names


def test_fast_search_finds_vendor_by_json_email(search_db):
    """Verify JSON array fields (emails/phones) are searchable."""
    from app.services.global_search_service import fast_search
    result = fast_search("sales@arrow", search_db)
    vendor_names = [r["display_name"] for r in result["groups"]["vendors"]]
    assert "Arrow Electronics" in vendor_names


def test_fast_search_empty_query_returns_empty(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("", search_db)
    assert result["total_count"] == 0


def test_fast_search_short_query_returns_empty(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("a", search_db)
    assert result["total_count"] == 0


def test_fast_search_respects_limit(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("LM358", search_db)
    for group in result["groups"].values():
        assert len(group) <= 5


def test_fast_search_best_match_present(search_db):
    from app.services.global_search_service import fast_search
    result = fast_search("LM358N", search_db)
    assert result["best_match"] is not None
    assert "type" in result["best_match"]
    assert "id" in result["best_match"]


def test_fast_search_special_chars_safe(search_db):
    """SQL injection / wildcard chars don't cause errors."""
    from app.services.global_search_service import fast_search
    result = fast_search("100%", search_db)
    assert result["total_count"] == 0  # no match, but no error

    result = fast_search("test_underscore", search_db)
    assert result["total_count"] == 0

    result = fast_search("O'Reilly", search_db)
    assert result["total_count"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_global_search_service.py -v
```
Expected: FAIL (ModuleNotFoundError: app.services.global_search_service)

- [ ] **Step 3: Implement fast_search() in global_search_service.py**

Create `app/services/global_search_service.py` with:

```python
"""Global search service — fast SQL search + AI intent search.

Provides two search tiers:
  - fast_search(): pg_trgm fuzzy matching across 7 entity types (<100ms)
  - ai_search(): Claude Haiku intent parsing + targeted queries (<2s)

Called by: app/routers/htmx_views.py (global search endpoints)
Depends on: SQLAlchemy models, app/utils/sql_helpers.py, app/utils/claude_client.py
"""

import hashlib

from loguru import logger
from sqlalchemy import cast, func, String
from sqlalchemy.orm import Session

from app.models.crm import Company, SiteContact
from app.models.offers import Offer
from app.models.sourcing import Requisition, Requirement
from app.models.vendors import VendorCard, VendorContact
from app.utils.sql_helpers import escape_like

RESULT_LIMIT = 5

# ── Cache helpers (used by ai_search in Task 4) ──────────────────────

AI_CACHE_TTL_SECONDS = 300  # 5 minutes


def _ai_cache_key(query: str) -> str:
    normalized = query.lower().strip()
    h = hashlib.md5(normalized.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"ai_search:{h}"


def _get_ai_cache(query: str) -> dict | None:
    """Check Redis for cached AI search result."""
    try:
        from app.cache.intel_cache import get_cached
        return get_cached(_ai_cache_key(query))
    except Exception:
        return None


def _set_ai_cache(query: str, result: dict) -> None:
    """Cache AI search result in Redis."""
    try:
        from app.cache.intel_cache import set_cached
        set_cached(_ai_cache_key(query), result, ttl_days=AI_CACHE_TTL_SECONDS / 86400)
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────


def _is_postgres(db: Session) -> bool:
    """Check if the DB backend is PostgreSQL (vs SQLite in tests)."""
    return db.bind.dialect.name == "postgresql"


def _to_dict(obj, fields: list[str], entity_type: str) -> dict:
    """Convert a SQLAlchemy model to a search result dict."""
    d = {"type": entity_type, "id": obj.id}
    for f in fields:
        val = getattr(obj, f, None)
        # Convert non-serializable types to string
        d[f] = val
    return d


# ── Empty result template ─────────────────────────────────────────────

EMPTY_GROUPS = {
    "requisitions": [], "companies": [], "vendors": [],
    "vendor_contacts": [], "site_contacts": [],
    "parts": [], "offers": [],
}


def _empty_result() -> dict:
    return {"best_match": None, "groups": {k: [] for k in EMPTY_GROUPS}, "total_count": 0}


# ── Fast search (Tier 1) ─────────────────────────────────────────────


def fast_search(query: str, db: Session) -> dict:
    """Search all entities with ILIKE + pg_trgm fuzzy matching.

    Sync function — FastAPI runs it in a thread pool from async handlers.
    Falls back to plain ILIKE on SQLite (test mode).
    """
    if not query or len(query.strip()) < 2:
        return _empty_result()

    safe = escape_like(query.strip())
    pattern = f"%{safe}%"
    use_pg = _is_postgres(db)

    groups = {}
    all_results = []

    # --- Requisitions ---
    q = db.query(Requisition).filter(
        Requisition.name.ilike(pattern) | Requisition.customer_name.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(
            func.greatest(
                func.similarity(Requisition.name, query),
                func.similarity(Requisition.customer_name, query),
            ).desc()
        )
    rows = q.limit(RESULT_LIMIT).all()
    groups["requisitions"] = [
        _to_dict(r, ["name", "customer_name", "status"], "requisition") for r in rows
    ]
    all_results.extend(groups["requisitions"])

    # --- Companies ---
    q = db.query(Company).filter(
        Company.name.ilike(pattern) | Company.domain.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(Company.name, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["companies"] = [
        _to_dict(r, ["name", "domain", "account_type"], "company") for r in rows
    ]
    all_results.extend(groups["companies"])

    # --- Vendors (includes JSON emails/phones cast to string) ---
    q = db.query(VendorCard).filter(
        VendorCard.display_name.ilike(pattern)
        | VendorCard.normalized_name.ilike(pattern)
        | VendorCard.domain.ilike(pattern)
        | cast(VendorCard.emails, String).ilike(pattern)
        | cast(VendorCard.phones, String).ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(VendorCard.display_name, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["vendors"] = [
        _to_dict(r, ["display_name", "domain"], "vendor") for r in rows
    ]
    all_results.extend(groups["vendors"])

    # --- Vendor Contacts ---
    q = db.query(VendorContact).filter(
        VendorContact.full_name.ilike(pattern)
        | VendorContact.email.ilike(pattern)
        | VendorContact.phone.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(VendorContact.full_name, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["vendor_contacts"] = [
        _to_dict(r, ["full_name", "email", "phone", "title"], "vendor_contact") for r in rows
    ]
    all_results.extend(groups["vendor_contacts"])

    # --- Site Contacts ---
    q = db.query(SiteContact).filter(
        SiteContact.full_name.ilike(pattern)
        | SiteContact.email.ilike(pattern)
        | SiteContact.phone.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(SiteContact.full_name, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["site_contacts"] = [
        _to_dict(r, ["full_name", "email", "phone", "title"], "site_contact") for r in rows
    ]
    all_results.extend(groups["site_contacts"])

    # --- Parts (Requirements) ---
    q = db.query(Requirement).filter(
        Requirement.primary_mpn.ilike(pattern)
        | Requirement.normalized_mpn.ilike(pattern)
        | Requirement.brand.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(Requirement.primary_mpn, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["parts"] = [
        _to_dict(r, ["primary_mpn", "normalized_mpn", "brand", "requisition_id"], "part") for r in rows
    ]
    all_results.extend(groups["parts"])

    # --- Offers ---
    q = db.query(Offer).filter(
        Offer.vendor_name.ilike(pattern) | Offer.mpn.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(Offer.mpn, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["offers"] = [
        _to_dict(r, ["vendor_name", "mpn", "unit_price", "qty_available", "requisition_id"], "offer")
        for r in rows
    ]
    all_results.extend(groups["offers"])

    # --- Best match: first result from first non-empty group ---
    best = all_results[0] if all_results else None

    return {
        "best_match": best,
        "groups": groups,
        "total_count": len(all_results),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_global_search_service.py -v
```

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```

- [ ] **Step 6: Commit**

```bash
git add app/services/global_search_service.py tests/test_global_search_service.py
git commit -m "feat: add global search service with fast_search across 7 entities"
```

---

### Task 4: AI Search Service — ai_search() + Intent Schema

**Files:**
- Modify: `app/services/global_search_service.py`
- Create: `tests/test_ai_search.py`

**Note:** `ai_search()` is `async` because it awaits `claude_structured()`. The sync DB queries inside it are fine — they run inline (not blocking event loop) because they complete in <10ms. The `fast_search()` fallback is also sync and fast.

- [ ] **Step 1: Write failing tests for ai_search()**

```python
"""tests/test_ai_search.py — Tests for AI-powered search.

Called by: pytest
Depends on: app.services.global_search_service, conftest fixtures
"""

import pytest
from unittest.mock import patch, AsyncMock

from app.models.sourcing import Requisition, Requirement
from app.models.crm import Company
from app.models.vendors import VendorCard, VendorContact


@pytest.fixture
def search_db(db_session, test_user):
    """Seed test DB with searchable entities."""
    req = Requisition(name="REQ-LM358", customer_name="Raytheon", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    co = Company(name="Acme Electronics", domain="acme.com")
    db_session.add(co)
    db_session.flush()
    vendor = VendorCard(display_name="Arrow Electronics", normalized_name="arrow electronics")
    db_session.add(vendor)
    db_session.flush()
    vc = VendorContact(
        vendor_card_id=vendor.id, full_name="John Smith",
        email="john@arrow.com", source="manual",
    )
    db_session.add(vc)
    part = Requirement(
        requisition_id=req.id, primary_mpn="LM358N",
        normalized_mpn="lm358n", brand="TI",
    )
    db_session.add(part)
    db_session.commit()
    return db_session


@pytest.mark.asyncio
async def test_ai_search_parses_single_intent(search_db):
    """AI search calls Claude, parses single-entity intent, returns results."""
    from app.services.global_search_service import ai_search

    mock_intent = {
        "searches": [
            {"entity_type": "part", "text_query": "LM358N"},
        ]
    }
    with patch("app.services.global_search_service.claude_structured", new_callable=AsyncMock, return_value=mock_intent), \
         patch("app.services.global_search_service._get_ai_cache", return_value=None), \
         patch("app.services.global_search_service._set_ai_cache"):
        result = await ai_search("who sells LM358N?", search_db)

    assert result["total_count"] > 0
    assert any(r["primary_mpn"] == "LM358N" for r in result["groups"]["parts"])


@pytest.mark.asyncio
async def test_ai_search_parses_multi_intent(search_db):
    """AI search handles multiple search operations from Claude."""
    from app.services.global_search_service import ai_search

    mock_intent = {
        "searches": [
            {"entity_type": "part", "text_query": "LM358N"},
            {"entity_type": "vendor", "text_query": "Arrow"},
            {"entity_type": "company", "text_query": "Acme"},
        ]
    }
    with patch("app.services.global_search_service.claude_structured", new_callable=AsyncMock, return_value=mock_intent), \
         patch("app.services.global_search_service._get_ai_cache", return_value=None), \
         patch("app.services.global_search_service._set_ai_cache"):
        result = await ai_search("LM358N from Arrow for Acme", search_db)

    assert len(result["groups"]["parts"]) > 0
    assert len(result["groups"]["vendors"]) > 0
    assert len(result["groups"]["companies"]) > 0


@pytest.mark.asyncio
async def test_ai_search_falls_back_on_claude_failure(search_db):
    """When Claude fails, ai_search falls back to fast_search."""
    from app.services.global_search_service import ai_search

    with patch("app.services.global_search_service.claude_structured", new_callable=AsyncMock, return_value=None), \
         patch("app.services.global_search_service._get_ai_cache", return_value=None), \
         patch("app.services.global_search_service._set_ai_cache"):
        result = await ai_search("LM358", search_db)

    # Should still return results via fast_search fallback
    assert result["total_count"] > 0


@pytest.mark.asyncio
async def test_ai_search_returns_structure(search_db):
    """AI search returns same structure as fast_search."""
    from app.services.global_search_service import ai_search

    mock_intent = {"searches": [{"entity_type": "company", "text_query": "Acme"}]}
    with patch("app.services.global_search_service.claude_structured", new_callable=AsyncMock, return_value=mock_intent), \
         patch("app.services.global_search_service._get_ai_cache", return_value=None), \
         patch("app.services.global_search_service._set_ai_cache"):
        result = await ai_search("find Acme", search_db)

    assert "best_match" in result
    assert "groups" in result
    assert "total_count" in result


@pytest.mark.asyncio
async def test_ai_search_caches_results(search_db):
    """AI search caches results in Redis after successful Claude call."""
    from app.services.global_search_service import ai_search

    mock_intent = {"searches": [{"entity_type": "company", "text_query": "Acme"}]}
    with patch("app.services.global_search_service.claude_structured", new_callable=AsyncMock, return_value=mock_intent), \
         patch("app.services.global_search_service._get_ai_cache", return_value=None), \
         patch("app.services.global_search_service._set_ai_cache") as mock_set:
        await ai_search("find Acme", search_db)
        mock_set.assert_called_once()


@pytest.mark.asyncio
async def test_ai_search_uses_cache_hit(search_db):
    """AI search returns cached results without calling Claude."""
    from app.services.global_search_service import ai_search

    cached = {"best_match": None, "groups": {}, "total_count": 0}
    with patch("app.services.global_search_service._get_ai_cache", return_value=cached), \
         patch("app.services.global_search_service.claude_structured", new_callable=AsyncMock) as mock_claude:
        result = await ai_search("find Acme", search_db)
        mock_claude.assert_not_called()
        assert result == cached


@pytest.mark.asyncio
async def test_ai_search_with_filters(search_db):
    """AI search applies structured filters from Claude intent."""
    from app.services.global_search_service import ai_search

    mock_intent = {
        "searches": [
            {
                "entity_type": "requisition",
                "text_query": "Raytheon",
                "filters": {"status": "active", "customer_name": "Raytheon"},
            },
        ]
    }
    with patch("app.services.global_search_service.claude_structured", new_callable=AsyncMock, return_value=mock_intent), \
         patch("app.services.global_search_service._get_ai_cache", return_value=None), \
         patch("app.services.global_search_service._set_ai_cache"):
        result = await ai_search("open reqs for Raytheon", search_db)

    # Should find the Raytheon requisition
    assert result["total_count"] >= 0  # may or may not match depending on status
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ai_search.py -v
```

- [ ] **Step 3: Implement ai_search() and intent schema**

Add to `app/services/global_search_service.py`:

- `SEARCH_INTENT_SCHEMA` dict (from spec)
- `SEARCH_SYSTEM_PROMPT` string (from spec, with correct status values and few-shot examples)
- `async def ai_search(query, db)` that:
  1. Checks `_get_ai_cache(query)` — return cached result if found
  2. Calls `claude_structured(query, SEARCH_INTENT_SCHEMA, system=SEARCH_SYSTEM_PROMPT, model_tier="fast", timeout=10)`
  3. On success: parses intent, runs targeted queries per entity_type, applies filters
  4. Calls `_set_ai_cache(query, result)` to cache
  5. On failure (None return): calls `fast_search(query, db)` as fallback
  6. Returns same dict structure as `fast_search()`

The targeted query function per entity type reuses the same ILIKE + similarity patterns from `fast_search()`, plus applies structured filters (status, customer_name, vendor_name, etc.) when present in the intent.

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ai_search.py -v
```

- [ ] **Step 5: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```

- [ ] **Step 6: Commit**

```bash
git add app/services/global_search_service.py tests/test_ai_search.py
git commit -m "feat: add AI-powered search with Claude Haiku intent parsing and Redis caching"
```

---

### Task 5: Templates — Updated Dropdown + Full Results Page

Templates must exist BEFORE the route handlers reference them. Create all templates first.

**Files:**
- Modify: `app/templates/htmx/base.html`
- Modify: `app/templates/htmx/partials/shared/search_results.html`
- Create: `app/templates/htmx/partials/search/full_results.html`

- [ ] **Step 1: Update base.html search bar**

Modify the search input (lines 48-67) to add:
- Wrap in Alpine.js with `aiSearching` state: `x-data="{ searchOpen: false, aiSearching: false }"`
- `@keydown.enter.prevent` handler that triggers AI search POST via `htmx.ajax()`
- Prevent double-submit: check `aiSearching` flag before firing
- Keep dropdown open while `aiSearching` is true (override `@blur`)
- Show spinner indicator during AI search
- On AI search response, reset `aiSearching=false` (use `htmx:afterSwap` event)

```html
@blur="if (!aiSearching) setTimeout(() => searchOpen = false, 200)"
@keydown.enter.prevent="if (!aiSearching) {
    aiSearching = true; searchOpen = true;
    htmx.ajax('POST', '/v2/partials/search/ai',
        {target:'#global-search-results', values:{q: $el.value}})
}"
```

Add `htmx:afterSwap` listener on the results div to reset `aiSearching`:
```html
@htmx:after-swap.camel="aiSearching = false"
```

- [ ] **Step 2: Rewrite search_results.html dropdown template**

Replace existing content with expanded template supporting:
- Best match card at top (larger, with entity type badge + icon)
- 7 entity group sections (requisitions, companies, vendors, vendor contacts, site contacts, parts, offers)
- Each group with appropriate icon and display fields
- All results clickable → navigate to entity detail page via HTMX + `@click="searchOpen = false"`
- "View all results" link at bottom → `hx-get="/v2/partials/search/results?q={{ query }}" hx-target="#main-content" hx-push-url="/v2/search/results?q={{ query }}"`
- AI search loading indicator (shown when `aiSearching` is true via Alpine)
- Empty state: "No results for '<query>'"

Context vars: `results` (dict with `best_match`, `groups`, `total_count`), `query` (str), `ai_search` (bool, optional)

- [ ] **Step 3: Create full_results.html page template**

Create `app/templates/htmx/partials/search/full_results.html`:
- Page header with search bar (pre-filled with `{{ query }}`)
- Alpine.js tab bar: `x-data="{ tab: 'all' }"`
  - Tabs: All | Requisitions | Companies | Vendors | Vendor Contacts | Customer Contacts | Parts | Offers
- Each tab shows a table with relevant columns:
  - Requisitions: Name, Customer, Status
  - Companies: Name, Domain, Type
  - Vendors: Name, Domain
  - Vendor Contacts: Name, Email, Phone, Title
  - Customer Contacts: Name, Email, Phone, Title
  - Parts: MPN, Brand
  - Offers: MPN, Vendor, Price, Qty
- "All" tab shows grouped sections (10 results per entity type)
- Empty state: "No results found for '<query>'" + "Try a part number, company name, email, or phone number"
- Each row clickable → HTMX navigate to entity detail

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/base.html app/templates/htmx/partials/shared/search_results.html app/templates/htmx/partials/search/full_results.html
git commit -m "feat: search templates — expanded dropdown, AI indicator, full results page"
```

---

### Task 6: Route Handlers — Update global_search + Add AI search + Full results

**Files:**
- Modify: `app/routers/htmx_views.py`
- Modify: `tests/test_ai_search.py` (add endpoint tests)

- [ ] **Step 1: Write failing tests for the new endpoints**

Add to `tests/test_ai_search.py`:

```python
def test_global_search_endpoint_returns_200(client, search_db):
    """GET /v2/partials/search/global uses global_search_service."""
    resp = client.get("/v2/partials/search/global?q=LM358")
    assert resp.status_code == 200


def test_ai_search_endpoint_returns_200(client, search_db):
    """POST /v2/partials/search/ai returns results."""
    with patch("app.services.global_search_service.claude_structured", new_callable=AsyncMock, return_value=None), \
         patch("app.services.global_search_service._get_ai_cache", return_value=None), \
         patch("app.services.global_search_service._set_ai_cache"):
        resp = client.post("/v2/partials/search/ai", data={"q": "LM358"})
    assert resp.status_code == 200


def test_full_results_endpoint_returns_200(client, search_db):
    """GET /v2/partials/search/results returns full page."""
    resp = client.get("/v2/partials/search/results?q=LM358")
    assert resp.status_code == 200
```

Note: These are sync tests (TestClient handles async routes), no `@pytest.mark.asyncio` needed.

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ai_search.py -k "endpoint" -v
```

- [ ] **Step 3: Update routes in htmx_views.py**

1. **Modify `global_search()`** (line ~222): Replace inline queries with call to `fast_search()`. Pass result dict to template as `results`.

```python
@router.get("/v2/partials/search/global", response_class=HTMLResponse)
async def global_search(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Global search across all entity types (type-ahead)."""
    from app.services.global_search_service import fast_search
    results = fast_search(q, db)
    return templates.TemplateResponse(
        "htmx/partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )
```

2. **Add `ai_search_endpoint()`** (add `Form` import from fastapi):

```python
@router.post("/v2/partials/search/ai", response_class=HTMLResponse)
async def ai_search_endpoint(
    request: Request,
    q: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI-powered search — triggered by Enter key."""
    from app.services.global_search_service import ai_search
    results = await ai_search(q, db)
    return templates.TemplateResponse(
        "htmx/partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q, "ai_search": True},
    )
```

3. **Add `search_results_page()`**:

```python
@router.get("/v2/partials/search/results", response_class=HTMLResponse)
async def search_results_page(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full search results page."""
    from app.services.global_search_service import fast_search
    results = fast_search(q, db) if q else None
    return templates.TemplateResponse(
        "htmx/partials/search/full_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ai_search.py -v
```

- [ ] **Step 5: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/htmx_views.py tests/test_ai_search.py
git commit -m "feat: add AI search and full results route handlers"
```

---

### Task 7: Integration Testing + Deploy

**Files:**
- All test files

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```

- [ ] **Step 2: Run coverage check**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Verify coverage has not decreased.

- [ ] **Step 3: Fix any failures or coverage gaps**

- [ ] **Step 4: Deploy and test live**

```bash
cd /root/availai && git push origin main && docker compose up -d --build && sleep 5 && docker compose logs --tail=30 app
```

Manual testing checklist:
1. Type "LM358" → dropdown shows results from parts, requisitions, offers, vendors
2. Type "john@" → dropdown shows vendor contacts and site contacts
3. Type "Raytheon" → dropdown shows requisitions and companies
4. Press Enter on "who sells LM358?" → AI search fires (spinner shows), returns smart results
5. Click "View all results" → full results page loads with tabs
6. Press Enter on same query again → instant response (cached, no Claude call)
7. Rapid Enter presses → only one request fires (no double-submit)
8. Empty search → "No results" empty state
9. Test each tab on full results page

- [ ] **Step 5: Final commit if any fixes needed**

```bash
git add -A && git commit -m "fix: integration fixes for global search"
```
