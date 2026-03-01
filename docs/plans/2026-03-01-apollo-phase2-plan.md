# Apollo Phase 2 — Full Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire Apollo.io into AvailAI for contact discovery, enrichment, and sequence enrollment with a review-before-import UX.

**Architecture:** New router (`apollo_sync.py`) + service (`apollo_sync_service.py`) + schemas (`apollo.py`). All Apollo API calls go through the existing `app/connectors/apollo_client.py` or `app/http_client.py`. Frontend adds an "Apollo" tab to the company drawer in `crm.js`. No new DB tables — enrichment data stored in existing `vendor_contacts` + JSON columns.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, httpx (async), Pydantic v2, pytest + TestClient

**Credits budget:** 95 lead credits, 160 direct dial credits. Each people/match = 1 lead credit. Always show credit cost before actions.

**Security note:** Frontend uses the project's existing `esc()` HTML-escape helper for all dynamic content rendered to DOM. This matches the pattern used throughout `app.js` (11K lines) and `crm.js`.

---

## Task 1: Pydantic Schemas for Apollo Endpoints

**Files:**
- Create: `app/schemas/apollo.py`
- Test: `tests/test_schemas_apollo.py`

**Step 1: Write the failing test**

Create `tests/test_schemas_apollo.py`:

```python
"""Tests for Apollo sync schemas.

Validates request/response models for Apollo integration endpoints.
Called by: pytest
Depends on: app.schemas.apollo
"""

from app.schemas.apollo import (
    ApolloCreditsResponse,
    ApolloDiscoverRequest,
    ApolloDiscoverResponse,
    ApolloEnrichRequest,
    ApolloEnrichResponse,
    ApolloSyncResponse,
    DiscoveredContact,
)


def test_discover_request_defaults():
    req = ApolloDiscoverRequest(domain="example.com")
    assert req.domain == "example.com"
    assert req.max_results == 10
    assert len(req.title_keywords) > 0  # has defaults


def test_discovered_contact_model():
    c = DiscoveredContact(
        apollo_id="abc123",
        full_name="Jane Doe",
        title="VP Procurement",
        seniority="decision_maker",
    )
    assert c.apollo_id == "abc123"
    assert c.email_masked is None  # optional


def test_sync_response():
    r = ApolloSyncResponse(synced=5, skipped=2, errors=0)
    assert r.synced == 5


def test_enrich_response_credits():
    r = ApolloEnrichResponse(
        enriched=3, verified=2, credits_used=3, credits_remaining=92
    )
    assert r.credits_remaining == 92


def test_credits_response():
    r = ApolloCreditsResponse(
        lead_credits_remaining=95,
        lead_credits_used=0,
        direct_dial_remaining=160,
        direct_dial_used=0,
    )
    assert r.lead_credits_remaining == 95
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_schemas_apollo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas.apollo'`

**Step 3: Write the schemas**

Create `app/schemas/apollo.py`:

```python
"""Apollo sync request/response schemas.

Pydantic models for Apollo integration endpoints: discover, enrich,
sync, enroll, and credit tracking.

Called by: app/routers/apollo_sync.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# -- Discovery --

class ApolloDiscoverRequest(BaseModel):
    domain: str = Field(..., description="Company domain to search")
    title_keywords: list[str] = Field(
        default=[
            "procurement", "purchasing", "buyer", "supply chain",
            "component engineer", "commodity manager", "sourcing",
        ],
        description="Job title keywords to filter by",
    )
    max_results: int = Field(default=10, ge=1, le=25)


class DiscoveredContact(BaseModel, extra="allow"):
    apollo_id: str | None = None
    full_name: str
    title: str | None = None
    seniority: str | None = None
    email_masked: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None


class ApolloDiscoverResponse(BaseModel, extra="allow"):
    domain: str
    contacts: list[DiscoveredContact] = []
    total_found: int = 0
    note: str | None = None


# -- Enrichment --

class ApolloEnrichRequest(BaseModel):
    apollo_ids: list[str] = Field(..., min_length=1, max_length=25)
    vendor_card_id: int = Field(..., description="AvailAI vendor card to attach contacts to")


class EnrichedContact(BaseModel, extra="allow"):
    apollo_id: str | None = None
    full_name: str
    title: str | None = None
    email: str | None = None
    email_status: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    seniority: str | None = None
    is_verified: bool = False


class ApolloEnrichResponse(BaseModel, extra="allow"):
    enriched: int = 0
    verified: int = 0
    credits_used: int = 0
    credits_remaining: int = 0
    contacts: list[EnrichedContact] = []


# -- Sync --

class ApolloSyncResponse(BaseModel, extra="allow"):
    synced: int = 0
    skipped: int = 0
    errors: int = 0


# -- Sequence Enrollment --

class ApolloEnrollRequest(BaseModel):
    sequence_id: str = Field(..., description="Apollo sequence ID")
    contact_ids: list[str] = Field(..., min_length=1, description="Apollo contact IDs")
    email_account_id: str = Field(..., description="Apollo email account ID for sending")


class ApolloEnrollResponse(BaseModel, extra="allow"):
    enrolled: int = 0
    skipped_no_email: int = 0
    errors: int = 0


# -- Credits --

class ApolloCreditsResponse(BaseModel, extra="allow"):
    lead_credits_remaining: int = 0
    lead_credits_used: int = 0
    direct_dial_remaining: int = 0
    direct_dial_used: int = 0
    ai_credits_remaining: int = 0
    ai_credits_used: int = 0
```

**Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_schemas_apollo.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add app/schemas/apollo.py tests/test_schemas_apollo.py
git commit -m "feat: add Apollo sync Pydantic schemas"
```

---

## Task 2: Apollo Sync Service — Discovery + Credits

**Files:**
- Create: `app/services/apollo_sync_service.py`
- Test: `tests/test_apollo_sync_service.py`

**Step 1: Write the failing test**

Create `tests/test_apollo_sync_service.py`:

```python
"""Tests for Apollo sync service.

Tests discovery, enrichment, sync, and enrollment logic with mocked API calls.
Called by: pytest
Depends on: app.services.apollo_sync_service, app.connectors.apollo_client
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.apollo_sync_service import (
    discover_contacts,
    get_credits,
)


@pytest.mark.asyncio
async def test_discover_contacts_returns_masked_emails():
    """Discovery should return contacts with masked emails (not full emails)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "people": [
            {
                "id": "abc123",
                "first_name": "Jane",
                "last_name": "Doe",
                "title": "VP Procurement",
                "email": "jane.doe@acme.com",
                "linkedin_url": "https://linkedin.com/in/janedoe",
                "seniority": "vp",
                "organization": {"name": "Acme Corp"},
            }
        ],
        "pagination": {"total_entries": 1},
    }

    with patch("app.services.apollo_sync_service.http.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await discover_contacts("acme.com", title_keywords=["procurement"], max_results=10)

    assert result["total_found"] == 1
    assert len(result["contacts"]) == 1
    c = result["contacts"][0]
    assert c["apollo_id"] == "abc123"
    assert c["full_name"] == "Jane Doe"
    assert "jane.doe@acme.com" not in str(c)
    assert c["email_masked"] == "j***@acme..."


@pytest.mark.asyncio
async def test_discover_contacts_no_api_key():
    """Discovery should return empty when no API key is configured."""
    with patch("app.services.apollo_sync_service.settings") as mock_settings:
        mock_settings.apollo_api_key = ""
        result = await discover_contacts("acme.com")

    assert result["total_found"] == 0
    assert result["contacts"] == []


@pytest.mark.asyncio
async def test_discover_contacts_api_error():
    """Discovery should return empty on API error, not raise."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch("app.services.apollo_sync_service.http.post", new_callable=AsyncMock, return_value=mock_resp):
        with patch("app.services.apollo_sync_service.settings") as mock_settings:
            mock_settings.apollo_api_key = "test-key"
            result = await discover_contacts("acme.com")

    assert result["total_found"] == 0


@pytest.mark.asyncio
async def test_get_credits():
    """Credits endpoint should parse Apollo profile response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "user1",
        "effective_num_lead_credits": 95,
        "num_lead_credits_used": 5,
        "effective_num_direct_dial_credits": 160,
        "num_direct_dial_credits_used": 10,
        "effective_num_ai_credits": 5000,
        "num_ai_credits_used": 0,
    }

    with patch("app.services.apollo_sync_service.http.get", new_callable=AsyncMock, return_value=mock_resp):
        with patch("app.services.apollo_sync_service.settings") as mock_settings:
            mock_settings.apollo_api_key = "test-key"
            result = await get_credits()

    assert result["lead_credits_remaining"] == 90
    assert result["direct_dial_remaining"] == 150
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_apollo_sync_service.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the service**

