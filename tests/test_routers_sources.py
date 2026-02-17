"""
tests/test_routers_sources.py — Tests for Sources & Email Mining Router

Tests connector factory, sighting creation from attachments, and
email mining test connector.

Called by: pytest
Depends on: routers/sources.py
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.routers.sources import (
    _EmailMiningTestConnector,
    _create_sightings_from_attachment,
    _get_connector_for_source,
)


# ── _EmailMiningTestConnector ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_mining_test_connector_returns_status():
    connector = _EmailMiningTestConnector()
    results = await connector.search("LM358N")
    assert len(results) == 1
    assert results[0]["status"] == "ok"


# ── _get_connector_for_source ─────────────────────────────────────────


def test_get_connector_unknown_source():
    """Unknown source name returns None."""
    result = _get_connector_for_source("nonexistent_source")
    assert result is None


def test_get_connector_email_mining_when_enabled(monkeypatch):
    """Email mining returns test connector when enabled."""
    monkeypatch.setattr("app.routers.sources.settings", SimpleNamespace(
        email_mining_enabled=True,
        nexar_client_id=None, brokerbin_api_key=None, ebay_client_id=None,
        digikey_client_id=None, mouser_api_key=None, oemsecrets_api_key=None,
        sourcengine_api_key=None,
    ))
    result = _get_connector_for_source("email_mining")
    assert isinstance(result, _EmailMiningTestConnector)


def test_get_connector_email_mining_when_disabled(monkeypatch):
    """Email mining returns None when disabled."""
    monkeypatch.setattr("app.routers.sources.settings", SimpleNamespace(
        email_mining_enabled=False,
        nexar_client_id=None, brokerbin_api_key=None, ebay_client_id=None,
        digikey_client_id=None, mouser_api_key=None, oemsecrets_api_key=None,
        sourcengine_api_key=None,
    ))
    result = _get_connector_for_source("email_mining")
    assert result is None


# ── _create_sightings_from_attachment ────────────────────────────────


def _mock_db_for_sightings(requirements: list, existing_sightings: list | None = None):
    """Build a mock db session for sighting creation tests."""
    db = MagicMock()

    def query_side_effect(model):
        mock_q = MagicMock()
        model_name = model.__name__ if hasattr(model, "__name__") else str(model)
        if model_name == "Requirement":
            mock_q.filter_by.return_value.all.return_value = requirements
        elif model_name == "Sighting":
            mock_q.filter_by.return_value.first.return_value = (
                existing_sightings[0] if existing_sightings else None
            )
        return mock_q

    db.query.side_effect = query_side_effect
    db.add = MagicMock()
    db.flush = MagicMock()
    return db


def test_create_sightings_exact_mpn_match():
    """Rows with exact MPN match create sightings."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "LM358N", "qty": 100, "unit_price": 0.50}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 1
    assert db.add.call_count == 1


def test_create_sightings_no_requirements():
    """No requirements → 0 sightings."""
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([])

    rows = [{"mpn": "LM358N", "qty": 100}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 0


def test_create_sightings_skips_empty_mpn():
    """Rows with no MPN are skipped."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "", "qty": 100}, {"mpn": None, "qty": 200}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 0


def test_create_sightings_skips_duplicates():
    """Existing sighting prevents duplicate creation."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    existing = SimpleNamespace(id=99)  # Already exists
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req], existing_sightings=[existing])

    rows = [{"mpn": "LM358N", "qty": 100}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 0


def test_create_sightings_case_insensitive_mpn():
    """MPN matching is case-insensitive (both uppercased)."""
    req = SimpleNamespace(id=1, mpn="lm358n", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "LM358N", "qty": 100}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 1
