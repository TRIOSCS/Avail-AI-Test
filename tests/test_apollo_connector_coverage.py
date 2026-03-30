"""tests/test_apollo_connector_coverage.py — Coverage tests for app/connectors/apollo.py.

Tests all branches of _parse_company_response, _parse_contacts_response,
search_company, and search_contacts.

Called by: pytest
Depends on: app.connectors.apollo, unittest.mock, pytest
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# -- _parse_company_response --------------------------------------------------


def test_parse_company_response_returns_none_when_no_org():
    from app.connectors.apollo import _parse_company_response

    result = _parse_company_response({})
    assert result is None


def test_parse_company_response_returns_none_when_org_is_none():
    from app.connectors.apollo import _parse_company_response

    result = _parse_company_response({"organization": None})
    assert result is None


def test_parse_company_response_full_org():
    from app.connectors.apollo import _parse_company_response

    data = {
        "organization": {
            "name": "Acme Corp",
            "website_url": "https://www.acme.com/",
            "linkedin_url": "https://linkedin.com/company/acme",
            "industry": "electronics",
            "estimated_num_employees": 500,
            "city": "Austin",
            "state": "TX",
            "country": "US",
        }
    }
    result = _parse_company_response(data)
    assert result is not None
    assert result["source"] == "apollo"
    assert result["legal_name"] == "Acme Corp"
    assert result["domain"] == "www.acme.com"
    assert result["linkedin_url"] == "https://linkedin.com/company/acme"
    assert result["industry"] == "electronics"
    assert result["employee_size"] == "500"
    assert result["hq_city"] == "Austin"
    assert result["hq_state"] == "TX"
    assert result["hq_country"] == "US"


def test_parse_company_response_strips_http_prefix():
    from app.connectors.apollo import _parse_company_response

    data = {"organization": {"name": "Beta Inc", "website_url": "http://beta.io/"}}
    result = _parse_company_response(data)
    assert result is not None
    assert result["domain"] == "beta.io"


def test_parse_company_response_no_employees_returns_none():
    from app.connectors.apollo import _parse_company_response

    data = {"organization": {"name": "Tiny Co", "estimated_num_employees": 0}}
    result = _parse_company_response(data)
    assert result is not None
    # 0 is falsy so employee_size should be None
    assert result["employee_size"] is None


def test_parse_company_response_missing_optional_fields():
    from app.connectors.apollo import _parse_company_response

    data = {"organization": {"name": "Minimal Corp"}}
    result = _parse_company_response(data)
    assert result is not None
    assert result["legal_name"] == "Minimal Corp"
    assert result["domain"] == ""
    assert result["linkedin_url"] is None
    assert result["industry"] is None
    assert result["employee_size"] is None
    assert result["hq_city"] is None


# -- _parse_contacts_response -------------------------------------------------


def test_parse_contacts_response_empty():
    from app.connectors.apollo import _parse_contacts_response

    result = _parse_contacts_response({})
    assert result == []


def test_parse_contacts_response_no_people_key():
    from app.connectors.apollo import _parse_contacts_response

    result = _parse_contacts_response({"other": "data"})
    assert result == []


def test_parse_contacts_response_single_person():
    from app.connectors.apollo import _parse_contacts_response

    data = {
        "people": [
            {
                "name": "Jane Smith",
                "email": "jane@example.com",
                "phone_number": "+1-555-1234",
                "title": "VP Sales",
                "linkedin_url": "https://linkedin.com/in/janesmith",
            }
        ]
    }
    result = _parse_contacts_response(data)
    assert len(result) == 1
    contact = result[0]
    assert contact["source"] == "apollo"
    assert contact["full_name"] == "Jane Smith"
    assert contact["email"] == "jane@example.com"
    assert contact["phone"] == "+1-555-1234"
    assert contact["title"] == "VP Sales"
    assert contact["linkedin_url"] == "https://linkedin.com/in/janesmith"


def test_parse_contacts_response_multiple_people():
    from app.connectors.apollo import _parse_contacts_response

    data = {
        "people": [
            {"name": "Alice", "email": "alice@co.com"},
            {"name": "Bob", "email": "bob@co.com"},
        ]
    }
    result = _parse_contacts_response(data)
    assert len(result) == 2
    assert result[0]["full_name"] == "Alice"
    assert result[1]["full_name"] == "Bob"


def test_parse_contacts_response_missing_fields():
    from app.connectors.apollo import _parse_contacts_response

    data = {"people": [{}]}
    result = _parse_contacts_response(data)
    assert len(result) == 1
    assert result[0]["full_name"] is None
    assert result[0]["email"] is None
    assert result[0]["phone"] is None


# -- search_company -----------------------------------------------------------


async def test_search_company_success():
    from app.connectors.apollo import search_company

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "organization": {
            "name": "Test Corp",
            "website_url": "https://testcorp.com",
        }
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_company("testcorp.com", "fake-key")

    assert result is not None
    assert result["legal_name"] == "Test Corp"


async def test_search_company_non_200_returns_none():
    from app.connectors.apollo import search_company

    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.json.return_value = {}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_company("testcorp.com", "fake-key")

    assert result is None


async def test_search_company_http_error_returns_none():
    from app.connectors.apollo import search_company

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.HTTPError("connection error"))

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_company("testcorp.com", "fake-key")

    assert result is None


async def test_search_company_value_error_returns_none():
    from app.connectors.apollo import search_company

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=ValueError("bad value"))

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_company("testcorp.com", "fake-key")

    assert result is None


async def test_search_company_response_no_org_returns_none():
    from app.connectors.apollo import search_company

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_company("testcorp.com", "fake-key")

    assert result is None


# -- search_contacts ----------------------------------------------------------


async def test_search_contacts_success():
    from app.connectors.apollo import search_contacts

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "people": [
            {"name": "Jane Doe", "email": "jane@co.com", "title": "CEO"},
        ]
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_contacts("co.com", "fake-key", limit=5)

    assert len(result) == 1
    assert result[0]["full_name"] == "Jane Doe"


async def test_search_contacts_non_200_returns_empty():
    from app.connectors.apollo import search_contacts

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.json.return_value = {}

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_contacts("co.com", "fake-key")

    assert result == []


async def test_search_contacts_http_error_returns_empty():
    from app.connectors.apollo import search_contacts

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.HTTPError("timeout"))

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_contacts("co.com", "fake-key")

    assert result == []


async def test_search_contacts_key_error_returns_empty():
    from app.connectors.apollo import search_contacts

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=KeyError("missing"))

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_contacts("co.com", "fake-key")

    assert result == []


async def test_search_contacts_default_limit():
    """search_contacts should pass per_page=10 by default."""
    from app.connectors.apollo import search_contacts

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"people": []}

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await search_contacts("co.com", "fake-key")

    # Verify the per_page was sent as 10
    call_kwargs = mock_client.post.call_args
    assert call_kwargs.kwargs["json"]["per_page"] == 10
    assert result == []