Create `app/services/apollo_sync_service.py`:

```python
"""Apollo sync service -- discovery, enrichment, sync, and enrollment.

Orchestrates Apollo API calls for the /api/apollo/* endpoints.
Masks emails during discovery (revealed only after enrichment).

Called by: app/routers/apollo_sync.py
Depends on: app/http_client.py, app/config.py, app/services/prospect_contacts.py
"""

from loguru import logger

from app.config import settings
from app.http_client import http
from app.services.prospect_contacts import classify_contact_seniority, mask_email

APOLLO_BASE = "https://api.apollo.io/api/v1"


def _get_api_key() -> str:
    return getattr(settings, "apollo_api_key", "")


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-Api-Key": _get_api_key(),
    }


async def discover_contacts(
    domain: str,
    title_keywords: list[str] | None = None,
    max_results: int = 10,
) -> dict:
    """Search Apollo for contacts at a domain. Returns masked preview (no raw emails).

    Returns: {domain, contacts: [{apollo_id, full_name, title, seniority, email_masked, ...}], total_found}
    """
    api_key = _get_api_key()
    if not api_key:
        return {"domain": domain, "contacts": [], "total_found": 0, "note": "Apollo API key not configured"}

    titles = title_keywords or [
        "procurement", "purchasing", "buyer", "supply chain",
        "component engineer", "commodity manager", "sourcing",
    ]

    payload = {
        "q_organization_domains": domain,
        "person_titles": titles,
        "per_page": min(max_results, 25),
        "page": 1,
    }

    try:
        resp = await http.post(
            f"{APOLLO_BASE}/mixed_people/api_search",
            json=payload,
            headers=_headers(),
            timeout=30,
        )

        if resp.status_code != 200:
            logger.warning("Apollo discover failed for {}: {} {}", domain, resp.status_code, resp.text[:200])
            return {"domain": domain, "contacts": [], "total_found": 0, "note": f"API error: {resp.status_code}"}

        data = resp.json()
        people = data.get("people", [])
        total = data.get("pagination", {}).get("total_entries", len(people))

        contacts = []
        for p in people:
            first = (p.get("first_name") or "").strip()
            last = (p.get("last_name") or "").strip()
            full_name = f"{first} {last}".strip() if first or last else p.get("name", "Unknown")
            email = p.get("email") or ""
            title = p.get("title") or p.get("headline") or ""
            org = p.get("organization") or {}

            contacts.append({
                "apollo_id": p.get("id"),
                "full_name": full_name,
                "title": title,
                "seniority": classify_contact_seniority(title),
                "email_masked": mask_email(email) if email else None,
                "linkedin_url": p.get("linkedin_url"),
                "company_name": org.get("name"),
            })

        return {"domain": domain, "contacts": contacts, "total_found": total}

    except Exception as e:
        logger.error("Apollo discover error for {}: {}", domain, e)
        return {"domain": domain, "contacts": [], "total_found": 0, "note": str(e)}


async def get_credits() -> dict:
    """Fetch current Apollo credit usage from profile endpoint."""
    api_key = _get_api_key()
    if not api_key:
        return {
            "lead_credits_remaining": 0, "lead_credits_used": 0,
            "direct_dial_remaining": 0, "direct_dial_used": 0,
            "ai_credits_remaining": 0, "ai_credits_used": 0,
            "note": "Apollo API key not configured",
        }

    try:
        resp = await http.get(
            f"{APOLLO_BASE}/users/api_profile",
            params={"include_credit_usage": "true"},
            headers=_headers(),
            timeout=15,
        )

        if resp.status_code != 200:
            logger.warning("Apollo credits fetch failed: {} {}", resp.status_code, resp.text[:200])
            return {"lead_credits_remaining": 0, "lead_credits_used": 0,
                    "direct_dial_remaining": 0, "direct_dial_used": 0,
                    "ai_credits_remaining": 0, "ai_credits_used": 0,
                    "note": f"API error: {resp.status_code}"}

        data = resp.json()
        lead_total = data.get("effective_num_lead_credits", 0)
        lead_used = data.get("num_lead_credits_used", 0)
        dd_total = data.get("effective_num_direct_dial_credits", 0)
        dd_used = data.get("num_direct_dial_credits_used", 0)
        ai_total = data.get("effective_num_ai_credits", 0)
        ai_used = data.get("num_ai_credits_used", 0)

        return {
            "lead_credits_remaining": lead_total - lead_used,
            "lead_credits_used": lead_used,
            "direct_dial_remaining": dd_total - dd_used,
            "direct_dial_used": dd_used,
            "ai_credits_remaining": ai_total - ai_used,
            "ai_credits_used": ai_used,
        }

    except Exception as e:
        logger.error("Apollo credits error: {}", e)
        return {"lead_credits_remaining": 0, "lead_credits_used": 0,
                "direct_dial_remaining": 0, "direct_dial_used": 0,
                "ai_credits_remaining": 0, "ai_credits_used": 0,
                "note": str(e)}
```

**Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_apollo_sync_service.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add app/services/apollo_sync_service.py tests/test_apollo_sync_service.py
git commit -m "feat: add Apollo sync service -- discovery + credits"
```

---

## Task 3: Apollo Sync Service — Enrichment + Sync

**Files:**
- Modify: `app/services/apollo_sync_service.py`
- Modify: `tests/test_apollo_sync_service.py`

**Step 1: Write the failing tests**

Append to `tests/test_apollo_sync_service.py`:

```python
from app.services.apollo_sync_service import enrich_selected_contacts, sync_contacts_to_apollo


@pytest.mark.asyncio
async def test_enrich_selected_contacts(db_session):
    """Enrich should call people/match and return contact details with credit tracking."""
    from app.models import VendorCard
    vc = VendorCard(vendor_name="Acme", normalized_name="acme", primary_source="manual")
    db_session.add(vc)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "person": {
            "id": "abc123",
            "first_name": "Jane",
            "last_name": "Doe",
            "title": "VP Procurement",
            "email": "jane@acme.com",
            "email_status": "verified",
            "phone_numbers": [{"type": "direct_dial", "sanitized_number": "+15551234"}],
            "linkedin_url": "https://linkedin.com/in/janedoe",
        }
    }

    mock_credits = MagicMock()
    mock_credits.status_code = 200
    mock_credits.json.return_value = {
        "effective_num_lead_credits": 95,
        "num_lead_credits_used": 1,
        "effective_num_direct_dial_credits": 160,
        "num_direct_dial_credits_used": 0,
        "effective_num_ai_credits": 5000,
        "num_ai_credits_used": 0,
    }

    with patch("app.services.apollo_sync_service.http.post", new_callable=AsyncMock, return_value=mock_resp):
        with patch("app.services.apollo_sync_service.http.get", new_callable=AsyncMock, return_value=mock_credits):
            with patch("app.services.apollo_sync_service.settings") as ms:
                ms.apollo_api_key = "test-key"
                result = await enrich_selected_contacts(
                    apollo_ids=["abc123"],
                    vendor_card_id=vc.id,
                    db=db_session,
                )

    assert result["enriched"] == 1
    assert len(result["contacts"]) == 1
    assert result["contacts"][0]["email"] == "jane@acme.com"


@pytest.mark.asyncio
async def test_sync_contacts_to_apollo(db_session):
    """Sync should push AvailAI contacts to Apollo with dedup enabled."""
    from app.models import VendorCard, VendorContact
    vc = VendorCard(vendor_name="Acme", normalized_name="acme", primary_source="manual")
    db_session.add(vc)
    db_session.flush()

    contact = VendorContact(
        vendor_card_id=vc.id, full_name="John Smith",
        email="john@acme.com", source="manual",
        first_name="John", last_name="Smith", title="Buyer",
    )
    db_session.add(contact)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"contact": {"id": "apollo_new_1"}}

    with patch("app.services.apollo_sync_service.http.post", new_callable=AsyncMock, return_value=mock_resp):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            ms.apollo_rate_limit_per_min = 60
            result = await sync_contacts_to_apollo(db=db_session)

    assert result["synced"] == 1
    assert result["skipped"] == 0
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_apollo_sync_service.py::test_enrich_selected_contacts tests/test_apollo_sync_service.py::test_sync_contacts_to_apollo -v`
Expected: FAIL with `ImportError: cannot import name 'enrich_selected_contacts'`

**Step 3: Add enrich + sync functions to the service**

Append to `app/services/apollo_sync_service.py` (after `get_credits`):

```python
from sqlalchemy.orm import Session

from app.models import VendorCard, VendorContact


