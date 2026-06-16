"""Tests for app/connectors/_core_attrs.py shared helpers and connector wiring.

All tests use synthetic raw-response dicts matching the documented API shapes. No live
network or DB required.
"""

import pytest

from app.connectors._core_attrs import (
    clean_str,
    digikey_parameter,
    generic_attribute,
    map_lifecycle,
    map_rohs,
    safe_pin_count,
)

# ── _core_attrs helpers ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Active", "active"),
        ("Not For New Designs", "nrfnd"),
        ("Obsolete", "obsolete"),
        ("totally unknown status", None),
        (None, None),
        ("NRND", "nrfnd"),
        ("Discontinued", "obsolete"),
        ("Last Time Buy", "ltb"),
        ("End Of Life", "eol"),
        ("EOL", "eol"),
        ("", None),
    ],
)
def test_lifecycle_mapping(value, expected):
    assert map_lifecycle(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ROHS Compliant", "compliant"),
        ("Non-Compliant", "non-compliant"),
        ("weird", None),
        ("compliant", "compliant"),
        ("RoHS3 Compliant", "compliant"),
        ("Not Compliant", "non-compliant"),
        ("RoHS Exempt", "exempt"),
        ("Exempt", "exempt"),
        (None, None),
        ("", None),
    ],
)
def test_rohs_mapping(value, expected):
    assert map_rohs(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("64", 64),
        ("0", None),
        ("abc", None),
        (32, 32),
        (-1, None),
        (None, None),
        ("  128  ", 128),
    ],
)
def test_pin_count(value, expected):
    assert safe_pin_count(value) == expected


@pytest.mark.parametrize(
    ("value", "maxlen", "expected"),
    [
        ("  hello  ", 100, "hello"),
        ("toolongstring", 5, "toolo"),
        (None, 100, None),
        ("  ", 100, None),
        (42, 100, "42"),
    ],
)
def test_clean_str(value, maxlen, expected):
    assert clean_str(value, maxlen=maxlen) == expected


def test_digikey_parameter():
    params = [
        {"ParameterText": "Package / Case", "ValueText": "100-LQFP"},
        {"ParameterText": "Number of I/O", "ValueText": "82"},
    ]
    assert digikey_parameter(params, ("Package / Case",)) == "100-LQFP"
    assert digikey_parameter(params, ("Mounting Type",)) is None
    assert digikey_parameter([], ("Package / Case",)) is None


def test_digikey_parameter_dash_value():
    """A '-' ValueText should be treated as missing."""
    params = [{"ParameterText": "Package / Case", "ValueText": "-"}]
    assert digikey_parameter(params, ("Package / Case",)) is None


def test_digikey_parameter_case_insensitive():
    params = [{"ParameterText": "number of terminations", "ValueText": "64"}]
    assert digikey_parameter(params, ("Number of Terminations",)) == "64"


def test_digikey_parameter_not_list():
    assert digikey_parameter(None, ("Package / Case",)) is None
    assert digikey_parameter("not a list", ("Package / Case",)) is None


def test_generic_attribute_mouser_style():
    """Mouser ProductAttributes use AttributeName / AttributeValue."""
    attrs = [
        {"AttributeName": "Package", "AttributeValue": "SOIC-8"},
        {"AttributeName": "Number of Pins", "AttributeValue": "8"},
    ]
    assert generic_attribute(attrs, "AttributeName", "AttributeValue", ("Package",)) == "SOIC-8"
    assert generic_attribute(attrs, "AttributeName", "AttributeValue", ("Number of Pins",)) == "8"
    assert generic_attribute(attrs, "AttributeName", "AttributeValue", ("Missing",)) is None


def test_generic_attribute_element14_style():
    """Element14 attributes use attributeLabel / attributeValue."""
    attrs = [
        {"attributeLabel": "RoHS", "attributeValue": "Compliant"},
        {"attributeLabel": "Package", "attributeValue": "DIP-8"},
    ]
    assert generic_attribute(attrs, "attributeLabel", "attributeValue", ("RoHS",)) == "Compliant"
    assert generic_attribute(attrs, "attributeLabel", "attributeValue", ("Package",)) == "DIP-8"


