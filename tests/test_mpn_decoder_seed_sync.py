"""Decoder ↔ seed vocabulary sync for app/services/mpn_decoder/.

What: record_spec silently drops any spec_key without a CommoditySpecSchema row and any
      enum value outside the seeded enum_values, so a drift between the decoder modules'
      hand-copied canonical strings and app/data/commodity_seeds.json would zero a whole
      decode feature in production while unit tests stay green. These tests pin the sync:
      (a) every spec key a decoder can emit has a seeded schema for its commodity, and
      (b) every canonical enum constant in storage.py / ssd.py / memory.py is a member of
      the corresponding seeded enum_values.
Called by: pytest (CI + local).
Depends on: commodity_registry._load_commodity_seeds, the decoder modules' constants.
"""

import pytest

from app.services.commodity_registry import _load_commodity_seeds
from app.services.mpn_decoder import memory, ssd, storage

SEEDS = _load_commodity_seeds()


def _schema(commodity: str, spec_key: str) -> dict | None:
    return next((s for s in SEEDS[commodity] if s["spec_key"] == spec_key), None)


# Every spec key each decoder family can emit — keep in sync when a decoder grows a key.
EMITTED_KEYS = {
    "hdd": {"capacity_gb", "form_factor", "usage_class"},
    "ssd": {"capacity_gb", "form_factor", "interface", "nand_type"},
    "dram": {"ddr_type", "capacity_gb", "speed_mhz", "ecc", "form_factor", "rank", "registered", "voltage"},
}


@pytest.mark.parametrize("commodity,keys", sorted(EMITTED_KEYS.items()))
def test_every_emitted_spec_key_has_a_seeded_schema(commodity, keys):
    for key in sorted(keys):
        assert _schema(commodity, key) is not None, (
            f"decoder emits {commodity}.{key} but commodity_seeds.json has no schema for it — "
            "record_spec would silently drop every value"
        )


# (commodity, spec_key, the decoder constants that must be seeded enum members)
ENUM_MEMBERSHIP = [
    ("hdd", "form_factor", {storage.FF_35, storage.FF_25}),
    (
        "hdd",
        "usage_class",
        {storage.UC_ENTERPRISE, storage.UC_NAS, storage.UC_SURVEILLANCE, storage.UC_DESKTOP},
    ),
    (
        "ssd",
        "form_factor",
        {ssd.FF_25, ssd.FF_M2_2280, ssd.FF_M2_2230, ssd.FF_M2_22110, ssd.FF_U2, ssd.FF_U3, ssd.FF_MSATA},
    ),
    ("ssd", "interface", {ssd.IF_SATA, ssd.IF_SAS, ssd.IF_NVME3, ssd.IF_NVME4}),
    ("ssd", "nand_type", {ssd.TLC, ssd.MLC, ssd.QLC}),
    ("dram", "form_factor", {memory.RDIMM, memory.LRDIMM, memory.UDIMM, memory.SODIMM, memory.DIMM}),
    ("dram", "ddr_type", {memory.DDR3, memory.DDR4, memory.DDR5}),
    ("dram", "registered", {memory.REG_R, memory.REG_U, memory.REG_LR}),
    ("dram", "rank", memory._ALLOWED_RANKS),
]


@pytest.mark.parametrize("commodity,spec_key,constants", ENUM_MEMBERSHIP)
def test_decoder_enum_constants_are_seeded(commodity, spec_key, constants):
    schema = _schema(commodity, spec_key)
    assert schema is not None, f"{commodity}.{spec_key} schema missing from seeds"
    assert schema["data_type"] == "enum"
    missing = set(constants) - set(schema["enum_values"])
    assert not missing, (
        f"{commodity}.{spec_key}: decoder constants {sorted(missing)} are not in the seeded "
        "enum_values — record_spec would silently drop those values"
    )


def test_dram_rank_allowlist_exactly_mirrors_seed():
    # _ALLOWED_RANKS is the decoder-local mirror of the seeded rank enum; a value seeded but
    # not allowed would be unreachable, a value allowed but not seeded would be dropped.
    assert memory._ALLOWED_RANKS == set(_schema("dram", "rank")["enum_values"])


def test_dram_voltage_is_a_seeded_numeric_volt_spec():
    schema = _schema("dram", "voltage")
    assert schema is not None
    assert schema["data_type"] == "numeric"
    assert schema["unit"] == "V"
    assert schema["canonical_unit"] == "V"
