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

    _module, client = _make_mock_anthropic("350")

    with (
        patch("app.services.credential_service.get_credential_cached", return_value="sk-test-key"),
        patch("anthropic.Anthropic", return_value=client),
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


# ── O6: the synchronous Anthropic client is pooled/reused, not rebuilt per call ──


def test_get_anthropic_client_reuses_one_client_per_key():
    """The shared getter builds one Anthropic client per distinct key and reuses it."""
    from app.http_client import _anthropic_clients, get_anthropic_client

    _anthropic_clients.clear()
    with patch("anthropic.Anthropic", side_effect=lambda api_key: MagicMock(name=api_key)) as cls:
        c1 = get_anthropic_client("sk-a")
        c2 = get_anthropic_client("sk-a")
        c3 = get_anthropic_client("sk-b")

    assert c1 is c2, "same key must reuse the cached client, not re-instantiate"
    assert c3 is not c1, "a different key gets its own client"
    assert cls.call_count == 2, "one construction per distinct key (sk-a, sk-b), not per call"


def test_estimate_qty_reuses_anthropic_client_across_calls():
    """Repeated qty-estimation calls reuse one Anthropic client (its httpx pool is not
    rebuilt every call — O6)."""
    from app.services.sighting_aggregation import _estimate_qty_with_ai

    _module, client = _make_mock_anthropic("350")

    with (
        patch("app.services.credential_service.get_credential_cached", return_value="sk-reuse"),
        patch("anthropic.Anthropic", return_value=client) as cls,
    ):
        _estimate_qty_with_ai([100, 200, 300])
        _estimate_qty_with_ai([400, 500, 600])

    assert cls.call_count == 1, "Anthropic client must be built once and reused, not per call"
    assert client.messages.create.call_count == 2, "both estimations run through the one reused client"
