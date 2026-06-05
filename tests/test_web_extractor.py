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
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_untrusted_domain_rejected(mock_cj):
    mock_cj.return_value = {**_GOOD, "source_urls": ["https://www.ebay.com/itm/1"]}
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_mpn_mismatch_rejected(mock_cj):
    mock_cj.return_value = {**_GOOD, "exact_mpn_found": "LM317MT"}
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_low_confidence_rejected(mock_cj):
    mock_cj.return_value = {**_GOOD, "confidence": 0.80}
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_claude_error_returns_failed(mock_cj):
    mock_cj.side_effect = RuntimeError("claude down")
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"
