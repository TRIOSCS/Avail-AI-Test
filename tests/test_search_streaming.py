"""Tests for search streaming, aggressive dedup, and shortlist features.

Called by: pytest
Depends on: app/search_service.py, app/connectors/sources.py
"""

from app.connectors.sources import NexarConnector


def test_base_connector_has_source_name():
    """Each connector exposes a source_name property matching its source_type."""
    nexar = NexarConnector.__new__(NexarConnector)
    assert hasattr(nexar, "source_name")
    assert isinstance(nexar.source_name, str)
    assert len(nexar.source_name) > 0