async def enrich_selected_contacts(
    apollo_ids: list[str],
    vendor_card_id: int,
    db: Session,
) -> dict:
    """Enrich selected contacts via Apollo people/match. Costs 1 lead credit each.

    Creates VendorContact rows attached to the given vendor card.
    Returns: {enriched, verified, credits_used, credits_remaining, contacts: [...]}
    """
    api_key = _get_api_key()
    if not api_key:
        return {"enriched": 0, "verified": 0, "credits_used": 0, "credits_remaining": 0, "contacts": []}

    vendor_card = db.get(VendorCard, vendor_card_id)
    if not vendor_card:
        return {"enriched": 0, "verified": 0, "credits_used": 0, "credits_remaining": 0,
                "contacts": [], "error": "Vendor card not found"}

    contacts = []
    verified_count = 0

    for apollo_id in apollo_ids:
        try:
            resp = await http.post(
                f"{APOLLO_BASE}/people/match",
                json={"id": apollo_id},
                headers=_headers(),
                timeout=30,
            )

            if resp.status_code != 200:
                logger.warning("Apollo enrich failed for {}: {}", apollo_id, resp.status_code)
                continue

            person = resp.json().get("person")
            if not person:
                continue

            first = (person.get("first_name") or "").strip()
            last = (person.get("last_name") or "").strip()
            full_name = f"{first} {last}".strip() or "Unknown"
            email = person.get("email")
            email_status = person.get("email_status", "unknown")
            is_verified = email_status == "verified"
            phone = _extract_phone(person)
            title = person.get("title") or ""

            if is_verified:
                verified_count += 1

            # Upsert VendorContact (dedup on vendor_card_id + email)
            existing = None
            if email:
                existing = db.query(VendorContact).filter_by(
                    vendor_card_id=vendor_card_id, email=email
                ).first()

            if existing:
                existing.full_name = full_name
                existing.title = title
                existing.phone = phone or existing.phone
                existing.linkedin_url = person.get("linkedin_url") or existing.linkedin_url
                existing.is_verified = is_verified
                existing.source = "apollo"
            else:
                new_contact = VendorContact(
                    vendor_card_id=vendor_card_id,
                    full_name=full_name,
                    first_name=first,
                    last_name=last,
                    title=title,
                    email=email,
                    phone=phone,
                    linkedin_url=person.get("linkedin_url"),
                    source="apollo",
                    is_verified=is_verified,
                    confidence=90 if is_verified else 60,
                    contact_type="person",
                )
                db.add(new_contact)

            contacts.append({
                "apollo_id": apollo_id,
                "full_name": full_name,
                "title": title,
                "email": email,
                "email_status": email_status,
                "phone": phone,
                "linkedin_url": person.get("linkedin_url"),
                "seniority": classify_contact_seniority(title),
                "is_verified": is_verified,
            })

        except Exception as e:
            logger.error("Apollo enrich error for {}: {}", apollo_id, e)

    db.commit()
    credit_info = await get_credits()

    return {
        "enriched": len(contacts),
        "verified": verified_count,
        "credits_used": len(contacts),
        "credits_remaining": credit_info.get("lead_credits_remaining", 0),
        "contacts": contacts,
    }


async def sync_contacts_to_apollo(
    db: Session,
    label: str = "availai-import",
) -> dict:
    """Push AvailAI vendor contacts (with emails) to Apollo as contacts.

    Uses run_dedupe=true to avoid duplicates.
    Returns: {synced, skipped, errors}
    """
    api_key = _get_api_key()
    if not api_key:
        return {"synced": 0, "skipped": 0, "errors": 0, "note": "No API key"}

    contacts = db.query(VendorContact).filter(
        VendorContact.email.isnot(None),
        VendorContact.email != "",
    ).all()

    synced = 0
    skipped = 0
    errors = 0

    for contact in contacts:
        payload = {
            "first_name": contact.first_name or "",
            "last_name": contact.last_name or "",
            "email": contact.email,
            "title": contact.title or "",
            "organization_name": contact.vendor_card.vendor_name if contact.vendor_card else "",
            "label_names": [label],
            "run_dedupe": True,
        }

        try:
            resp = await http.post(
                f"{APOLLO_BASE}/contacts",
                json=payload,
                headers=_headers(),
                timeout=15,
            )

            if resp.status_code == 200:
                synced += 1
            elif resp.status_code == 422:
                skipped += 1
            else:
                errors += 1
                logger.warning("Apollo sync error for {}: {}", contact.email, resp.status_code)

        except Exception as e:
            errors += 1
            logger.error("Apollo sync exception for {}: {}", contact.email, e)

    return {"synced": synced, "skipped": skipped, "errors": errors}


