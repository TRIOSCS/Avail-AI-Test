"""tests/test_vendor_spec_map.py — the per-commodity vendor-attribute alias map.

Drives ``app.connectors._vendor_spec_map.extract_vendor_specs``: an Element14-shaped
``attributes`` list ([{attributeLabel, attributeValue}, …]) is mapped to a normalized
``specs`` dict keyed by SEEDED spec_keys, with unmapped attributes observable in
``dropped`` and the known enum-format gaps (± spacing, [Metric] case suffix) closed so a
correct value reaches its seed enum. Pure — no DB, no network.
"""

from app.connectors._vendor_spec_map import VENDOR_SPEC_MAP, extract_vendor_specs

_E14_KEYS = {"name_key": "attributeLabel", "value_key": "attributeValue"}


def _attrs(pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"attributeLabel": k, "attributeValue": v} for k, v in pairs]


def test_capacitor_attributes_map_to_seeded_keys():
    attrs = _attrs(
        [
            ("Capacitance", "0.1µF"),
            ("Voltage Rating DC", "16V"),
            ("Dielectric", "X7R"),
            ("Capacitance Tolerance", "± 10%"),
            ("Case Style", "0402 [1005 Metric]"),
            ("Operating Temperature Min", "-55°C"),  # unmapped → dropped
        ]
    )
    specs, dropped = extract_vendor_specs(attrs, "capacitors", **_E14_KEYS)

    assert specs == {
        "capacitance": "0.1µF",
        "voltage_rating": "16V",
        "dielectric": "X7R",
        "tolerance": "±10%",  # whitespace collapsed to the seed spelling
        "package": "0402",  # [1005 Metric] suffix stripped
    }
    assert dropped == {"Operating Temperature Min": "-55°C"}


def test_resistor_tolerance_dropped_to_bare_seed_spelling():
    # Element14 reports resistor tolerance WITH the ± sign (the same shape as caps), but the
    # resistor seed enum is BARE ("1%"). The commodity-aware normalizer must DROP the sign
    # so the value lands in the resistor enum (else the headline tolerance facet never fills).
    attrs = _attrs(
        [
            ("Resistance", "10kΩ"),
            ("Power Rating", "0.1W"),
            ("Resistance Tolerance", "± 1%"),
            ("Case Style", "0603 [1608 Metric]"),
        ]
    )
    specs, dropped = extract_vendor_specs(attrs, "resistors", **_E14_KEYS)

    assert specs == {
        "resistance": "10kΩ",
        "power_rating": "0.1W",
        "tolerance": "1%",  # ± dropped → matches the bare resistor seed enum
        "package": "0603",
    }
    assert dropped == {}


def test_tolerance_sign_follows_seed_convention_not_vendor():
    # The seed convention wins over the vendor's sign style, per commodity. A sign-less
    # capacitor tolerance gets the ± (cap seed carries it); a signed resistor tolerance
    # loses it (resistor seed is bare). Either way the value reaches its enum.
    cap_specs, _ = extract_vendor_specs(_attrs([("Capacitance Tolerance", "10 %")]), "capacitors", **_E14_KEYS)
    assert cap_specs == {"tolerance": "±10%"}

    res_specs, _ = extract_vendor_specs(_attrs([("Resistance Tolerance", "± 5%")]), "resistors", **_E14_KEYS)
    assert res_specs == {"tolerance": "5%"}


def test_case_code_letter_suffix_and_five_digit():
    # "0402M" → "0402" (letter boundary now handled); a 5-digit code is left intact (not
    # truncated to its first 4 digits) so it can be dropped honestly by the enum gate.
    cap, _ = extract_vendor_specs(_attrs([("Case Style", "0402M")]), "capacitors", **_E14_KEYS)
    assert cap == {"package": "0402"}
    five, _ = extract_vendor_specs(_attrs([("Case Style", "01005")]), "capacitors", **_E14_KEYS)
    assert five == {"package": "01005"}  # left intact (not mangled to "0100")


def test_second_distinct_attribute_sharing_a_key_alias_lands_in_dropped():
    # voltage_rating aliases include both "Voltage Rating DC" and "Voltage Rating AC". Only
    # the first present one supplies the value; the OTHER, distinct attribute must still be
    # observable in `dropped` (not silently suppressed).
    attrs = _attrs([("Voltage Rating DC", "16V"), ("Voltage Rating AC", "10V")])
    specs, dropped = extract_vendor_specs(attrs, "capacitors", **_E14_KEYS)
    assert specs == {"voltage_rating": "16V"}
    assert dropped == {"Voltage Rating AC": "10V"}


def test_unmapped_commodity_returns_empty():
    attrs = _attrs([("Capacitance", "0.1µF")])
    assert extract_vendor_specs(attrs, "dram", **_E14_KEYS) == ({}, {})
    assert extract_vendor_specs(attrs, None, **_E14_KEYS) == ({}, {})


def test_non_list_attrs_returns_empty():
    assert extract_vendor_specs(None, "capacitors", **_E14_KEYS) == ({}, {})
    assert extract_vendor_specs({}, "capacitors", **_E14_KEYS) == ({}, {})


def test_placeholder_dash_values_dropped_not_mapped():
    # generic_attribute treats "-" as missing; a placeholder neither maps nor appears in dropped.
    attrs = _attrs([("Capacitance", "-"), ("Voltage Rating DC", "50V")])
    specs, dropped = extract_vendor_specs(attrs, "capacitors", **_E14_KEYS)
    assert specs == {"voltage_rating": "50V"}
    assert "Capacitance" not in dropped


def test_map_only_lists_seeded_commodities():
    # Guard: every commodity in the map is a real seeded commodity (no typo'd key).
    import json
    from pathlib import Path

    seeds = json.loads(Path("app/data/commodity_seeds.json").read_text())
    for commodity, key_map in VENDOR_SPEC_MAP.items():
        assert commodity in seeds, f"{commodity} not in commodity_seeds.json"
        seeded_keys = {s["spec_key"] for s in seeds[commodity]}
        for spec_key in key_map:
            assert spec_key in seeded_keys, f"{commodity}.{spec_key} not a seeded key"
