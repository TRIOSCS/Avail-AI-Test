"""Tests for Lusha client — find_person, search_contacts, enrich_company.

Covers: all 3 API functions, graceful no-key fallback, error handling,
Prospecting API 2-step flow, helper functions.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.fixture(autouse=True)
def _mock_settings():
    with patch("app.connectors.lusha_client.settings") as mock_s:
        mock_s.lusha_api_key = "test-lusha-key"
        yield mock_s


@pytest.fixture
def _mock_http():
    with patch("app.connectors.lusha_client.http") as mock_h:
        yield mock_h


# ── find_person tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_person_by_email(_mock_http):
    _mock_http.get = AsyncMock(return_value=MagicMock(
        status_code=200,
        json=lambda: {
            "firstName": "Jane",
            "lastName": "Doe",
            "title": "VP Procurement",
            "phoneNumbers": [{"number": "+1-555-0100", "type": "direct_dial"}],
            "emailAddresses": [{"email": "jane@acme.com", "type": "work"}],
            "linkedinUrl": "https://linkedin.com/in/janedoe",
            "confidence": 90,
        },
    ))
    from app.connectors.lusha_client import find_person
    result = await find_person(email="jane@acme.com")
    assert result is not None
    assert result["full_name"] == "Jane Doe"
    assert result["email"] == "jane@acme.com"
    assert result["phone"] == "+1-555-0100"
    assert result["phone_type"] == "direct_dial"
    assert result["source"] == "lusha"


@pytest.mark.asyncio
async def test_find_person_no_key():
    with patch("app.connectors.lusha_client.settings") as mock_s:
        mock_s.lusha_api_key = ""
        from app.connectors.lusha_client import find_person
        result = await find_person(email="test@test.com")
        assert result is None


@pytest.mark.asyncio
async def test_find_person_api_error(_mock_http):
    _mock_http.get = AsyncMock(return_value=MagicMock(
        status_code=429,
        text="Rate limited",
    ))
    from app.connectors.lusha_client import find_person
    result = await find_person(email="test@test.com")
    assert result is None


@pytest.mark.asyncio
async def test_find_person_no_params():
    from app.connectors.lusha_client import find_person
    result = await find_person()
    assert result is None


@pytest.mark.asyncio
async def test_find_person_by_name(_mock_http):
    _mock_http.get = AsyncMock(return_value=MagicMock(
        status_code=200,
        json=lambda: {
            "firstName": "John",
            "lastName": "Smith",
            "title": "Buyer",
            "phoneNumbers": [],
            "emailAddresses": [],
        },
    ))
    from app.connectors.lusha_client import find_person
    result = await find_person(first_name="John", last_name="Smith", company_name="Acme")
    assert result is not None
    assert result["full_name"] == "John Smith"


@pytest.mark.asyncio
async def test_find_person_exception(_mock_http):
    _mock_http.get = AsyncMock(side_effect=Exception("Network error"))
    from app.connectors.lusha_client import find_person
    result = await find_person(email="test@test.com")
    assert result is None


# ── search_contacts tests (Prospecting API 2-step) ───────────────


@pytest.mark.asyncio
async def test_search_contacts_success(_mock_http):
    """search_contacts uses POST /prospecting/contact/search then /enrich."""
    search_resp = MagicMock(
        status_code=201,
        json=lambda: {
            "requestId": "req-123",
            "totalResults": 2,
            "data": [
                {"contactId": "c1", "name": "Alice Buyer", "jobTitle": "Procurement Manager"},
                {"contactId": "c2", "name": "Bob Tech", "jobTitle": "Engineering Lead"},
            ],
        },
    )
    enrich_resp = MagicMock(
        status_code=201,
        json=lambda: {
            "requestId": "req-123",
            "contacts": [
                {
                    "id": "c1",
                    "isSuccess": True,
                    "data": {
                        "fullName": "Alice Buyer",
                        "jobTitle": "Procurement Manager",
                        "emailAddresses": [{"email": "alice@acme.com", "emailType": "work", "emailConfidence": "A+"}],
                        "phoneNumbers": [{"number": "+1-555-0200", "phoneType": "mobile"}],
                        "socialLinks": {"linkedin": "https://linkedin.com/in/alice"},
                    },
                },
                {
                    "id": "c2",
                    "isSuccess": True,
                    "data": {
                        "fullName": "Bob Tech",
                        "jobTitle": "Engineering Lead",
                        "emailAddresses": [{"email": "bob@acme.com", "emailType": "work", "emailConfidence": "A"}],
                        "phoneNumbers": [],
                        "socialLinks": {},
                    },
                },
            ],
        },
    )
    _mock_http.post = AsyncMock(side_effect=[search_resp, enrich_resp])

    from app.connectors.lusha_client import search_contacts
    result = await search_contacts("acme.com", titles=["procurement", "engineer"], limit=5)
    assert len(result) == 2
    assert result[0]["full_name"] == "Alice Buyer"
    assert result[0]["email"] == "alice@acme.com"
    assert result[0]["confidence"] == 95  # A+ → 95
    assert result[1]["source"] == "lusha"
    assert result[1]["full_name"] == "Bob Tech"


@pytest.mark.asyncio
async def test_search_contacts_no_domain():
    from app.connectors.lusha_client import search_contacts
    result = await search_contacts("")
    assert result == []


@pytest.mark.asyncio
async def test_search_contacts_search_error(_mock_http):
    _mock_http.post = AsyncMock(return_value=MagicMock(status_code=500, text="Server error"))
    from app.connectors.lusha_client import search_contacts
    result = await search_contacts("acme.com")
    assert result == []


@pytest.mark.asyncio
async def test_search_contacts_enrich_error(_mock_http):
    """If search succeeds but enrich fails, return empty."""
    search_resp = MagicMock(
        status_code=201,
        json=lambda: {
            "requestId": "req-456",
            "data": [{"contactId": "c1", "name": "Test", "jobTitle": "Buyer"}],
        },
    )
    enrich_resp = MagicMock(status_code=500, text="Internal error")
    _mock_http.post = AsyncMock(side_effect=[search_resp, enrich_resp])

    from app.connectors.lusha_client import search_contacts
    result = await search_contacts("acme.com")
    assert result == []


@pytest.mark.asyncio
async def test_search_contacts_exception(_mock_http):
    _mock_http.post = AsyncMock(side_effect=Exception("Timeout"))
    from app.connectors.lusha_client import search_contacts
    result = await search_contacts("acme.com")
    assert result == []


@pytest.mark.asyncio
async def test_search_contacts_no_results(_mock_http):
    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=201,
        json=lambda: {"requestId": "req-789", "data": [], "totalResults": 0},
    ))
    from app.connectors.lusha_client import search_contacts
    result = await search_contacts("unknown.com")
    assert result == []


# ── enrich_company tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_company_success(_mock_http):
    _mock_http.get = AsyncMock(return_value=MagicMock(
        status_code=200,
        json=lambda: {
            "name": "Acme Corp",
            "domain": "acme.com",
            "industry": "Electronics",
            "employeeCount": 500,
            "location": {"city": "New York", "state": "NY", "country": "US"},
            "website": "https://acme.com",
            "linkedinUrl": "https://linkedin.com/company/acme",
        },
    ))
    from app.connectors.lusha_client import enrich_company
    result = await enrich_company("acme.com")
    assert result is not None
    assert result["name"] == "Acme Corp"
    assert result["hq_city"] == "New York"
    assert result["source"] == "lusha"


@pytest.mark.asyncio
async def test_enrich_company_no_domain():
    from app.connectors.lusha_client import enrich_company
    result = await enrich_company("")
    assert result is None


@pytest.mark.asyncio
async def test_enrich_company_api_error(_mock_http):
    _mock_http.get = AsyncMock(return_value=MagicMock(status_code=404, text="Not found"))
    from app.connectors.lusha_client import enrich_company
    result = await enrich_company("unknown.com")
    assert result is None


# ── helper function tests ──────────────────────────────────────────


def test_best_phone_priority():
    from app.connectors.lusha_client import _best_phone
    phones = [
        {"number": "+1-work", "type": "work"},
        {"number": "+1-direct", "type": "direct_dial"},
        {"number": "+1-mobile", "type": "mobile"},
    ]
    number, ptype = _best_phone(phones)
    assert ptype == "direct_dial"
    assert number == "+1-direct"


def test_best_phone_prospecting_format():
    """Prospecting API uses phoneType instead of type."""
    from app.connectors.lusha_client import _best_phone
    phones = [
        {"number": "+1-phone", "phoneType": "phone"},
        {"number": "+1-direct", "phoneType": "direct"},
    ]
    number, ptype = _best_phone(phones)
    assert ptype == "direct"
    assert number == "+1-direct"


def test_best_phone_empty():
    from app.connectors.lusha_client import _best_phone
    assert _best_phone([]) == (None, None)


def test_best_email_priority():
    from app.connectors.lusha_client import _best_email
    emails = [
        {"email": "personal@gmail.com", "type": "personal"},
        {"email": "work@acme.com", "type": "work"},
    ]
    email, conf = _best_email(emails)
    assert email == "work@acme.com"
    assert conf == 50  # no emailConfidence → default 50


def test_best_email_with_confidence():
    from app.connectors.lusha_client import _best_email
    emails = [{"email": "test@acme.com", "emailType": "work", "emailConfidence": "A+"}]
    email, conf = _best_email(emails)
    assert email == "test@acme.com"
    assert conf == 95


def test_best_email_empty():
    from app.connectors.lusha_client import _best_email
    assert _best_email([]) == (None, 0)
