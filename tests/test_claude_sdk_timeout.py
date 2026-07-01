"""Regression tests: synchronous Anthropic SDK calls must pass a bounded timeout.

Covers the two sync ``anthropic.Anthropic().messages.create(...)`` call sites that
run inside the post-search thread-pool worker:
  - app/services/sighting_aggregation.py :: _estimate_qty_with_ai
  - app/services/vendor_affinity_service.py :: _classify_mpn

Without an explicit ``timeout=``, a hung API response would block the worker for the
SDK's ~600s default. These tests assert every call passes a bounded (~30s) timeout so
that can't happen.

Depends on: unittest.mock (patches the ``anthropic`` module + settings).
"""

from unittest.mock import MagicMock, patch

# Ceiling for what counts as "bounded" — the shared claude_client uses ~30s.
BOUNDED_TIMEOUT_MAX = 60


def _make_mock_anthropic(text: str):
    """Return (anthropic_module_mock, client_mock) whose create() yields ``text``."""
    mock_content = MagicMock()
    mock_content.text = text
    mock_resp = MagicMock()
    mock_resp.content = [mock_content]
    client = MagicMock()
    client.messages.create.return_value = mock_resp
    module = MagicMock()
    module.Anthropic.return_value = client
    return module, client


def test_estimate_qty_passes_bounded_timeout():
    from app.services.sighting_aggregation import _estimate_qty_with_ai

    module, client = _make_mock_anthropic("350")
    mock_settings = MagicMock()
    mock_settings.ANTHROPIC_API_KEY = "sk-test-key"
    mock_claude_client = MagicMock()
    mock_claude_client.MODELS = {"fast": "claude-haiku-3"}

    with patch.dict(
        "sys.modules",
        {
            "anthropic": module,
            "app.config": MagicMock(settings=mock_settings),
            "app.utils.claude_client": mock_claude_client,
        },
    ):
        result = _estimate_qty_with_ai([100, 200, 300])

    assert result == {"qty": 350, "approximate": False}
    client.messages.create.assert_called_once()
    kwargs = client.messages.create.call_args.kwargs
    assert "timeout" in kwargs, "messages.create must pass an explicit timeout"
    assert 0 < kwargs["timeout"] <= BOUNDED_TIMEOUT_MAX


def test_classify_mpn_passes_bounded_timeout():
    from app.services.vendor_affinity_service import _classify_mpn

    module, client = _make_mock_anthropic("Resistor")

    with patch.dict("sys.modules", {"anthropic": module}):
        result = _classify_mpn("RC0402", None, "sk-fake")

    assert result == "Resistor"
    client.messages.create.assert_called_once()
    kwargs = client.messages.create.call_args.kwargs
    assert "timeout" in kwargs, "messages.create must pass an explicit timeout"
    assert 0 < kwargs["timeout"] <= BOUNDED_TIMEOUT_MAX