def _extract_phone(person: dict) -> str | None:
    """Extract best phone from Apollo person record."""
    if person.get("phone_number"):
        return person["phone_number"]
    phones = person.get("phone_numbers", [])
    if phones:
        for ptype in ("direct_dial", "mobile", "work"):
            for p in phones:
                if p.get("type") == ptype and p.get("sanitized_number"):
                    return p["sanitized_number"]
        if phones[0].get("sanitized_number"):
            return phones[0]["sanitized_number"]
    return None
```

**Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_apollo_sync_service.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add app/services/apollo_sync_service.py tests/test_apollo_sync_service.py
git commit -m "feat: add Apollo enrich + sync to apollo_sync_service"
```

---

## Task 4: Apollo Router — All Endpoints

**Files:**
- Create: `app/routers/apollo_sync.py`
- Modify: `app/main.py:918` (add router registration after prospect_suggested_router)
- Test: `tests/test_routers_apollo_sync.py`

**Step 1: Write the failing tests**

Create `tests/test_routers_apollo_sync.py`:

```python
"""Tests for Apollo sync router.

Tests all /api/apollo/* endpoints with mocked service layer.
Called by: pytest
Depends on: app.routers.apollo_sync, conftest fixtures
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestApolloDiscover:
    def test_discover_success(self, client):
        mock_result = {
            "domain": "acme.com",
            "contacts": [{"apollo_id": "abc", "full_name": "Jane Doe", "title": "VP Procurement"}],
            "total_found": 1,
        }
        with patch("app.routers.apollo_sync.discover_contacts", new_callable=AsyncMock, return_value=mock_result):
            resp = client.get("/api/apollo/discover/acme.com")
        assert resp.status_code == 200
        assert resp.json()["total_found"] == 1

    def test_discover_with_params(self, client):
        mock_result = {"domain": "acme.com", "contacts": [], "total_found": 0}
        with patch("app.routers.apollo_sync.discover_contacts", new_callable=AsyncMock, return_value=mock_result):
            resp = client.get("/api/apollo/discover/acme.com?max_results=5")
        assert resp.status_code == 200


class TestApolloEnrich:
    def test_enrich_success(self, client, db_session):
        from app.models import VendorCard
        vc = VendorCard(vendor_name="Acme", normalized_name="acme", primary_source="manual")
        db_session.add(vc)
        db_session.commit()

        mock_result = {
            "enriched": 1, "verified": 1, "credits_used": 1,
            "credits_remaining": 94, "contacts": [],
        }
        with patch("app.routers.apollo_sync.enrich_selected_contacts", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post("/api/apollo/enrich", json={
                "apollo_ids": ["abc123"],
                "vendor_card_id": vc.id,
            })
        assert resp.status_code == 200
        assert resp.json()["enriched"] == 1

    def test_enrich_empty_ids(self, client):
        resp = client.post("/api/apollo/enrich", json={
            "apollo_ids": [],
            "vendor_card_id": 1,
        })
        assert resp.status_code == 422


class TestApolloCredits:
    def test_credits_success(self, client):
        mock_result = {
            "lead_credits_remaining": 90, "lead_credits_used": 5,
            "direct_dial_remaining": 160, "direct_dial_used": 0,
            "ai_credits_remaining": 5000, "ai_credits_used": 0,
        }
        with patch("app.routers.apollo_sync.get_credits", new_callable=AsyncMock, return_value=mock_result):
            resp = client.get("/api/apollo/credits")
        assert resp.status_code == 200
        assert resp.json()["lead_credits_remaining"] == 90


class TestApolloSync:
    def test_sync_success(self, client):
        mock_result = {"synced": 3, "skipped": 1, "errors": 0}
        with patch("app.routers.apollo_sync.sync_contacts_to_apollo", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post("/api/apollo/sync-contacts")
        assert resp.status_code == 200
        assert resp.json()["synced"] == 3
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_apollo_sync.py -v`
Expected: FAIL

**Step 3: Create the router**

Create `app/routers/apollo_sync.py`:

