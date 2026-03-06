"""Tests for per-source 7-day sighting cache in search_service.

Verifies that _fetch_fresh() skips API calls for connectors with recent
sightings in the DB, and correctly merges cached + fresh results.

Called by: pytest
Depends on: app.search_service, app.models.sourcing, tests.conftest
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, Sighting, User
from app.search_service import (
    _get_cached_sources,
    _load_cached_sightings,
    _sighting_to_connector_dict,
)


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(
        email="cache-test@test.com",
        name="Cache Tester",
        role="buyer",
        azure_id="cache-test-001",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def req(db_session: Session, user: User) -> Requirement:
    """A requisition + requirement for sighting creation."""
    requisition = Requisition(
        name="Cache Test Req",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requisition)
    db_session.flush()
    requirement = Requirement(
        requisition_id=requisition.id,
        primary_mpn="LM358N",
        normalized_mpn="lm358n",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.commit()
    db_session.refresh(requirement)
    return requirement


def _make_sighting(
    db_session, req, source_type="digikey", mpn="lm358n", vendor="DigiKey", days_ago=2, price=1.50, qty=500
):
    """Helper: create a Sighting with a specific age."""
    s = Sighting(
        requirement_id=req.id,
        vendor_name=vendor,
        vendor_name_normalized=vendor.lower(),
        mpn_matched="LM358N",
        normalized_mpn=mpn,
        manufacturer="Texas Instruments",
        qty_available=qty,
        unit_price=price,
        currency="USD",
        source_type=source_type,
        is_authorized=True,
        confidence=0.9,
        score=75.0,
        raw_data={"vendor_sku": "296-1395-5-ND", "vendor_url": "https://digikey.com/lm358n"},
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


# ── Test _get_cached_sources ─────────────────────────────────────────


def test_get_cached_sources_recent(db_session, req):
    """Sighting 2 days old → found in cache."""
    _make_sighting(db_session, req, source_type="digikey", days_ago=2)
    result = _get_cached_sources(["lm358n"], db_session)
    assert "lm358n" in result
    assert "digikey" in result["lm358n"]


def test_get_cached_sources_stale(db_session, req):
    """Sighting 10 days old → NOT found (outside 7-day window)."""
    _make_sighting(db_session, req, source_type="digikey", days_ago=10)
    result = _get_cached_sources(["lm358n"], db_session)
    assert result.get("lm358n", set()) == set()


def test_get_cached_sources_empty_keys(db_session):
    """Empty key list → empty result."""
    assert _get_cached_sources([], db_session) == {}


# ── Test _load_cached_sightings ──────────────────────────────────────


def test_load_cached_sightings_deduplicates(db_session, req):
    """Multiple sightings for same vendor+source+mpn → only newest kept."""
    _make_sighting(db_session, req, source_type="digikey", days_ago=5, price=1.20)
    _make_sighting(db_session, req, source_type="digikey", days_ago=1, price=1.50)

    cached_map = {"lm358n": {"digikey"}}
    results = _load_cached_sightings(cached_map, db_session)
    # Should get 1 result (deduped by vendor_name_normalized+source_type+normalized_mpn)
    assert len(results) == 1
    assert results[0].unit_price == 1.50  # newest one


def test_load_cached_sightings_empty(db_session):
    """Empty cached_map → empty list."""
    assert _load_cached_sightings({}, db_session) == []


# ── Test _sighting_to_connector_dict ─────────────────────────────────


def test_sighting_to_connector_dict_roundtrip(db_session, req):
    """All key fields preserved in conversion."""
    s = _make_sighting(db_session, req, source_type="mouser", vendor="Mouser Electronics", price=2.35, qty=1000)
    d = _sighting_to_connector_dict(s)
    assert d["vendor_name"] == "Mouser Electronics"
    assert d["mpn_matched"] == "LM358N"
    assert d["manufacturer"] == "Texas Instruments"
    assert d["qty_available"] == 1000
    assert d["unit_price"] == 2.35
    assert d["currency"] == "USD"
    assert d["source_type"] == "mouser"
    assert d["is_authorized"] is True
    assert d["vendor_sku"] == "296-1395-5-ND"
    assert d["vendor_url"] == "https://digikey.com/lm358n"


# ── Test _fetch_fresh with cache ─────────────────────────────────────


def _mock_connector(name, source_type, results=None):
    """Create a mock connector with a search method."""
    conn = AsyncMock()
    conn.__class__.__name__ = name
    conn.search = AsyncMock(return_value=results or [])
    return conn


@patch("app.search_service._get_search_redis", return_value=None)
@patch("app.search_service._get_search_cache", return_value=None)
@patch("app.search_service._set_search_cache")
def test_fetch_fresh_skips_cached_source(mock_set, mock_get, mock_redis, db_session, req):
    """DigiKey has cached sighting → DigiKey.search() NOT called, Mouser IS called."""
    from app.search_service import _fetch_fresh

    # Pre-create a recent DigiKey sighting
    _make_sighting(db_session, req, source_type="digikey", days_ago=2)

    # Mock ApiSource query to return no disabled sources
    with patch.object(db_session, "query") as orig_query:
        # We need the real query for sightings but mock for ApiSource
        real_query = (
            db_session.__class__.query.fget(db_session) if hasattr(db_session.__class__.query, "fget") else None
        )

    # Simpler approach: mock connectors directly
    dk_conn = _mock_connector("DigiKeyConnector", "digikey")
    mouser_conn = _mock_connector(
        "MouserConnector",
        "mouser",
        results=[
            {"vendor_name": "Mouser", "source_type": "mouser", "qty_available": 200, "unit_price": 1.80},
        ],
    )

    with (
        patch("app.services.credential_service.get_credential", return_value="fake-key"),
        patch("app.search_service.DigiKeyConnector", return_value=dk_conn),
        patch("app.search_service.MouserConnector", return_value=mouser_conn),
        patch("app.search_service.NexarConnector"),
        patch("app.search_service.BrokerBinConnector"),
        patch("app.search_service.EbayConnector"),
        patch("app.search_service.OEMSecretsConnector"),
        patch("app.search_service.SourcengineConnector"),
        patch("app.search_service.Element14Connector"),
    ):
        results, stats = asyncio.get_event_loop().run_until_complete(_fetch_fresh(["LM358N"], db_session))

    # DigiKey should NOT have been called (cached)
    dk_conn.search.assert_not_called()
    # Mouser SHOULD have been called
    mouser_conn.search.assert_called_once()

    # Stats should show digikey as "cached"
    stats_map = {s["source"]: s for s in stats}
    assert stats_map.get("digikey", {}).get("status") == "cached"


@patch("app.search_service._get_search_redis", return_value=None)
@patch("app.search_service._get_search_cache", return_value=None)
@patch("app.search_service._set_search_cache")
def test_fetch_fresh_all_cached(mock_set, mock_get, mock_redis, db_session, req):
    """All sources have cached sightings → zero API calls."""
    from app.search_service import _fetch_fresh

    # Create sightings for multiple sources
    _make_sighting(db_session, req, source_type="digikey", vendor="DigiKey", days_ago=1)
    _make_sighting(db_session, req, source_type="mouser", vendor="Mouser", days_ago=3)

    dk_conn = _mock_connector("DigiKeyConnector", "digikey")
    mouser_conn = _mock_connector("MouserConnector", "mouser")

    with (
        patch("app.services.credential_service.get_credential", return_value="fake-key"),
        patch("app.search_service.DigiKeyConnector", return_value=dk_conn),
        patch("app.search_service.MouserConnector", return_value=mouser_conn),
        patch("app.search_service.NexarConnector"),
        patch("app.search_service.BrokerBinConnector"),
        patch("app.search_service.EbayConnector"),
        patch("app.search_service.OEMSecretsConnector"),
        patch("app.search_service.SourcengineConnector"),
        patch("app.search_service.Element14Connector"),
    ):
        results, stats = asyncio.get_event_loop().run_until_complete(_fetch_fresh(["LM358N"], db_session))

    dk_conn.search.assert_not_called()
    mouser_conn.search.assert_not_called()
    # Should still have results from cached sightings
    assert len(results) >= 2


@patch("app.search_service._get_search_redis", return_value=None)
@patch("app.search_service._get_search_cache", return_value=None)
@patch("app.search_service._set_search_cache")
def test_fetch_fresh_partial_cache(mock_set, mock_get, mock_redis, db_session, req):
    """Mix: DigiKey cached, Mouser fresh → correct merge."""
    from app.search_service import _fetch_fresh

    _make_sighting(db_session, req, source_type="digikey", days_ago=2, price=1.50)

    dk_conn = _mock_connector("DigiKeyConnector", "digikey")
    mouser_conn = _mock_connector(
        "MouserConnector",
        "mouser",
        results=[
            {
                "vendor_name": "Mouser",
                "source_type": "mouser",
                "qty_available": 300,
                "unit_price": 1.75,
                "manufacturer": "TI",
            },
        ],
    )

    with (
        patch("app.services.credential_service.get_credential", return_value="fake-key"),
        patch("app.search_service.DigiKeyConnector", return_value=dk_conn),
        patch("app.search_service.MouserConnector", return_value=mouser_conn),
        patch("app.search_service.NexarConnector"),
        patch("app.search_service.BrokerBinConnector"),
        patch("app.search_service.EbayConnector"),
        patch("app.search_service.OEMSecretsConnector"),
        patch("app.search_service.SourcengineConnector"),
        patch("app.search_service.Element14Connector"),
    ):
        results, stats = asyncio.get_event_loop().run_until_complete(_fetch_fresh(["LM358N"], db_session))

    dk_conn.search.assert_not_called()
    mouser_conn.search.assert_called_once()
    # Should have both cached DigiKey + fresh Mouser results
    sources = {r.get("source_type") for r in results}
    assert "digikey" in sources
    assert "mouser" in sources


def test_cache_respects_nexar_alias(db_session, req):
    """Octopart sighting in DB → Nexar connector should be treated as cached."""
    _make_sighting(db_session, req, source_type="octopart", days_ago=3)

    cached = _get_cached_sources(["lm358n"], db_session)
    assert "octopart" in cached.get("lm358n", set())

    # The _SOURCE_CACHE_ALIASES mapping should cause nexar to be skipped
    from app.search_service import _SOURCE_CACHE_ALIASES

    nexar_aliases = _SOURCE_CACHE_ALIASES.get("nexar", set())
    # "octopart" is in the alias set for "nexar"
    assert "octopart" in nexar_aliases
    # So if cached sources include "octopart", nexar should be skipped
    assert nexar_aliases & cached["lm358n"]
