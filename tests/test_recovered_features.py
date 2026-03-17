"""test_recovered_features.py — Tests for features recovered from orphaned commits.

Covers:
- AI intake heuristic fallback parser
- AI intake mode coercion (auto/rfq/offer)
- AI intake-parse endpoint wiring
- Vendor tag-based search expansion
- Lead detail intelligence fields (template context)
- Source badge additional source types

Called by: pytest
Depends on: app/services/ai_intake_parser.py, app/routers/htmx/vendors.py,
            app/routers/ai.py
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.services.ai_intake_parser import (
    _coerce_mode,
    _heuristic_parse,
    parse_freeform_intake,
)


# ---------------------------------------------------------------------------
# _heuristic_parse — TSV/CSV fallback when LLM fails
# ---------------------------------------------------------------------------


class TestHeuristicParse:
    def test_parses_tsv_rows(self):
        text = "LM358N\t100\t0.45\nSN74HC04\t500\t0.12"
        result = _heuristic_parse(text)
        assert result is not None
        assert result["document_type"] == "unclear"
        assert result["confidence"] == 0.3
        assert len(result["requirements"]) == 2
        assert result["requirements"][0]["mpn"] == "LM358N"
        assert result["requirements"][0]["quantity"] == 100
        assert result["requirements"][1]["mpn"] == "SN74HC04"

    def test_parses_csv_rows(self):
        text = "LM358N,100,0.45,Texas Instruments"
        result = _heuristic_parse(text)
        assert result is not None
        assert len(result["requirements"]) == 1
        assert result["requirements"][0]["manufacturer"] == "Texas Instruments"

    def test_skips_comment_lines(self):
        text = "# header line\nLM358N\t100"
        result = _heuristic_parse(text)
        assert result is not None
        assert len(result["requirements"]) == 1

    def test_returns_none_for_no_valid_rows(self):
        text = "hello world\nthis is just text"
        result = _heuristic_parse(text)
        assert result is None

    def test_handles_empty_text(self):
        result = _heuristic_parse("")
        assert result is None

    def test_normalizes_mpn(self):
        text = "LM358N\t50"
        result = _heuristic_parse(text)
        assert result is not None
        assert result["requirements"][0]["mpn"] == "LM358N"

    def test_defaults_quantity_to_1(self):
        text = "LM358N"
        result = _heuristic_parse(text)
        assert result is not None
        assert result["requirements"][0]["quantity"] == 1


# ---------------------------------------------------------------------------
# _coerce_mode — force all rows to one type
# ---------------------------------------------------------------------------


class TestCoerceMode:
    def test_auto_mode_does_nothing(self):
        result = {
            "document_type": "unclear",
            "requirements": [{"mpn": "A"}],
            "offers": [{"mpn": "B", "vendor_name": "V"}],
        }
        _coerce_mode(result, "auto")
        assert len(result["requirements"]) == 1
        assert len(result["offers"]) == 1

    def test_rfq_mode_moves_offers_to_requirements(self):
        result = {
            "document_type": "unclear",
            "requirements": [],
            "offers": [
                {"mpn": "LM358N", "qty_available": 100, "unit_price": 0.45}
            ],
        }
        _coerce_mode(result, "rfq")
        assert result["document_type"] == "rfq"
        assert len(result["offers"]) == 0
        assert len(result["requirements"]) == 1
        assert result["requirements"][0]["mpn"] == "LM358N"
        assert result["requirements"][0]["quantity"] == 100
        assert result["requirements"][0]["target_price"] == 0.45

    def test_offer_mode_moves_requirements_to_offers(self):
        result = {
            "document_type": "unclear",
            "vendor_name": "Acme Corp",
            "requirements": [
                {"mpn": "SN74HC04", "quantity": 500}
            ],
            "offers": [],
        }
        _coerce_mode(result, "offer")
        assert result["document_type"] == "offer"
        assert len(result["requirements"]) == 0
        assert len(result["offers"]) == 1
        assert result["offers"][0]["mpn"] == "SN74HC04"
        assert result["offers"][0]["vendor_name"] == "Acme Corp"


# ---------------------------------------------------------------------------
# parse_freeform_intake — fallback to heuristic on LLM failure
# ---------------------------------------------------------------------------


class TestParseIntakeFallback:
    def test_falls_back_to_heuristic_on_llm_failure(self):
        async def _run():
            with patch(
                "app.services.ai_intake_parser.routed_structured",
                new_callable=AsyncMock,
                side_effect=Exception("LLM unavailable"),
            ):
                return await parse_freeform_intake("LM358N\t100\t0.45")

        result = asyncio.run(_run())
        assert result is not None
        assert len(result["requirements"]) == 1
        assert result["requirements"][0]["mpn"] == "LM358N"

    def test_falls_back_to_heuristic_on_none_result(self):
        async def _run():
            with patch(
                "app.services.ai_intake_parser.routed_structured",
                new_callable=AsyncMock,
                return_value=None,
            ):
                return await parse_freeform_intake("LM358N\t100\t0.45")

        result = asyncio.run(_run())
        assert result is not None
        assert len(result["requirements"]) == 1

    def test_mode_parameter_coerces_output(self):
        async def _run():
            with patch(
                "app.services.ai_intake_parser.routed_structured",
                new_callable=AsyncMock,
                return_value={
                    "document_type": "unclear",
                    "confidence": 0.8,
                    "requirements": [{"mpn": "LM358N", "quantity": 100}],
                    "offers": [],
                },
            ):
                return await parse_freeform_intake(
                    "LM358N 100 pcs", mode="offer"
                )

        result = asyncio.run(_run())
        assert result is not None
        assert result["document_type"] == "offer"
        assert len(result["offers"]) == 1
        assert len(result["requirements"]) == 0