```python
"""Apollo sync router -- contact discovery, enrichment, sync, and sequences.

Provides /api/apollo/* endpoints for bidirectional Apollo.io integration.
Discovery returns masked emails; enrichment reveals full contact data.

Called by: app/main.py
Depends on: app/services/apollo_sync_service.py, app/dependencies.py
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..services.apollo_sync_service import (
    discover_contacts,
    enrich_selected_contacts,
    get_credits,
    sync_contacts_to_apollo,
)
from ..schemas.apollo import (
    ApolloCreditsResponse,
    ApolloEnrichRequest,
    ApolloEnrichResponse,
    ApolloSyncResponse,
)

router = APIRouter(prefix="/api/apollo", tags=["apollo"])


@router.get("/discover/{domain}")
async def discover(
    domain: str,
    max_results: int = Query(default=10, ge=1, le=25),
    user=Depends(require_user),
):
    """Search Apollo for procurement contacts at a domain. Returns masked preview."""
    return await discover_contacts(domain, max_results=max_results)


@router.post("/enrich", response_model=ApolloEnrichResponse)
async def enrich(
    req: ApolloEnrichRequest,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich selected contacts via Apollo people/match. Costs 1 lead credit each."""
    return await enrich_selected_contacts(
        apollo_ids=req.apollo_ids,
        vendor_card_id=req.vendor_card_id,
        db=db,
    )


@router.get("/credits", response_model=ApolloCreditsResponse)
async def credits(user=Depends(require_user)):
    """Get current Apollo credit usage."""
    return await get_credits()


@router.post("/sync-contacts", response_model=ApolloSyncResponse)
async def sync(
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Push AvailAI vendor contacts to Apollo (dedup enabled)."""
    return await sync_contacts_to_apollo(db=db)
```

**Step 4: Register the router in main.py**

Add after the `prospect_suggested_router` line (around line 918):

```python
from .routers.apollo_sync import router as apollo_sync_router

app.include_router(apollo_sync_router)
```

**Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_apollo_sync.py -v`
Expected: All 5 tests PASS

**Step 6: Commit**

```bash
git add app/routers/apollo_sync.py app/main.py tests/test_routers_apollo_sync.py
git commit -m "feat: add Apollo sync router with discover/enrich/credits/sync endpoints"
```

---

## Task 5: Frontend — Apollo Tab in Company Drawer

**Files:**
- Modify: `app/static/crm.js` (add Apollo tab logic)
- Modify: `app/templates/index.html` (add Apollo tab button + panel)

**Step 1: Identify insertion points in index.html**

Search for the company drawer tab bar. Find existing tab buttons (e.g., "Contacts", "Activity") and add an "Apollo" tab button. Find the corresponding panel area and add the Apollo panel.

**Step 2: Add tab button to index.html**

In the company drawer tab bar, add after the last existing tab:

```html
<button class="tab-btn" data-tab="apollo">Apollo</button>
```

**Step 3: Add tab panel to index.html**

Below the existing tab panels, add:

```html
<div id="companyApolloTab" class="tab-panel" data-tab="apollo" style="display:none">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h6 class="mb-0">Apollo Contacts</h6>
    <div>
      <span id="apolloCredits" class="badge bg-secondary me-2">Credits: --</span>
      <button class="btn btn-sm btn-outline-primary" onclick="apolloDiscover()">Find Contacts</button>
    </div>
  </div>
  <div id="apolloDiscoverResults"></div>
  <div id="apolloEnrichResults" style="display:none"></div>
</div>
```

**Step 4: Add Apollo JS functions to crm.js**

Find the end of the company-drawer-related code in crm.js and add the Apollo functions. All dynamic content MUST use the existing `esc()` helper function for HTML escaping (project pattern). Use `textContent` where possible; `innerHTML` only with fully escaped values.

Key functions to add:
- `apolloLoadCredits()` - fetch `/api/apollo/credits`, update badge via `textContent`
- `apolloDiscover()` - fetch `/api/apollo/discover/{domain}`, render contact table with checkboxes (all values through `esc()`)
- `apolloToggleAll(master)` - select/deselect all checkboxes
- `apolloEnrichSelected()` - collect checked IDs, confirm credit cost, POST to `/api/apollo/enrich`, render results

**Step 5: Wire tab activation**

In the company drawer tab-switch handler in crm.js, add:
```javascript
if (tab === 'apollo') { apolloLoadCredits(); }
```

Also set `window._currentCompanyDomain` and `window._currentCompanyVendorCardId` when opening the company drawer.

**Step 6: Manual browser test**

1. Open AvailAI, go to CRM Companies
2. Click a company with a domain
3. Click "Apollo" tab
4. Verify credits badge loads
5. Click "Find Contacts", verify results table appears

**Step 7: Commit**

```bash
git add app/static/crm.js app/templates/index.html
git commit -m "feat: add Apollo tab to company drawer -- discover + enrich UI"
```

---

## Task 6: Edge Case Tests + Full Coverage

**Files:**
- Modify: `tests/test_apollo_sync_service.py`
- Modify: `tests/test_routers_apollo_sync.py`

**Step 1: Add edge case tests to service tests**

```python
@pytest.mark.asyncio
async def test_enrich_unknown_vendor_card(db_session):
    with patch("app.services.apollo_sync_service.settings") as ms:
        ms.apollo_api_key = "test-key"
        result = await enrich_selected_contacts(
            apollo_ids=["abc"], vendor_card_id=99999, db=db_session,
        )
    assert result["enriched"] == 0
    assert "error" in result


