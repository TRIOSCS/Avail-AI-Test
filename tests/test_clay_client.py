"""Tests for Clay OAuth2 client — token lifecycle, find_contacts, enrich_company.

Covers: PKCE generation, authorize URL, token exchange, token refresh,
        find_contacts, enrich_company, graceful fallbacks on missing tokens.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def _mock_settings():
    with patch("app.connectors.clay_client.settings") as mock_s:
        mock_s.clay_client_id = "test-client-id"
        mock_s.clay_client_secret = "test-client-secret"
        mock_s.clay_redirect_uri = "https://avail.trioscs.com/api/clay/callback"
        yield mock_s


@pytest.fixture
def _mock_http():
    with patch("app.connectors.clay_client.http") as mock_h:
        yield mock_h


# ── PKCE tests ────────────────────────────────────────────────────


def test_generate_pkce_challenge():
    from app.connectors.clay_client import generate_pkce_challenge
    verifier, challenge = generate_pkce_challenge()
    assert len(verifier) > 40
    assert len(challenge) > 20
    assert verifier != challenge


def test_generate_pkce_unique():
    from app.connectors.clay_client import generate_pkce_challenge
    v1, c1 = generate_pkce_challenge()
    v2, c2 = generate_pkce_challenge()
    assert v1 != v2
    assert c1 != c2


# ── Authorize URL tests ──────────────────────────────────────────


def test_build_authorize_url():
    from app.connectors.clay_client import build_authorize_url
    url, state = build_authorize_url()
    assert "app.clay.com/oauth/authorize" in url
    assert "client_id=test-client-id" in url
    assert "response_type=code" in url
    assert "code_challenge_method=S256" in url
    assert "scope=" in url
    assert state


def test_build_authorize_url_with_state():
    from app.connectors.clay_client import build_authorize_url
    url, state = build_authorize_url(state="my-state")
    assert state == "my-state"
    assert "state=my-state" in url


def test_build_authorize_url_no_client_id():
    with patch("app.connectors.clay_client.settings") as mock_s:
        mock_s.clay_client_id = ""
        from app.connectors.clay_client import build_authorize_url
        with pytest.raises(ValueError, match="CLAY_CLIENT_ID"):
            build_authorize_url()


# ── Token exchange tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_exchange_code_success(_mock_http):
    from app.connectors.clay_client import build_authorize_url, exchange_code_for_tokens
    _, state = build_authorize_url()

    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=200,
        json=lambda: {
            "access_token": "clay-access-123",
            "refresh_token": "clay-refresh-456",
            "expires_in": 3600,
            "scope": "mcp mcp:run-enrichment",
        },
    ))
    result = await exchange_code_for_tokens("auth-code-xyz", state)
    assert result is not None
    assert result["access_token"] == "clay-access-123"
    assert result["refresh_token"] == "clay-refresh-456"


@pytest.mark.asyncio
async def test_exchange_code_no_verifier(_mock_http):
    from app.connectors.clay_client import exchange_code_for_tokens
    result = await exchange_code_for_tokens("auth-code-xyz", "unknown-state")
    assert result is None


@pytest.mark.asyncio
async def test_exchange_code_api_error(_mock_http):
    from app.connectors.clay_client import build_authorize_url, exchange_code_for_tokens
    _, state = build_authorize_url()

    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=400,
        text="invalid_grant",
    ))
    result = await exchange_code_for_tokens("bad-code", state)
    assert result is None


@pytest.mark.asyncio
async def test_exchange_code_exception(_mock_http):
    from app.connectors.clay_client import build_authorize_url, exchange_code_for_tokens
    _, state = build_authorize_url()
    _mock_http.post = AsyncMock(side_effect=Exception("Network error"))
    result = await exchange_code_for_tokens("code", state)
    assert result is None


# ── Token refresh tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_token_success(_mock_http):
    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=200,
        json=lambda: {
            "access_token": "new-access-789",
            "refresh_token": "new-refresh-abc",
            "expires_in": 3600,
        },
    ))
    from app.connectors.clay_client import refresh_clay_token
    result = await refresh_clay_token("old-refresh-token")
    assert result is not None
    assert result["access_token"] == "new-access-789"


@pytest.mark.asyncio
async def test_refresh_token_failure(_mock_http):
    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=401,
        text="invalid_refresh_token",
    ))
    from app.connectors.clay_client import refresh_clay_token
    result = await refresh_clay_token("bad-refresh")
    assert result is None


@pytest.mark.asyncio
async def test_refresh_token_exception(_mock_http):
    _mock_http.post = AsyncMock(side_effect=Exception("Timeout"))
    from app.connectors.clay_client import refresh_clay_token
    result = await refresh_clay_token("token")
    assert result is None


# ── DB token helpers ─────────────────────────────────────────────


def test_save_and_get_token_from_db(db_session):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, _get_token_from_db

    _save_token_to_db(db_session, {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_in": 3600,
        "scope": "mcp",
    })
    db_session.flush()

    token = _get_token_from_db(db_session)
    assert token is not None
    assert token.access_token == "access-1"
    assert token.refresh_token == "refresh-1"
    assert token.scope == "mcp"
    # SQLite returns naive datetimes — compare without tz
    assert token.expires_at.replace(tzinfo=None) > datetime.now(timezone.utc).replace(tzinfo=None)


def test_save_token_upsert(db_session):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, _get_token_from_db

    _save_token_to_db(db_session, {
        "access_token": "first",
        "refresh_token": "refresh-first",
        "expires_in": 3600,
    })
    db_session.flush()

    _save_token_to_db(db_session, {
        "access_token": "second",
        "refresh_token": "refresh-second",
        "expires_in": 7200,
    })
    db_session.flush()

    from app.models.enrichment import ClayOAuthToken
    count = db_session.query(ClayOAuthToken).count()
    assert count == 1

    token = _get_token_from_db(db_session)
    assert token.access_token == "second"
    assert token.refresh_token == "refresh-second"


# ── get_valid_token tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_valid_token_no_token(db_session):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import get_valid_token
    result = await get_valid_token(db_session)
    assert result is None


@pytest.mark.asyncio
async def test_get_valid_token_still_valid(db_session):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, get_valid_token

    _save_token_to_db(db_session, {
        "access_token": "still-valid",
        "refresh_token": "refresh",
        "expires_in": 3600,
    })
    db_session.flush()

    result = await get_valid_token(db_session)
    assert result == "still-valid"


@pytest.mark.asyncio
async def test_get_valid_token_refreshes_expired(db_session, _mock_http):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, get_valid_token
    from app.models.enrichment import ClayOAuthToken

    _save_token_to_db(db_session, {
        "access_token": "expired-token",
        "refresh_token": "refresh-abc",
        "expires_in": 0,
    })
    db_session.flush()

    # Force expired
    token = db_session.query(ClayOAuthToken).first()
    token.expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.flush()

    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=200,
        json=lambda: {
            "access_token": "refreshed-token",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        },
    ))

    result = await get_valid_token(db_session)
    assert result == "refreshed-token"


@pytest.mark.asyncio
async def test_get_valid_token_refresh_fails(db_session, _mock_http):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, get_valid_token
    from app.models.enrichment import ClayOAuthToken

    _save_token_to_db(db_session, {
        "access_token": "expired",
        "refresh_token": "bad-refresh",
        "expires_in": 0,
    })
    db_session.flush()

    token = db_session.query(ClayOAuthToken).first()
    token.expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.flush()

    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=401,
        text="invalid_refresh_token",
    ))

    result = await get_valid_token(db_session)
    assert result is None


# ── find_contacts tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_contacts_no_db():
    from app.connectors.clay_client import find_contacts
    result = await find_contacts("acme.com")
    assert result == []


@pytest.mark.asyncio
async def test_find_contacts_no_token(db_session):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import find_contacts
    result = await find_contacts("acme.com", db=db_session)
    assert result == []


@pytest.mark.asyncio
async def test_find_contacts_success(db_session, _mock_http):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, find_contacts

    _save_token_to_db(db_session, {
        "access_token": "valid-token",
        "refresh_token": "refresh",
        "expires_in": 3600,
    })
    db_session.flush()

    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=200,
        json=lambda: {
            "people": [
                {"name": "Alice Buyer", "title": "Procurement Manager",
                 "email": "alice@acme.com", "phone": "+1-555-0100",
                 "linkedin_url": "https://linkedin.com/in/alice"},
                {"name": "Bob Tech", "title": "Engineer",
                 "email": "bob@acme.com"},
            ],
        },
    ))

    result = await find_contacts("acme.com", "procurement,engineer", db=db_session)
    assert len(result) == 2
    assert result[0]["full_name"] == "Alice Buyer"
    assert result[0]["source"] == "clay"
    assert result[0]["email"] == "alice@acme.com"
    assert result[1]["full_name"] == "Bob Tech"


@pytest.mark.asyncio
async def test_find_contacts_api_error(db_session, _mock_http):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, find_contacts

    _save_token_to_db(db_session, {
        "access_token": "valid",
        "refresh_token": "refresh",
        "expires_in": 3600,
    })
    db_session.flush()

    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=404,
        text="Not found",
    ))

    result = await find_contacts("acme.com", db=db_session)
    assert result == []


@pytest.mark.asyncio
async def test_find_contacts_exception(db_session, _mock_http):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, find_contacts

    _save_token_to_db(db_session, {
        "access_token": "valid",
        "refresh_token": "refresh",
        "expires_in": 3600,
    })
    db_session.flush()

    _mock_http.post = AsyncMock(side_effect=Exception("Timeout"))
    result = await find_contacts("acme.com", db=db_session)
    assert result == []


# ── enrich_company tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_company_no_db():
    from app.connectors.clay_client import enrich_company
    result = await enrich_company("acme.com")
    assert result is None


@pytest.mark.asyncio
async def test_enrich_company_no_token(db_session):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import enrich_company
    result = await enrich_company("acme.com", db=db_session)
    assert result is None


@pytest.mark.asyncio
async def test_enrich_company_success(db_session, _mock_http):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, enrich_company

    _save_token_to_db(db_session, {
        "access_token": "valid-token",
        "refresh_token": "refresh",
        "expires_in": 3600,
    })
    db_session.flush()

    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=200,
        json=lambda: {
            "name": "Acme Corp",
            "industry": "Electronics",
            "size": "100-500",
            "country": "US",
            "locality": "San Jose, CA",
            "website": "https://acme.com",
            "linkedin_url": "https://linkedin.com/company/acme",
        },
    ))

    result = await enrich_company("acme.com", db=db_session)
    assert result is not None
    assert result["source"] == "clay"
    assert result["legal_name"] == "Acme Corp"
    assert result["industry"] == "Electronics"
    assert result["hq_city"] == "San Jose"
    assert result["hq_state"] == "CA"
    assert result["domain"] == "acme.com"


@pytest.mark.asyncio
async def test_enrich_company_api_error(db_session, _mock_http):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, enrich_company

    _save_token_to_db(db_session, {
        "access_token": "valid",
        "refresh_token": "refresh",
        "expires_in": 3600,
    })
    db_session.flush()

    _mock_http.post = AsyncMock(return_value=MagicMock(
        status_code=500,
        text="Server error",
    ))

    result = await enrich_company("acme.com", db=db_session)
    assert result is None


@pytest.mark.asyncio
async def test_enrich_company_exception(db_session, _mock_http):
    from tests.conftest import engine  # noqa: F401
    from app.connectors.clay_client import _save_token_to_db, enrich_company

    _save_token_to_db(db_session, {
        "access_token": "valid",
        "refresh_token": "refresh",
        "expires_in": 3600,
    })
    db_session.flush()

    _mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
    result = await enrich_company("acme.com", db=db_session)
    assert result is None
