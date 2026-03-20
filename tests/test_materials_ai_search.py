"""tests/test_materials_ai_search.py -- Tests for AI-powered faceted search pre-selection.

Covers: app/services/materials_ai_search.py + AI interpret route in htmx_views.py
Depends on: conftest.py, commodity_registry
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.services.materials_ai_search import (
    _build_commodity_summary,
    _build_enum_reference,
    get_parent_for_commodity,
    interpret_search_query,
)
from tests.conftest import engine  # noqa: F401

# ── Unit tests for helper functions ──────────────────────────────────


def test_build_commodity_summary_includes_known_commodities():
    """Commodity summary should include key categories like dram, capacitors."""
    summary = _build_commodity_summary()
    assert "dram" in summary
    assert "capacitors" in summary
    assert "resistors" in summary


def test_build_enum_reference_includes_ddr_types():
    """Enum reference should include DDR type values for dram."""
    ref = _build_enum_reference()
    assert "dram.ddr_type" in ref
    assert "DDR4" in ref
    assert "DDR5" in ref


def test_get_parent_for_commodity_known():
    """Known commodities should return their parent group."""
    assert get_parent_for_commodity("dram") == "Memory & Storage"
    assert get_parent_for_commodity("capacitors") == "Passives"


def test_get_parent_for_commodity_unknown():
    """Unknown commodities should return empty string."""
    assert get_parent_for_commodity("nonexistent_thing") == ""


# ── Tests for interpret_search_query ─────────────────────────────────


def _make_mock_response(response_text: str):
    """Create a mock Anthropic API response."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    return mock_response


def _patch_anthropic(response_text: str = None, side_effect: Exception = None):
    """Return a context manager that patches anthropic module and settings.

    Mocks the lazy import of anthropic inside interpret_search_query.
    """
    mock_client = MagicMock()
    if side_effect:
        mock_client.messages.create.side_effect = side_effect
    else:
        mock_client.messages.create.return_value = _make_mock_response(response_text)

    mock_anthropic_mod = MagicMock()
    mock_anthropic_mod.Anthropic.return_value = mock_client

    mock_settings = MagicMock()
    mock_settings.anthropic_api_key = "test-key"

    return (
        patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}),
        patch("app.config.settings", mock_settings),
    )


@pytest.mark.asyncio
async def test_interpret_returns_none_for_short_query():
    """Queries with fewer than 3 words should return None without calling API."""
    result = await interpret_search_query("DDR5")
    assert result is None

    result = await interpret_search_query("DDR5 memory")
    assert result is None


@pytest.mark.asyncio
async def test_interpret_returns_none_for_empty_query():
    """Empty or None queries should return None."""
    result = await interpret_search_query("")
    assert result is None

    result = await interpret_search_query(None)
    assert result is None


@pytest.mark.asyncio
async def test_interpret_returns_none_when_no_api_key():
    """Should return None gracefully when no API key is configured."""
    mock_settings = MagicMock()
    mock_settings.anthropic_api_key = ""

    with patch("app.config.settings", mock_settings):
        result = await interpret_search_query("DDR5 32GB ECC server memory")
        assert result is None


@pytest.mark.asyncio
async def test_interpret_success_dram_query():
    """Successful AI interpretation should return commodity + filters."""
    ai_response = json.dumps(
        {
            "commodity": "dram",
            "filters": {"ddr_type": ["DDR5"], "capacity_gb_min": 32, "ecc": ["true"]},
            "summary": "DDR5, 32GB+, ECC server memory",
        }
    )

    p1, p2 = _patch_anthropic(ai_response)
    with p1, p2:
        result = await interpret_search_query("DDR5 32GB ECC server memory")

    assert result is not None
    assert result["commodity"] == "dram"
    assert "ddr_type" in result["filters"]
    assert result["filters"]["ddr_type"] == ["DDR5"]
    assert result["summary"] == "DDR5, 32GB+, ECC server memory"