@pytest.mark.asyncio
async def test_sync_no_contacts_with_email(db_session):
    with patch("app.services.apollo_sync_service.settings") as ms:
        ms.apollo_api_key = "test-key"
        result = await sync_contacts_to_apollo(db=db_session)
    assert result["synced"] == 0


@pytest.mark.asyncio
async def test_discover_masks_all_emails():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "people": [
            {"id": "1", "first_name": "A", "last_name": "B", "email": "a.b@secret.com", "title": "Buyer"},
            {"id": "2", "first_name": "C", "last_name": "D", "title": "Procurement Manager"},
        ],
        "pagination": {"total_entries": 2},
    }
    with patch("app.services.apollo_sync_service.http.post", new_callable=AsyncMock, return_value=mock_resp):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            result = await discover_contacts("secret.com")
    for c in result["contacts"]:
        assert "a.b@secret.com" not in str(c)
    assert result["contacts"][0]["email_masked"] is not None
    assert result["contacts"][1]["email_masked"] is None
```

**Step 2: Run full coverage check**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_apollo_sync_service.py tests/test_routers_apollo_sync.py tests/test_schemas_apollo.py --cov=app/services/apollo_sync_service --cov=app/routers/apollo_sync --cov=app/schemas/apollo --cov-report=term-missing --tb=short -q`
Expected: 100% coverage on all three modules

**Step 3: Run full test suite to check for regressions**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q`
Expected: All existing tests still pass

**Step 4: Commit**

```bash
git add tests/test_apollo_sync_service.py tests/test_routers_apollo_sync.py
git commit -m "test: add edge case coverage for Apollo sync -- 100% coverage"
```

---

## Task 7: Memory Update + Manual Steps Checklist

**Files:**
- Modify: `/root/.claude/projects/-root/memory/MEMORY.md`
- Modify: `/root/.claude/projects/-root/memory/plugin_integration_plan.md`

**Step 1: Update MEMORY.md**

Update the "In Progress: Plugin Integration" section to mark Phase 2 as code-complete. Note manual steps still needed.

**Step 2: Update plugin_integration_plan.md**

Mark Phase 2 with details: 4 new files, 4 endpoints, Apollo tab in company drawer.

**Step 3: Print manual steps checklist**

Output for user:
```
MANUAL STEPS (Apollo UI at app.apollo.io):
1. [ ] Update profile: Settings > Profile > First: M, Last: Khoury, Title: Owner
2. [ ] Link email: Settings > Email > Connect > Sign in with mkhoury@trioscs.com
3. [ ] Create sequence "Intro - Component Sourcing" (3 steps)
4. [ ] Create sequence "Follow-up - No Response" (2 steps)
5. [ ] Create sequence "Re-engage - Past Contacts" (1 step)
```

**Step 4: Commit**

```bash
git add -A
git commit -m "docs: update memory with Apollo Phase 2 completion"
```

---

## Summary

| Task | What | New Files | Tests |
|------|------|-----------|-------|
| 1 | Pydantic schemas | `app/schemas/apollo.py` | 5 |
| 2 | Service: discover + credits | `app/services/apollo_sync_service.py` | 4 |
| 3 | Service: enrich + sync | (same file) | 2 |
| 4 | Router + main.py | `app/routers/apollo_sync.py` | 5 |
| 5 | Frontend: Apollo tab | (modify crm.js + index.html) | Manual |
| 6 | Edge cases + coverage | (test files) | 3 |
| 7 | Memory + manual steps | (memory files) | N/A |

**Total: 7 tasks, ~19 automated tests, 3 new files, 4 modified files, 0 migrations**
