"""Tests for TME connector — parse logic, signing, and error handling."""

import pytest
from app.connectors.tme import TMEConnector


@pytest.fixture
def connector():
    return TMEConnector(token="test-token", secret="test-secret")


SAMPLE_SEARCH_PRODUCTS = [
    {
        "Symbol": "LM358N/NOPB",
        "OriginalSymbol": "LM358N",
        "Producer": "Texas Instruments",
        "Description": "Dual operational amplifier",
        "QuantityAvailable": 1200,
    },
    {
        "Symbol": "LM358NG-ON",
        "OriginalSymbol": "LM358NG",
        "Producer": "ON Semiconductor",
        "Description": "Dual Op Amp",
        "QuantityAvailable": 0,
    },
]

SAMPLE_PRICES_MAP = {
    "LM358N/NOPB": 0.45,
    "LM358NG-ON": 0.38,
}


def test_parse_returns_correct_count(connector):
    results = connector._parse(SAMPLE_SEARCH_PRODUCTS, SAMPLE_PRICES_MAP, "LM358N")
    assert len(results) == 2


def test_parse_field_mapping(connector):
    results = connector._parse(SAMPLE_SEARCH_PRODUCTS, SAMPLE_PRICES_MAP, "LM358N")
    r = results[0]
    assert r["vendor_name"] == "TME"
    assert r["manufacturer"] == "Texas Instruments"
    assert r["mpn_matched"] == "LM358N"
    assert r["qty_available"] == 1200
    assert r["unit_price"] == 0.45
    assert r["currency"] == "USD"
    assert r["source_type"] == "tme"
    assert r["is_authorized"] is True
    assert r["vendor_sku"] == "LM358N/NOPB"
    assert "tme.eu" in r["click_url"]
    assert "LM358N/NOPB" in r["click_url"]


def test_parse_confidence_in_stock(connector):
    results = connector._parse(SAMPLE_SEARCH_PRODUCTS, SAMPLE_PRICES_MAP, "LM358N")
    assert results[0]["confidence"] == 5  # qty > 0
    assert results[1]["confidence"] == 3  # qty == 0


def test_parse_empty_products(connector):
    results = connector._parse([], {}, "TEST123")
    assert results == []


def test_parse_no_prices(connector):
    results = connector._parse(SAMPLE_SEARCH_PRODUCTS, {}, "LM358N")
    assert len(results) == 2
    assert results[0]["unit_price"] is None
    assert results[1]["unit_price"] is None


def test_parse_missing_optional_fields(connector):
    products = [{"Symbol": "X123", "QuantityAvailable": 10}]
    results = connector._parse(products, {}, "X123")
    assert len(results) == 1
    assert results[0]["mpn_matched"] == "X123"
    assert results[0]["manufacturer"] == ""


def test_sign_produces_signature(connector):
    params = {"SearchPlain": "LM358N", "Country": "US"}
    signed = connector._sign("https://api.tme.eu/Products/Search.json", params)
    assert "Token" in signed
    assert "ApiSignature" in signed
    assert signed["Token"] == "test-token"
    # Signature should be base64
    import base64
    base64.b64decode(signed["ApiSignature"])  # Should not raise


def test_sign_is_deterministic(connector):
    params = {"SearchPlain": "LM358N", "Country": "US"}
    url = "https://api.tme.eu/Products/Search.json"
    sig1 = connector._sign(url, params)["ApiSignature"]
    sig2 = connector._sign(url, params)["ApiSignature"]
    assert sig1 == sig2


def test_no_credentials_returns_empty():
    conn = TMEConnector(token="", secret="")
    import asyncio
    results = asyncio.get_event_loop().run_until_complete(conn._do_search("LM358N"))
    assert results == []