@pytest.mark.asyncio
async def test_interpret_success_capacitor_query():
    """Capacitor search should return capacitors commodity with spec filters."""
    ai_response = json.dumps(
        {
            "commodity": "capacitors",
            "filters": {"dielectric": ["X7R"], "package": ["0805"]},
            "summary": "X7R 0805 capacitors",
        }
    )

    p1, p2 = _patch_anthropic(ai_response)
    with p1, p2:
        result = await interpret_search_query("X7R 0805 ceramic capacitor")

    assert result is not None
    assert result["commodity"] == "capacitors"
    assert result["filters"]["dielectric"] == ["X7R"]


@pytest.mark.asyncio
async def test_interpret_handles_markdown_fenced_json():
    """Should strip markdown code fences from the response."""
    fenced_json = '```json\n{"commodity": "dram", "filters": {}, "summary": "DRAM search"}\n```'

    p1, p2 = _patch_anthropic(fenced_json)
    with p1, p2:
        result = await interpret_search_query("DDR5 server memory modules")

    assert result is not None
    assert result["commodity"] == "dram"


@pytest.mark.asyncio
async def test_interpret_handles_invalid_commodity():
    """Unknown commodity should be cleared to empty string."""
    ai_response = json.dumps(
        {
            "commodity": "nonexistent_category",
            "filters": {},
            "summary": "unknown search",
        }
    )

    p1, p2 = _patch_anthropic(ai_response)
    with p1, p2:
        result = await interpret_search_query("some unknown component type search")

    assert result is not None
    assert result["commodity"] == ""


@pytest.mark.asyncio
async def test_interpret_handles_api_error():
    """API exceptions should return None gracefully."""
    p1, p2 = _patch_anthropic(side_effect=Exception("API timeout"))
    with p1, p2:
        result = await interpret_search_query("DDR5 ECC server memory module")

    assert result is None


@pytest.mark.asyncio
async def test_interpret_handles_invalid_json_response():
    """Non-JSON responses from AI should return None."""
    p1, p2 = _patch_anthropic("I think you want DDR5 memory")
    with p1, p2:
        result = await interpret_search_query("DDR5 ECC server memory module")

    assert result is None


@pytest.mark.asyncio
async def test_interpret_adds_default_summary():
    """When AI response omits summary, a default should be generated."""
    ai_response = json.dumps(
        {
            "commodity": "dram",
            "filters": {"ddr_type": ["DDR5"]},
        }
    )

    p1, p2 = _patch_anthropic(ai_response)
    with p1, p2:
        result = await interpret_search_query("DDR5 server memory modules")

    assert result is not None
    assert "summary" in result
    assert "dram" in result["summary"]


@pytest.mark.asyncio
async def test_interpret_validates_filters_type():
    """Non-dict filters should be replaced with empty dict."""
    ai_response = json.dumps(
        {
            "commodity": "dram",
            "filters": "invalid",
            "summary": "test",
        }
    )

    p1, p2 = _patch_anthropic(ai_response)
    with p1, p2:
        result = await interpret_search_query("DDR5 server memory modules")

    assert result is not None
    assert result["filters"] == {}


# ── Route tests ──────────────────────────────────────────────────────


def test_ai_interpret_route_returns_200(client):
    """AI interpret route should return 200 even with short query (no AI call)."""
    resp = client.get("/v2/partials/materials/ai-interpret?q=DDR5")
    assert resp.status_code == 200


def test_ai_interpret_route_empty_query(client):
    """AI interpret route with empty query should return 200."""
    resp = client.get("/v2/partials/materials/ai-interpret")
    assert resp.status_code == 200


def test_ai_interpret_route_with_long_query(client):
    """AI interpret route with 3+ words triggers AI (mocked)."""
    with patch(
        "app.services.materials_ai_search.interpret_search_query",
        return_value={
            "commodity": "dram",
            "filters": {"ddr_type": ["DDR5"]},
            "summary": "DDR5 server memory",
        },
    ):
        resp = client.get("/v2/partials/materials/ai-interpret?q=DDR5+32GB+ECC+server+memory")
        assert resp.status_code == 200
        assert "DDR5 server memory" in resp.text