def test_generic_attribute_case_insensitive():
    attrs = [{"AttributeName": "PACKAGE", "AttributeValue": "QFN-32"}]
    assert generic_attribute(attrs, "AttributeName", "AttributeValue", ("package",)) == "QFN-32"


def test_generic_attribute_not_list():
    assert generic_attribute(None, "AttributeName", "AttributeValue", ("Package",)) is None
    assert generic_attribute("not a list", "AttributeName", "AttributeValue", ("Package",)) is None


def test_generic_attribute_dash_value():
    attrs = [{"AttributeName": "Package", "AttributeValue": "-"}]
    assert generic_attribute(attrs, "AttributeName", "AttributeValue", ("Package",)) is None


# ── DigiKey connector wiring ──────────────────────────────────────────


def _make_digikey_prod(**overrides):
    """Build a synthetic DigiKey product dict with core-attribute fields."""
    base = {
        "ManufacturerPartNumber": "STM32F407VGT6",
        "Manufacturer": {"Name": "STMicroelectronics"},
        "DigiKeyPartNumber": "497-11802-ND",
        "QuantityAvailable": 1000,
        "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 12.50}],
        "ProductUrl": "https://www.digikey.com/product/497-11802-ND",
        "Description": {"DetailedDescription": "IC MCU 32BIT 1MB FLASH 100LQFP"},
        "Category": {"Name": "Embedded - Microcontrollers"},
        "ProductStatus": {"Status": "Active"},
        "Parameters": [
            {"ParameterText": "Package / Case", "ValueText": "100-LQFP"},
            {"ParameterText": "Number of I/O", "ValueText": "82"},
        ],
        "Classifications": {"RohsStatus": "ROHS Compliant"},
    }
    base.update(overrides)
    return base


def test_digikey_core_attrs_extracted():
    from app.connectors.digikey import DigiKeyConnector

    c = DigiKeyConnector(client_id="x", client_secret="y")
    data = {"Products": [_make_digikey_prod()]}
    results = c._parse(data, "STM32F407VGT6")
    assert len(results) == 1
    r = results[0]
    assert r["category"] == "Embedded - Microcontrollers"
    assert r["lifecycle_status"] == "active"
    assert r["package_type"] == "100-LQFP"
    assert r["pin_count"] == 82
    assert r["rohs_status"] == "compliant"


def test_digikey_core_attrs_missing_fields():
    """Missing optional fields should yield None — not crash."""
    from app.connectors.digikey import DigiKeyConnector

    c = DigiKeyConnector(client_id="x", client_secret="y")
    prod = {
        "ManufacturerPartNumber": "X",
        "Manufacturer": {"Name": "M"},
        "DigiKeyPartNumber": "D1",
        "QuantityAvailable": 10,
        "ProductUrl": "https://digikey.com/x",
        "Description": {"DetailedDescription": "Test"},
        # No Category, ProductStatus, Parameters, Classifications
    }
    results = c._parse({"Products": [prod]}, "X")
    r = results[0]
    assert r["category"] is None
    assert r["lifecycle_status"] is None
    assert r["package_type"] is None
    assert r["pin_count"] is None
    assert r["rohs_status"] is None


def test_digikey_unknown_lifecycle_yields_none():
    from app.connectors.digikey import DigiKeyConnector

    c = DigiKeyConnector(client_id="x", client_secret="y")
    prod = _make_digikey_prod(ProductStatus={"Status": "Some Exotic Status"})
    results = c._parse({"Products": [prod]}, "STM32F407VGT6")
    assert results[0]["lifecycle_status"] is None


# ── Mouser connector wiring ───────────────────────────────────────────


