"""Regression tests: price-break selection must survive an explicit null quantity.

A supplier may return a price-break row whose quantity key is PRESENT but null
(``{"Quantity": null}``). ``dict.get(key, default)`` returns the default only when
the key is MISSING, so a present-but-null value yields ``None`` — and ``min(...,
key=...)`` then compares ``None < int`` and raises ``TypeError``, erroring the WHOLE
part number. The parsers coalesce a null/zero break quantity to a large sentinel
(``... or 999999``) so a null row simply sorts last instead of crashing.

Covers all four price-break sites: DigiKey (StandardPricing), Mouser (PriceBreaks),
Nexar GraphQL (_parse_full) and Nexar REST v4 (_parse_rest_v4).

Called by: pytest
Depends on: app.connectors.{digikey,mouser,sources}
"""

import pytest


def test_digikey_parse_null_break_quantity_does_not_crash():
    from app.connectors.digikey import DigiKeyConnector

    c = DigiKeyConnector(client_id="id", client_secret="secret")
    data = {
        "Products": [
            {
                "ManufacturerPartNumber": "LM317T",
                "Manufacturer": {"Name": "TI"},
                "DigiKeyPartNumber": "DK1",
                "QuantityAvailable": 100,
                # First break has an explicit null BreakQuantity (the crash trigger);
                # the valid break must still win as the lowest real quantity.
                "StandardPricing": [
                    {"BreakQuantity": None, "UnitPrice": 9.99},
                    {"BreakQuantity": 1, "UnitPrice": 0.75},
                ],
                "ProductUrl": "",
                "Description": {"DetailedDescription": "x"},
            }
        ]
    }
    results = c._parse(data, "LM317T")
    assert len(results) == 1
    assert results[0]["unit_price"] == 0.75


def test_digikey_parse_all_null_break_quantities_does_not_crash():
    from app.connectors.digikey import DigiKeyConnector

    c = DigiKeyConnector(client_id="id", client_secret="secret")
    data = {
        "Products": [
            {
                "ManufacturerPartNumber": "X",
                "Manufacturer": {"Name": "M"},
                "DigiKeyPartNumber": "DK1",
                "QuantityAvailable": 1,
                "StandardPricing": [{"BreakQuantity": None, "UnitPrice": 2.50}],
                "ProductUrl": "",
                "Description": {"DetailedDescription": "x"},
            }
        ]
    }
    results = c._parse(data, "X")
    assert results[0]["unit_price"] == 2.50


def test_mouser_parse_null_break_quantity_does_not_crash():
    from app.connectors.mouser import MouserConnector

    c = MouserConnector(api_key="key")
    data = {
        "SearchResults": {
            "Parts": [
                {
                    "ManufacturerPartNumber": "LM317T",
                    "Manufacturer": "TI",
                    "MouserPartNumber": "M-1",
                    "Availability": "100 In Stock",
                    "PriceBreaks": [
                        {"Quantity": None, "Price": "$9.99"},
                        {"Quantity": 1, "Price": "$0.89"},
                    ],
                    "ProductDetailUrl": "",
                    "Description": "",
                }
            ]
        }
    }
    results = c._parse(data, "LM317T")
    assert len(results) == 1
    assert results[0]["unit_price"] == 0.89


def test_nexar_parse_full_null_price_break_quantity_does_not_crash():
    from app.connectors.sources import NexarConnector

    c = NexarConnector(client_id="id", client_secret="secret")
    results_data = [
        {
            "part": {
                "mpn": "LM317T",
                "manufacturer": {"name": "TI"},
                "sellers": [
                    {
                        "company": {"name": "Arrow", "homepageUrl": "https://arrow.com"},
                        "isAuthorized": True,
                        "offers": [
                            {
                                "inventoryLevel": 5000,
                                "prices": [
                                    {"price": 9.99, "currency": "USD", "quantity": None},
                                    {"price": 0.75, "currency": "USD", "quantity": 1},
                                ],
                                "clickUrl": "https://octo.click/abc",
                                "sku": "ARW-1",
                            }
                        ],
                    }
                ],
            }
        }
    ]
    results = c._parse_full(results_data, "LM317T")
    assert results
    assert results[0]["unit_price"] == 0.75


def test_nexar_parse_rest_v4_null_price_break_quantity_does_not_crash():
    from app.connectors.sources import NexarConnector

    c = NexarConnector(client_id="id", client_secret="secret")
    data = {
        "results": [
            {
                "item": {
                    "mpn": "LM317T",
                    "manufacturer": {"name": "TI"},
                    "sellers": [
                        {
                            "company": {"name": "Digi-Key", "homepage_url": "https://digikey.com"},
                            "is_authorized": True,
                            "offers": [
                                {
                                    "in_stock_quantity": 500,
                                    "sku": "DK-1",
                                    "product_url": "https://dk/x",
                                    "prices": [
                                        {"price": 9.99, "currency": "USD", "quantity": None},
                                        {"price": 0.60, "currency": "USD", "quantity": 1},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            }
        ]
    }
    results = c._parse_rest_v4(data, "LM317T")
    assert results
    assert results[0]["unit_price"] == pytest.approx(0.60)
