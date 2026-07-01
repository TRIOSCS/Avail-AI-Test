"""Tests for the web extractor with four trust gates.

Tests patch app.services.enrichment_worker.web_extractor.claude_json
(imported at the top of web_extractor.py via
``from app.utils.claude_client import claude_json``).

Gates under test (enforced in Python, NOT trusted from LLM output):
  1. Domain allowlist — untrusted URL → failed
  2. Exact MPN verbatim (normalize_mpn_key match) — mismatch → failed
  3. Confidence >= 0.92 — low score → failed
  4. URL capture — no URLs → failed
  Plus: description/manufacturer quality check.
"""

import dataclasses
from unittest.mock import AsyncMock, patch

import pytest

from app.services.enrichment_worker.web_extractor import extract_part_from_web

_GOOD = {
    "description": "Adjustable linear voltage regulator",
    "manufacturer": "Texas Instruments",
    "category": "Voltage Regulator",
    "datasheet_url": "https://www.ti.com/lit/ds/x.pdf",
    "confidence": 0.97,
    "exact_mpn_found": "LM317T",
    "source_urls": ["https://www.ti.com/product/LM317"],
}


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_all_gates_pass(mock_cj):
    mock_cj.return_value = dict(_GOOD)
    r = await extract_part_from_web("LM317T", "lm317t")
    assert r.status == "web_sourced"
    assert r.source_urls == ["https://www.ti.com/product/LM317"]
    assert mock_cj.call_args.kwargs["tools"][0]["type"] == "web_search_20250305"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"source_urls": ["https://www.ebay.com/itm/1"]}, id="untrusted_domain"),
        pytest.param({"exact_mpn_found": "LM317MT"}, id="mpn_mismatch"),
        pytest.param({"confidence": 0.80}, id="low_confidence"),
        # Gate 4 (anti-hallucination quality floor): a too-short description.
        pytest.param({"description": "reg"}, id="short_description"),
        # Gate 4: a missing manufacturer (never accept web data without a maker).
        pytest.param({"manufacturer": ""}, id="missing_manufacturer"),
    ],
)
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_gate_rejects(mock_cj, override):
    """Each trust gate rejects a single bad field → status 'failed'."""
    mock_cj.return_value = {**_GOOD, **override}
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_claude_error_returns_failed(mock_cj):
    """A NON-Claude exception (e.g. a bug) is swallowed → failed (chain falls
    through)."""
    mock_cj.side_effect = RuntimeError("unexpected bug")
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_claude_backend_error_propagates(mock_cj):
    """A Claude BACKEND failure must surface (not be swallowed) so the worker's circuit
    breaker can detect a sustained outage instead of marking every part not_found."""
    from app.utils.claude_errors import ClaudeError, ClaudeRateLimitError

    mock_cj.side_effect = ClaudeRateLimitError("429")
    with pytest.raises(ClaudeError):
        await extract_part_from_web("LM317T", "lm317t")


def test_web_extract_result_is_frozen():
    """WebExtractResult must be frozen so the shared ``_FAILED`` singleton can't be
    mutated in place — an aliasing footgun that would corrupt every future failure."""
    from app.services.enrichment_worker.web_extractor import WebExtractResult

    result = WebExtractResult(status="web_sourced", source_urls=["https://x.com"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.source_urls = ["https://evil.com"]  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]


def test_failed_singleton_cannot_be_mutated():
    """The module-level ``_FAILED`` singleton is shared across all failure returns;
    mutating any field on it must raise, not silently poison later results."""
    from app.services.enrichment_worker.web_extractor import _FAILED

    with pytest.raises(dataclasses.FrozenInstanceError):
        _FAILED.status = "web_sourced"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _FAILED.confidence = 0.99  # type: ignore[misc]
    # dataclasses.replace produces a new object, never touching the singleton.
    assert dataclasses.replace(_FAILED, status="web_sourced") is not _FAILED
    assert _FAILED.status == "failed"


def test_prompt_constrains_category_to_canonical_vocabulary():
    """The extracted category routes through the F1 ladder's normalize_category (off-
    vocab → silently dropped), so the prompt must solicit ladder-admissible keys — a
    free-text ``"category": str`` would suppress the web tier's category fill-rate."""
    from app.services.commodity_registry import get_all_commodities
    from app.services.enrichment_worker.web_extractor import _PROMPT

    assert "category MUST be one of" in _PROMPT
    for key in ("hdd", "ssd", "dram"):
        assert key in _PROMPT
    assert all(key in _PROMPT for key in get_all_commodities())