def _make_mouser_part(**overrides):
    base = {
        "ManufacturerPartNumber": "STM32F407VGT6",
        "Manufacturer": "STMicroelectronics",
        "MouserPartNumber": "511-STM32F407VGT6",
        "Availability": "500 In Stock",
        "PriceBreaks": [{"Quantity": 1, "Price": "$12.50"}],
        "ProductDetailUrl": "https://mouser.com/ProductDetail/STM32F407VGT6",
        "Description": "MCU ARM Cortex-M4",
        "Category": "Microcontrollers",
        "LifecycleStatus": "Active",
        "ProductAttributes": [
            {"AttributeName": "Package / Case", "AttributeValue": "100-LQFP"},
            {"AttributeName": "Number of Pins", "AttributeValue": "100"},
        ],
    }
    base.update(overrides)
    return base


def test_mouser_core_attrs_extracted():
    from app.connectors.mouser import MouserConnector

    c = MouserConnector(api_key="key")
    data = {"SearchResults": {"Parts": [_make_mouser_part()]}}
    results = c._parse(data, "STM32F407VGT6")
    assert len(results) == 1
    r = results[0]
    assert r["category"] == "Microcontrollers"
    assert r["lifecycle_status"] == "active"
    assert r["package_type"] == "100-LQFP"
    assert r["pin_count"] == 100


def test_mouser_core_attrs_missing():
    from app.connectors.mouser import MouserConnector

    c = MouserConnector(api_key="key")
    part = {
        "ManufacturerPartNumber": "X",
        "Manufacturer": "M",
        "MouserPartNumber": "M-X",
        "Availability": "10 In Stock",
        "PriceBreaks": [],
        "ProductDetailUrl": "",
        "Description": "",
        # No Category, LifecycleStatus, ProductAttributes
    }
    results = c._parse({"SearchResults": {"Parts": [part]}}, "X")
    r = results[0]
    assert r["category"] is None
    assert r["lifecycle_status"] is None
    assert r["package_type"] is None
    assert r["pin_count"] is None


# ── Element14 connector wiring ────────────────────────────────────────


def _make_element14_prod(**overrides):
    base = {
        "translatedManufacturerPartNumber": "LM317T",
        "brandName": "Texas Instruments",
        "displayName": "IC REG LINEAR 1.2V",
        "sku": "12345",
        "stock": {"level": 500},
        "prices": [{"cost": "0.65"}],
        "attributes": [
            {"attributeLabel": "RoHS", "attributeValue": "Compliant"},
            {"attributeLabel": "Package", "attributeValue": "TO-220-3"},
        ],
    }
    base.update(overrides)
    return base


def test_element14_core_attrs_extracted():
    from app.connectors.element14 import Element14Connector

    c = Element14Connector(api_key="key")
    data = {"manufacturerPartNumberSearchReturn": {"products": [_make_element14_prod()]}}
    results = c._parse(data, "LM317T")
    assert len(results) == 1
    r = results[0]
    assert r["rohs_status"] == "compliant"
    assert r["package_type"] == "TO-220-3"


def test_element14_core_attrs_missing():
    from app.connectors.element14 import Element14Connector

    c = Element14Connector(api_key="key")
    prod = {
        "translatedManufacturerPartNumber": "X",
        "brandName": "M",
        "displayName": "D",
        "sku": "S",
        "stock": {},
        "prices": [],
        # No attributes
    }
    data = {"manufacturerPartNumberSearchReturn": {"products": [prod]}}
    results = c._parse(data, "X")
    r = results[0]
    assert r["rohs_status"] is None
    assert r["package_type"] is None


# ── OEMSecrets connector wiring ───────────────────────────────────────


def _make_oemsecrets_item(**overrides):
    base = {
        "distributor": {"distributor_name": "Arrow"},
        "manufacturer": "TI",
        "source_part_number": "LM317T/NOPB",
        "quantity_in_stock": 5000,
        "prices": {"USD": [{"unit_break": 1, "unit_price": "0.89"}]},
        "buy_now_url": "https://arrow.com/buy",
        "datasheet_url": "https://ti.com/ds.pdf",
        "distributor_authorisation_status": "authorised",
        "category": "Linear Regulators",
        "lifecycle_status": "Active",
    }
    base.update(overrides)
    return base


