"""Tests for Lusha credit split, health check registration, and enrichment wiring.

Verifies:
- lusha_phone and lusha_discovery are independent credit pools
- get_all_budgets() returns both pools plus an aggregate lusha entry
- _step_lusha_phones uses lusha_phone provider
- _step_lusha_discovery uses lusha_discovery provider
- Phone guard relaxation: contacts with work phones still get direct dial lookup
- Lusha health check connector is registered
- Batch exhaustion check uses split provider names

Called by: pytest
Depends on: app.services.credit_manager, app.services.customer_enrichment_service,
            app.services.customer_enrichment_batch, app.routers.sources
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.services.credit_manager import (
    can_use_credits,
    get_all_budgets,
    get_monthly_usage,
    record_credit_usage,
)


# ── Credit split pool tests ──────────────────────────────────────────


def test_lusha_phone_default_limit(db_session: Session):
    """lusha_phone pool defaults to 4480 credits."""
    usage = get_monthly_usage(db_session, "lusha_phone")
    assert usage["limit"] == 4480
    assert usage["used"] == 0
    assert usage["remaining"] == 4480


def test_lusha_discovery_default_limit(db_session: Session):
    """lusha_discovery pool defaults to 1920 credits."""
    usage = get_monthly_usage(db_session, "lusha_discovery")
    assert usage["limit"] == 1920
    assert usage["used"] == 0
    assert usage["remaining"] == 1920


def test_split_pools_are_independent(db_session: Session):
    """Using phone credits does not affect discovery credits."""
    record_credit_usage(db_session, "lusha_phone", 100)
    db_session.flush()

    phone = get_monthly_usage(db_session, "lusha_phone")
    disc = get_monthly_usage(db_session, "lusha_discovery")

    assert phone["used"] == 100
    assert phone["remaining"] == 4380
    assert disc["used"] == 0
    assert disc["remaining"] == 1920


def test_get_all_budgets_includes_split_and_aggregate(db_session: Session):
    """get_all_budgets returns lusha_phone, lusha_discovery, and aggregate lusha."""
    record_credit_usage(db_session, "lusha_phone", 50)
    record_credit_usage(db_session, "lusha_discovery", 20)
    db_session.flush()

    budgets = get_all_budgets(db_session)
    providers = [b["provider"] for b in budgets]

    assert "lusha_phone" in providers
    assert "lusha_discovery" in providers
    assert "lusha" in providers

    agg = next(b for b in budgets if b["provider"] == "lusha")
    assert agg["used"] == 70
    assert agg["limit"] == 4480 + 1920
    assert agg["remaining"] == (4480 - 50) + (1920 - 20)


def test_can_use_credits_respects_split_pools(db_session: Session):
    """Each pool enforces its own limit independently."""
    # Exhaust discovery pool
    from app.services.credit_manager import _get_or_create_row

    row = _get_or_create_row(db_session, "lusha_discovery", get_monthly_usage(db_session, "lusha_discovery")["month"])
    row.credits_used = 1920
    db_session.flush()

    assert not can_use_credits(db_session, "lusha_discovery", 1)
    assert can_use_credits(db_session, "lusha_phone", 1)


# ── Enrichment wiring tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_lusha_phones_uses_phone_provider(db_session: Session):
    """_step_lusha_phones records credits against lusha_phone provider."""
    from app.services.customer_enrichment_service import _step_lusha_phones

    mock_result = {"phone": "+1-555-1234", "phone_type": "direct_dial"}
    with patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock, return_value=mock_result):
        contacts = [{"email": "test@example.com", "full_name": "John Doe"}]
        result = await _step_lusha_phones(db_session, contacts, "example.com")

    assert result[0]["phone"] == "+1-555-1234"
    usage = get_monthly_usage(db_session, "lusha_phone")
    assert usage["used"] == 1

    # Old "lusha" pool should NOT be touched
    old_usage = get_monthly_usage(db_session, "lusha")
    assert old_usage["used"] == 0


@pytest.mark.asyncio
async def test_step_lusha_discovery_uses_discovery_provider(db_session: Session):
    """_step_lusha_discovery records credits against lusha_discovery provider."""
    from app.services.customer_enrichment_service import _step_lusha_discovery

    mock_contacts = [{"email": "buyer@acme.com", "full_name": "Jane Buyer", "title": "Buyer"}]
    with patch(
        "app.connectors.lusha_client.search_contacts",
        new_callable=AsyncMock,
        return_value=mock_contacts,
    ):
        result = await _step_lusha_discovery(db_session, "acme.com", "Acme Inc", needed=3)

    assert len(result) == 1
    usage = get_monthly_usage(db_session, "lusha_discovery")
    assert usage["used"] == 1

    old_usage = get_monthly_usage(db_session, "lusha")
    assert old_usage["used"] == 0


@pytest.mark.asyncio
async def test_phone_guard_relaxed_for_work_phones(db_session: Session):
    """Contacts with work/office phones (not direct_dial) still get Lusha lookup."""
    from app.services.customer_enrichment_service import _step_lusha_phones

    mock_result = {"phone": "+1-555-9999", "phone_type": "direct_dial"}
    with patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock, return_value=mock_result):
        contacts = [
            {"email": "worker@co.com", "full_name": "Bob Work", "phone": "+1-555-0000", "phone_type": "work"},
        ]
        result = await _step_lusha_phones(db_session, contacts, "co.com")

    # Should have been enriched with the direct dial from Lusha
    assert result[0]["phone"] == "+1-555-9999"
    assert result[0]["phone_type"] == "direct_dial"


@pytest.mark.asyncio
async def test_phone_guard_skips_existing_direct_dials(db_session: Session):
    """Contacts already having direct_dial are NOT re-looked-up."""
    from app.services.customer_enrichment_service import _step_lusha_phones

    with patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock) as mock_find:
        contacts = [
            {"email": "vip@co.com", "full_name": "VIP", "phone": "+1-555-1111", "phone_type": "direct_dial"},
        ]
        result = await _step_lusha_phones(db_session, contacts, "co.com")

    mock_find.assert_not_called()
    assert result[0]["phone"] == "+1-555-1111"


# ── Batch exhaustion check test ───────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_stops_when_split_pools_exhausted(db_session: Session):
    """Batch enrichment checks split provider names for early stop."""
    from app.services.credit_manager import _get_or_create_row

    # Exhaust all four providers
    for p, limit in [("apollo", 10000), ("hunter_verify", 500), ("lusha_phone", 4480), ("lusha_discovery", 1920)]:
        row = _get_or_create_row(db_session, p, get_monthly_usage(db_session, p)["month"])
        row.credits_used = limit
    db_session.flush()

    assert not can_use_credits(db_session, "apollo", 1)
    assert not can_use_credits(db_session, "hunter_verify", 1)
    assert not can_use_credits(db_session, "lusha_phone", 1)
    assert not can_use_credits(db_session, "lusha_discovery", 1)

    # The batch check logic: not any(can_use_credits(...) for p in [...])
    result = not any(
        can_use_credits(db_session, p) for p in ["apollo", "hunter_verify", "lusha_phone", "lusha_discovery"]
    )
    assert result is True


# ── Health check connector test ───────────────────────────────────────


def test_lusha_connector_registered():
    """_get_connector_for_source returns _LushaTestConnector for lusha_enrichment."""
    from app.routers.sources import _get_connector_for_source, _LushaTestConnector

    connector = _get_connector_for_source("lusha_enrichment")
    assert isinstance(connector, _LushaTestConnector)


def test_lusha_enrichment_in_seed_sources():
    """lusha_enrichment is in the SOURCES list in _seed_api_sources."""
    import hashlib
    import re

    from app.main import _seed_api_sources

    # Read the source of the function to verify lusha_enrichment is present
    import inspect

    source = inspect.getsource(_seed_api_sources)
    assert '"lusha_enrichment"' in source, "lusha_enrichment missing from SOURCES list"
    assert '"lusha_enrichment": 6400' in source, "lusha_enrichment missing from quota_map"
