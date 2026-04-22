"""tests/test_ai_intake_parser_coverage.py — Branch coverage for uncovered lines.

Called by: pytest
Depends on: app/services/ai_intake_parser.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

from app.services.ai_intake_parser import _coerce_mode, _heuristic_parse, parse_freeform_intake

# --- Lines 140-142: LLM exception → heuristic fallback ---


async def test_llm_exception_triggers_heuristic_fallback():
    # routed_structured raises → heuristic parser handles TSV text
    tsv_text = "LM317T\t1000\t0.45\tTexasInstruments"
    with patch("app.services.ai_intake_parser.routed_structured", side_effect=RuntimeError("timeout")):
        result = await parse_freeform_intake(tsv_text)

    assert result is not None
    assert result["document_type"] in ("rfq", "unclear")
    assert len(result["requirements"]) == 1
    assert result["requirements"][0]["mpn"] == "LM317T"


async def test_llm_exception_with_unparseable_text_returns_none():
    # routed_structured raises and heuristic also finds nothing → None
    with patch("app.services.ai_intake_parser.routed_structured", side_effect=ValueError("bad")):
        result = await parse_freeform_intake("no parts here at all")

    assert result is None


# --- Line 236: offer row with empty mpn → skipped ---


async def test_offer_row_with_empty_mpn_is_skipped():
    mock_result = {
        "document_type": "offer",
        "confidence": 0.8,
        "vendor_name": "Acme",
        "requirements": [],
        "offers": [
            {"mpn": "", "qty_available": 100, "unit_price": 0.50},
            {"mpn": None, "qty_available": 200, "unit_price": 0.75},
            {"mpn": "LM7805", "qty_available": 500, "unit_price": 0.30},
        ],
    }
    with patch("app.services.ai_intake_parser.routed_structured", AsyncMock(return_value=mock_result)):
        result = await parse_freeform_intake("offer text")

    assert result is not None
    # Only the row with a real MPN survives
    assert len(result["offers"]) == 1
    assert result["offers"][0]["mpn"] == "LM7805"


# --- Lines 303-318: _coerce_mode with mode="rfq" ---


def test_coerce_mode_rfq_moves_offers_to_requirements():
    result = {
        "document_type": "offer",
        "requirements": [],
        "offers": [
            {
                "mpn": "TL431",
                "qty_available": 500,
                "unit_price": 0.10,
                "manufacturer": "TI",
                "condition": "new",
                "date_code": "2301",
                "packaging": "reel",
                "notes": "in stock",
            }
        ],
    }
    _coerce_mode(result, "rfq")

    assert result["document_type"] == "rfq"
    assert result["offers"] == []
    assert len(result["requirements"]) == 1
    req = result["requirements"][0]
    assert req["mpn"] == "TL431"
    assert req["quantity"] == 500
    assert req["target_price"] == 0.10
    assert req["manufacturer"] == "TI"


def test_coerce_mode_rfq_uses_qty_fallback_when_no_qty_available():
    result = {
        "document_type": "offer",
        "requirements": [],
        "offers": [{"mpn": "NE555", "qty_available": None, "unit_price": None}],
    }
    _coerce_mode(result, "rfq")

    assert result["requirements"][0]["quantity"] == 1


# --- Lines 320-340: _coerce_mode with mode="offer" ---


def test_coerce_mode_offer_moves_requirements_to_offers():
    result = {
        "document_type": "rfq",
        "vendor_name": "SupplierX",
        "requirements": [
            {
                "mpn": "LM317T",
                "quantity": 1000,
                "target_price": 0.45,
                "manufacturer": "TI",
                "condition": "new",
                "date_codes": "2301",
                "packaging": "reel",
                "notes": "urgent",
            }
        ],
        "offers": [],
    }
    _coerce_mode(result, "offer")

    assert result["document_type"] == "offer"
    assert result["requirements"] == []
    assert len(result["offers"]) == 1
    offer = result["offers"][0]
    assert offer["mpn"] == "LM317T"
    assert offer["vendor_name"] == "SupplierX"
    assert offer["qty_available"] == 1000
    assert offer["unit_price"] == 0.45
    assert offer["currency"] == "USD"
    assert offer["lead_time"] is None
    assert offer["moq"] is None


def test_coerce_mode_auto_is_no_op():
    result = {
        "document_type": "rfq",
        "requirements": [{"mpn": "ABC123"}],
        "offers": [],
    }
    original_reqs = list(result["requirements"])
    _coerce_mode(result, "auto")

    assert result["requirements"] == original_reqs
    assert result["document_type"] == "rfq"


# --- Lines 358, 362: heuristic parser skips blank and comment lines ---


def test_heuristic_parse_skips_blank_lines():
    text = "\n\n\nLM317T\t100\n\n"
    result = _heuristic_parse(text)

    assert result is not None
    assert len(result["requirements"]) == 1


def test_heuristic_parse_skips_hash_comment_lines():
    text = "# this is a header\nLM317T\t500\n// another comment"
    result = _heuristic_parse(text)

    assert result is not None
    assert len(result["requirements"]) == 1
    assert result["requirements"][0]["mpn"] == "LM317T"


# --- Lines 368-380: heuristic parser with qty/price/manufacturer columns ---


def test_heuristic_parse_single_column_mpn_only():
    # len(cells) == 1: qty stays None → defaults to 1
    result = _heuristic_parse("LM317T")

    assert result is not None
    assert result["requirements"][0]["quantity"] == 1
    assert result["requirements"][0]["target_price"] is None
    assert result["requirements"][0]["manufacturer"] is None


def test_heuristic_parse_two_columns_mpn_and_qty():
    # len(cells) > 1: qty parsed
    result = _heuristic_parse("LM317T\t250")

    assert result is not None
    assert result["requirements"][0]["quantity"] == 250
    assert result["requirements"][0]["target_price"] is None


def test_heuristic_parse_three_columns_mpn_qty_price():
    # len(cells) > 2: price also parsed
    result = _heuristic_parse("LM317T\t250\t0.99")

    assert result is not None
    assert result["requirements"][0]["quantity"] == 250
    assert result["requirements"][0]["target_price"] == 0.99
    assert result["requirements"][0]["manufacturer"] is None


def test_heuristic_parse_four_columns_includes_manufacturer():
    # len(cells) > 3: manufacturer also parsed
    result = _heuristic_parse("LM317T\t250\t0.99\tTexas Instruments")

    assert result is not None
    assert result["requirements"][0]["manufacturer"] == "Texas Instruments"


# --- Line 396: _heuristic_parse returns full result dict ---


def test_heuristic_parse_returns_full_result_structure():
    result = _heuristic_parse("NE555\t1000\t0.20")

    assert result is not None
    assert "document_type" in result
    assert result["document_type"] == "unclear"
    assert "confidence" in result
    assert result["confidence"] == 0.3
    assert "summary" in result
    assert "1 row" in result["summary"]
    assert "requirements" in result
    assert result["offers"] == []
    assert result["requisition_name"] is None


def test_heuristic_parse_no_valid_rows_returns_none():
    result = _heuristic_parse("# comment\n\n   \n// another")

    assert result is None


# --- Line 236: offer row that is not a dict → skipped via continue ---


async def test_offer_row_not_a_dict_is_skipped():
    # _normalize_offers skips rows that aren't dicts (line 235-236)
    mock_result = {
        "document_type": "offer",
        "confidence": 0.8,
        "vendor_name": "Acme",
        "requirements": [],
        "offers": [
            "not-a-dict",
            42,
            {"mpn": "LM7805", "qty_available": 100, "unit_price": 0.30},
        ],
    }
    with patch("app.services.ai_intake_parser.routed_structured", AsyncMock(return_value=mock_result)):
        result = await parse_freeform_intake("offer text")

    assert result is not None
    assert len(result["offers"]) == 1
    assert result["offers"][0]["mpn"] == "LM7805"