def test_oemsecrets_core_attrs_extracted():
    from app.connectors.oemsecrets import OEMSecretsConnector

    c = OEMSecretsConnector(api_key="key")
    data = {"stock": [_make_oemsecrets_item()]}
    results = c._parse(data, "LM317T")
    assert len(results) == 1
    r = results[0]
    assert r["category"] == "Linear Regulators"
    assert r["lifecycle_status"] == "active"


def test_oemsecrets_core_attrs_missing():
    from app.connectors.oemsecrets import OEMSecretsConnector

    c = OEMSecretsConnector(api_key="key")
    item = {
        "distributor": {"distributor_name": "Broker X"},
        "source_part_number": "X",
        "quantity_in_stock": 100,
        "prices": {"USD": [{"unit_break": 1, "unit_price": "1.50"}]},
        # No category or lifecycle_status
    }
    results = c._parse({"stock": [item]}, "X")
    r = results[0]
    assert r["category"] is None
    assert r["lifecycle_status"] is None


def test_oemsecrets_unknown_lifecycle_yields_none():
    from app.connectors.oemsecrets import OEMSecretsConnector

    c = OEMSecretsConnector(api_key="key")
    item = _make_oemsecrets_item(lifecycle_status="Exotic Status")
    results = c._parse({"stock": [item]}, "LM317T")
    assert results[0]["lifecycle_status"] is None


# ── Nexar connector wiring ────────────────────────────────────────────


def _make_nexar_full_hit(category_name: str | None = "Microcontrollers"):
    """Build a synthetic Nexar FULL_QUERY result hit."""
    part: dict = {
        "mpn": "STM32F407VGT6",
        "manufacturer": {"name": "STMicroelectronics"},
        "sellers": [
            {
                "company": {"name": "Arrow", "homepageUrl": "https://arrow.com"},
                "isAuthorized": True,
                "offers": [
                    {
                        "inventoryLevel": 500,
                        "prices": [{"price": "12.50", "currency": "USD", "quantity": 1}],
                        "clickUrl": "https://arrow.com/buy",
                        "sku": "ARW-STM32",
                    }
                ],
            }
        ],
    }
    if category_name is not None:
        part["category"] = {"name": category_name}
    return {"part": part}


def test_nexar_full_query_category_extracted():
    from app.connectors.sources import NexarConnector

    c = NexarConnector(client_id="x", client_secret="y")
    hits = [_make_nexar_full_hit("Microcontrollers")]
    results = c._parse_full(hits, "STM32F407VGT6")
    assert len(results) >= 1
    # All results from the same part share the same category
    for r in results:
        assert r["category"] == "Microcontrollers"


def test_nexar_full_query_no_category_yields_none():
    from app.connectors.sources import NexarConnector

    c = NexarConnector(client_id="x", client_secret="y")
    hits = [_make_nexar_full_hit(category_name=None)]
    results = c._parse_full(hits, "STM32F407VGT6")
    assert len(results) >= 1
    for r in results:
        assert r["category"] is None


def test_nexar_aggregate_query_category_extracted():
    from app.connectors.sources import NexarConnector

    c = NexarConnector(client_id="x", client_secret="y")
    hits = [
        {
            "part": {
                "mpn": "LM317T",
                "manufacturer": {"name": "TI"},
                "totalAvail": 5000,
                "medianPrice1000": {"price": "0.89", "currency": "USD"},
                "shortDescription": "Voltage Regulator",
                "category": {"name": "Linear Regulators"},
                "octopartUrl": "https://octopart.com/lm317t",
                "manufacturerUrl": "",
            }
        }
    ]
    results = c._parse_aggregate(hits, "LM317T")
    assert len(results) == 1
    assert results[0]["category"] == "Linear Regulators"
