"""Tests for element14/Newark connector — parse logic and error handling."""

import pytest
from app.connectors.element14 import Element14Connector


@pytest.fixture
def connector():
    return Element14Connector(api_key="test-key")


SAMPLE_RESPONSE = {
    "manufacturerPartNumberSearchReturn": {
        "numberOfResults": 2,
        "products": [
            {
                "brandName": "Texas Instruments",
                "translatedManufacturerPartNumber": "LM358N",
                "displayName": "Dual Op Amp, 1MHz",
                "sku": "12AB3456",
                "stock": {"level": 500},
                "prices": [
                    {"cost": "0.52", "from": 1, "to": 9},
                    {"cost": "0.45", "from": 10, "to": 99},
                ],
            },
            {
                "brandName": "ON Semiconductor",
                "translatedManufacturerPartNumber": "LM358NG",
                "displayName": "Dual Op Amp, General Purpose",
                "sku": "78CD9012",
                "stock": {"level": 0},
                "prices": [{"cost": "0.38", "from": 1, "to": 9}],
            },
        ],
    }
}


def test_parse_returns_correct_count(connector):
    results = connector._parse(SAMPLE_RESPONSE, "LM358N")
    assert len(results) == 2


def test_parse_field_mapping(connector):
    results = connector._parse(SAMPLE_RESPONSE, "LM358N")
    r = results[0]
    assert r["vendor_name"] == "element14"
    assert r["manufacturer"] == "Texas Instruments"
    assert r["mpn_matched"] == "LM358N"
    assert r["qty_available"] == 500
    assert r["unit_price"] == 0.52
    assert r["currency"] == "USD"
    assert r["source_type"] == "element14"
    assert r["is_authorized"] is True
    assert r["vendor_sku"] == "12AB3456"
    assert "newark.com" in r["click_url"]
    assert r["description"] == "Dual Op Amp, 1MHz"


def test_parse_confidence_in_stock(connector):
    results = connector._parse(SAMPLE_RESPONSE, "LM358N")
    assert results[0]["confidence"] == 5  # qty > 0
    assert results[1]["confidence"] == 3  # qty == 0


def test_parse_empty_response(connector):
    results = connector._parse({}, "TEST123")
    assert results == []


def test_parse_no_products(connector):
    data = {"manufacturerPartNumberSearchReturn": {"products": []}}
    results = connector._parse(data, "TEST123")
    assert results == []


def test_parse_missing_optional_fields(connector):
    data = {
        "manufacturerPartNumberSearchReturn": {
            "products": [
                {
                    "translatedManufacturerPartNumber": "ABC123",
                    "brandName": "",
                    "displayName": "",
                    "sku": "",
                }
            ]
        }
    }
    results = connector._parse(data, "ABC123")
    assert len(results) == 1
    assert results[0]["mpn_matched"] == "ABC123"
    assert results[0]["qty_available"] is None
    assert results[0]["unit_price"] is None


@pytest.mark.asyncio
async def test_no_api_key_returns_empty():
    conn = Element14Connector(api_key="")
    results = await conn._do_search("LM358N")
    assert results == []
