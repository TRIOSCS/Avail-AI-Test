"""tests/test_taxonomy_seed_expansion.py — TRIO taxonomy seed expansion.

What: Asserts the taxonomy seed expansion landed exactly as specified — tape_drives is a
      fully-seeded new commodity; dram/ssd/hdd/motherboards/power_supplies/connectors
      carry their matrix extension specs; connectors no longer ships the retired
      open-vocab 'series' spec; and extension rows on already-seeded commodities reach
      an existing DB through the insert-only boot seeder.
Called by: pytest (CI + local).
Depends on: app/data/commodity_seeds.json, app/services/commodity_registry.py,
            conftest.py (db_session fixture).
"""

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema
from app.services.commodity_registry import COMMODITY_SPEC_SEEDS, seed_commodity_schemas


def _spec(commodity: str, key: str) -> dict:
    matches = [s for s in COMMODITY_SPEC_SEEDS[commodity] if s["spec_key"] == key]
    assert matches, f"{commodity}/{key} missing from seeds"
    return matches[0]


# ── tape_drives: new fully-seeded commodity ─────────────────────────


def test_tape_drives_spec_set():
    keys = [s["spec_key"] for s in COMMODITY_SPEC_SEEDS["tape_drives"]]
    assert keys == ["drive_type", "interface", "form_factor", "native_capacity_gb", "encryption"]

    drive_type = _spec("tape_drives", "drive_type")
    assert drive_type["is_primary"] is True
    assert drive_type["enum_values"] == [
        "LTO-5", "LTO-6", "LTO-7", "LTO-8", "LTO-9",
        "TS1140", "TS1150", "TS1155", "TS1160", "DAT", "AIT",
    ]  # fmt: skip

    interface = _spec("tape_drives", "interface")
    assert interface["is_primary"] is True
    assert interface["enum_values"] == ["FC", "SAS", "SCSI"]

    assert _spec("tape_drives", "form_factor")["enum_values"] == ["Full-Height", "Half-Height", "Library Module"]

    capacity = _spec("tape_drives", "native_capacity_gb")
    assert capacity["data_type"] == "numeric"
    assert capacity["unit"] == "GB"
    assert capacity["canonical_unit"] == "GB"

    assert _spec("tape_drives", "encryption")["data_type"] == "boolean"


# ── matrix extensions on already-seeded commodities ──────────────────


def test_dram_extensions():
    assert _spec("dram", "rank")["enum_values"] == ["1Rx4", "1Rx8", "2Rx4", "2Rx8", "4Rx4", "8Rx4"]
    assert _spec("dram", "registered")["enum_values"] == [
        "Unbuffered",
        "Registered",
        "Load-Reduced",
        "Fully-Buffered",
    ]
    voltage = _spec("dram", "voltage")
    assert voltage["data_type"] == "numeric"
    assert voltage["unit"] == "V"
    assert voltage["canonical_unit"] == "V"


def test_ssd_extensions():
    endurance = _spec("ssd", "endurance_dwpd")
    assert endurance["data_type"] == "numeric"
    assert endurance["unit"] == "DWPD"
    assert endurance["canonical_unit"] == "DWPD"


def test_hdd_extensions():
    assert _spec("hdd", "drive_class")["enum_values"] == [
        "Enterprise/Nearline",
        "NAS",
        "Surveillance",
        "Desktop",
        "Mobile",
        "Datacenter",
    ]
    assert _spec("hdd", "sector_size")["enum_values"] == ["512n", "512e", "4Kn", "520", "528"]
    # Graded ladder in ascending-tier order; the values are NOT mutually exclusive
    # (a FIPS 140-2 drive is also an SED with ISE) and the facet stores one value per
    # card, so the seed note pins the highest-tier-wins convention for writers.
    encryption = _spec("hdd", "encryption")
    assert encryption["enum_values"] == ["None", "ISE/Instant Secure Erase", "SED", "FIPS 140-2"]
    assert "highest tier wins" in encryption["note"]


def test_motherboards_extensions():
    assert _spec("motherboards", "memory_type")["enum_values"] == [
        "DDR3",
        "DDR4",
        "DDR5",
        "DDR4 RDIMM",
        "DDR5 RDIMM",
    ]
    assert _spec("motherboards", "socket_count")["enum_values"] == ["1", "2", "4", "8"]


def test_power_supplies_extensions():
    psu_class = _spec("power_supplies", "psu_class")
    assert psu_class["is_primary"] is True
    assert psu_class["enum_values"] == [
        "AC-DC (Enclosed)",
        "AC-DC (Open Frame)",
        "AC-DC (DIN Rail)",
        "AC-DC (External/Adapter)",
        "ATX/PC",
        "Server/Redundant",
        "DC-DC Converter",
        "Module/On-Board",
    ]
    output_current = _spec("power_supplies", "output_current")
    assert output_current["data_type"] == "numeric"
    assert output_current["unit"] == "A"
    assert _spec("power_supplies", "input_voltage_type")["enum_values"] == [
        "AC (Universal 85-264V)",
        "AC 120V",
        "AC 230V",
        "AC 3-Phase",
        "DC Input",
    ]


def test_connectors_extensions_and_series_retired():
    assert _spec("connectors", "orientation")["enum_values"] == ["vertical/straight", "right-angle", "horizontal"]
    current = _spec("connectors", "current_rating")
    assert current["data_type"] == "numeric"
    assert current["unit"] == "A"
    assert _spec("connectors", "rows")["enum_values"] == ["1", "2", "3", "4"]
    # The matrix replaced the empty-enum open-vocab 'series' facet with 'rows'.
    assert all(s["spec_key"] != "series" for s in COMMODITY_SPEC_SEEDS["connectors"])


# ── extensions reach an existing (already-seeded) DB at startup ───────


def test_extension_specs_insert_into_already_seeded_db(db_session: Session):
    """seed_commodity_schemas is keyed on (commodity, spec_key) pairs, so net-new spec
    rows on already-seeded commodities insert on the next boot without a reseed."""
    # Simulate a pre-extension DB: dram seeded WITHOUT the new rank/registered/voltage rows.
    for spec in COMMODITY_SPEC_SEEDS["dram"]:
        if spec["spec_key"] in {"rank", "registered", "voltage"}:
            continue
        db_session.add(
            CommoditySpecSchema(
                commodity="dram",
                spec_key=spec["spec_key"],
                display_name=spec["display_name"],
                data_type=spec["data_type"],
                unit=spec.get("unit"),
                canonical_unit=spec.get("canonical_unit"),
                enum_values=spec.get("enum_values"),
                numeric_range=spec.get("numeric_range"),
                sort_order=spec.get("sort_order", 0),
                is_filterable=spec.get("is_filterable", True),
                is_primary=spec.get("is_primary", False),
            )
        )
    db_session.commit()

    seed_commodity_schemas(db_session)

    dram_keys = {row[0] for row in db_session.query(CommoditySpecSchema.spec_key).filter_by(commodity="dram").all()}
    assert {"rank", "registered", "voltage"} <= dram_keys

    # And the brand-new commodity seeds in the same pass.
    tape_keys = {
        row[0] for row in db_session.query(CommoditySpecSchema.spec_key).filter_by(commodity="tape_drives").all()
    }
    assert tape_keys == {"drive_type", "interface", "form_factor", "native_capacity_gb", "encryption"}
