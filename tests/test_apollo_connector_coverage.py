"""tests/test_apollo_connector_coverage.py — Coverage tests for
app/connectors/apollo.py.

Tests all branches of _parse_company_response, _parse_contacts_response,
search_company, and search_contacts.

Called by: pytest
Depends on: app.connectors.apollo, unittest.mock, pytest
"""

import os

os.environ["TESTING"] = "1"

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


@contextmanager
def patched_async_client(mock_client: AsyncMock):
    """Patch httpx.AsyncClient so its `async with` yields `mock_client`."""
    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        yield mock_client


def make_response(status_code: int, payload: dict) -> MagicMock:
    """Build a MagicMock httpx response with the given status and JSON body."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


# -- _parse_company_response --------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({}, id="no_org_key"),
        pytest.param({"organization": None}, id="org_is_none"),
    ],
)
def test_parse_company_response_returns_none(data):
    from app.connectors.apollo import _parse_company_response

    assert _parse_company_response(data) is None


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


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({}, id="empty"),
        pytest.param({"other": "data"}, id="no_people_key"),
    ],
)
def test_parse_contacts_response_returns_empty(data):
    from app.connectors.apollo import _parse_contacts_response

    assert _parse_contacts_response(data) == []


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

    mock_response = make_response(
        200,
        {"organization": {"name": "Test Corp", "website_url": "https://testcorp.com"}},
    )
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    with patched_async_client(mock_client):
        result = await search_company("testcorp.com", "fake-key")

    assert result is not None
    assert result["legal_name"] == "Test Corp"


@pytest.mark.parametrize(
    "get_kwargs",
    [
        pytest.param({"return_value": make_response(403, {})}, id="non_200"),
        pytest.param({"side_effect": httpx.HTTPError("connection error")}, id="http_error"),
        pytest.param({"side_effect": ValueError("bad value")}, id="value_error"),
        pytest.param({"return_value": make_response(200, {})}, id="response_no_org"),
    ],
)
async def test_search_company_returns_none(get_kwargs):
    from app.connectors.apollo import search_company

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(**get_kwargs)

    with patched_async_client(mock_client):
        result = await search_company("testcorp.com", "fake-key")

    assert result is None


# -- search_contacts ----------------------------------------------------------


async def test_search_contacts_success():
    from app.connectors.apollo import search_contacts

    mock_response = make_response(
        200,
        {"people": [{"name": "Jane Doe", "email": "jane@co.com", "title": "CEO"}]},
    )
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patched_async_client(mock_client):
        result = await search_contacts("co.com", "fake-key", limit=5)

    assert len(result) == 1
    assert result[0]["full_name"] == "Jane Doe"


@pytest.mark.parametrize(
    "post_kwargs",
    [
        pytest.param({"return_value": make_response(429, {})}, id="non_200"),
        pytest.param({"side_effect": httpx.HTTPError("timeout")}, id="http_error"),
        pytest.param({"side_effect": KeyError("missing")}, id="key_error"),
    ],
)
async def test_search_contacts_returns_empty(post_kwargs):
    from app.connectors.apollo import search_contacts

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(**post_kwargs)

    with patched_async_client(mock_client):
        result = await search_contacts("co.com", "fake-key")

    assert result == []


async def test_search_contacts_default_limit():
    """search_contacts should pass per_page=10 by default."""
    from app.connectors.apollo import search_contacts

    mock_response = make_response(200, {"people": []})
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patched_async_client(mock_client):
        result = await search_contacts("co.com", "fake-key")

    # Verify the per_page was sent as 10
    call_kwargs = mock_client.post.call_args
    assert call_kwargs.kwargs["json"]["per_page"] == 10
    assert result == []
