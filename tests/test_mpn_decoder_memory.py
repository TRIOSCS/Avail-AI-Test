"""Accuracy guard for the DRAM MPN decoders — known part numbers → expected specs."""

import pytest

from app.services.mpn_decoder import decode_mpn

CASES = [
    # Samsung — module code → form/ecc, generation letter → ddr_type
    ("M393A2K43DB3-CWE", {"form_factor": "RDIMM", "ecc": "true", "ddr_type": "DDR4"}),
    ("M378B5273DH0-CK0", {"form_factor": "UDIMM", "ecc": "false", "ddr_type": "DDR3"}),
    ("M471A1K43CB1-CTD", {"form_factor": "SO-DIMM", "ecc": "false", "ddr_type": "DDR4"}),
    ("M386A8K40CM2-CVF", {"form_factor": "LRDIMM", "ecc": "true", "ddr_type": "DDR4"}),
    # SK Hynix — prefix → generation, R/U/S/L letter → form/ecc
    ("HMA84GR7AFR4N-UH", {"ddr_type": "DDR4", "form_factor": "RDIMM", "ecc": "true"}),
    ("HMA81GU6CJR8N-VK", {"ddr_type": "DDR4", "form_factor": "UDIMM", "ecc": "false"}),
    ("HMT351R7CFR8C-H9", {"ddr_type": "DDR3", "form_factor": "RDIMM", "ecc": "true"}),
    # Micron — generation from MTA/MTC, 72=ECC / 64=non-ECC
    ("MTA18ASF2G72PZ-2G6E1", {"ddr_type": "DDR4", "ecc": "true"}),
    ("MTA8ATF1G64AZ-2G6E1", {"ddr_type": "DDR4", "ecc": "false"}),
    # Kingston — trailing /<cap>, KVR/KSM speed+module, explicit D<gen> token
    ("KVR16N11/8", {"capacity_gb": 8, "speed_mhz": 1600, "form_factor": "UDIMM", "ecc": "false"}),
    (
        "KVR21R15D4/16",
        {"capacity_gb": 16, "speed_mhz": 2133, "form_factor": "RDIMM", "ecc": "true", "ddr_type": "DDR4"},
    ),
    ("KSM32RD4/32", {"capacity_gb": 32, "speed_mhz": 3200, "form_factor": "RDIMM", "ecc": "true", "ddr_type": "DDR4"}),
    # Crucial — CT<cap>G<gen><form>…<speed>
    (
        "CT16G4RFD8266",
        {"capacity_gb": 16, "ddr_type": "DDR4", "form_factor": "RDIMM", "ecc": "true", "speed_mhz": 2666},
    ),
    ("CT8G4DFRA266", {"capacity_gb": 8, "ddr_type": "DDR4", "form_factor": "UDIMM", "speed_mhz": 2666}),
]


@pytest.mark.parametrize("mpn,expected", CASES)
def test_memory_decode(mpn, expected):
    result = decode_mpn(mpn)
    assert result is not None, f"{mpn} did not decode"
    assert result.commodity == "dram"
    for key, val in expected.items():
        assert result.specs.get(key) == val, f"{mpn}: {key} expected {val!r}, got {result.specs.get(key)!r}"


def test_unrecognized_memory_returns_none():
    assert decode_mpn("SOMETHINGELSE") is None
