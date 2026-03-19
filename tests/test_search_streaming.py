"""Tests for search streaming, aggressive dedup, and shortlist features.

Called by: pytest
Depends on: app/search_service.py, app/connectors/sources.py
"""

from unittest.mock import patch

from app.connectors.mouser import MouserConnector
from app.connectors.sources import NexarConnector


def test_base_connector_has_source_name():
    """Each connector exposes a source_name property matching its source_type."""
    nexar = NexarConnector.__new__(NexarConnector)
    assert hasattr(nexar, "source_name")
    assert isinstance(nexar.source_name, str)
    assert len(nexar.source_name) > 0


def test_build_connectors_all_skipped_when_no_creds(db_session):
    """_build_connectors skips all sources when no credentials are configured."""
    from app.search_service import _build_connectors

    with patch("app.search_service.get_credentials_batch", return_value={}):
        connectors, stats, disabled = _build_connectors(db_session)

    assert isinstance(connectors, list)
    assert isinstance(stats, dict)
    assert isinstance(disabled, set)
    assert len(connectors) == 0
    assert any(s["status"] in ("skipped", "disabled") for s in stats.values())


def test_build_connectors_instantiates_with_creds(db_session):
    """_build_connectors creates connector instances when credentials exist."""
    from app.search_service import _build_connectors

    fake_creds = {("mouser", "MOUSER_API_KEY"): "fake-mouser-key"}
    with patch("app.search_service.get_credentials_batch", return_value=fake_creds):
        connectors, stats, disabled = _build_connectors(db_session)

    assert len(connectors) == 1
    assert isinstance(connectors[0], MouserConnector)
    # Mouser should not appear in stats (it was instantiated, not skipped)
    assert "mouser" not in stats
    # Other sources should be skipped
    assert stats["nexar"]["status"] == "skipped"
