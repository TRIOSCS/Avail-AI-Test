"""Accuracy guard for the DRAM MPN decoders — known part numbers → expected specs."""

import pytest

from app.services.mpn_decoder import decode_mpn

CASES = [
    # Samsung — module code → form/ecc, generation letter → ddr_type
    ("M393A2K43DB3-CWE", {"form_factor": "RDIMM", "ecc": True, "ddr_type": "DDR4"}),
    ("M378B5273DH0-CK0", {"form_factor": "UDIMM", "ecc": False, "ddr_type": "DDR3"}),
    ("M471A1K43CB1-CTD", {"form_factor": "SO-DIMM", "ecc": False, "ddr_type": "DDR4"}),
    ("M386A8K40CM2-CVF", {"form_factor": "LRDIMM", "ecc": True, "ddr_type": "DDR4"}),
    # SK Hynix — prefix → generation, R/U/S/L letter → form/ecc
    ("HMA84GR7AFR4N-UH", {"ddr_type": "DDR4", "form_factor": "RDIMM", "ecc": True}),
    ("HMA81GU6CJR8N-VK", {"ddr_type": "DDR4", "form_factor": "UDIMM", "ecc": False}),
    ("HMT351R7CFR8C-H9", {"ddr_type": "DDR3", "form_factor": "RDIMM", "ecc": True}),
    # Micron — generation from MTA/MTC, 72=ECC / 64=non-ECC
    ("MTA18ASF2G72PZ-2G6E1", {"ddr_type": "DDR4", "ecc": True}),
    ("MTA8ATF1G64AZ-2G6E1", {"ddr_type": "DDR4", "ecc": False}),
    # Kingston — trailing /<cap>, KVR/KSM speed+module, explicit D<gen> token
    ("KVR16N11/8", {"capacity_gb": 8, "speed_mhz": 1600, "form_factor": "UDIMM", "ecc": False}),
    (
        "KVR21R15D4/16",
        {"capacity_gb": 16, "speed_mhz": 2133, "form_factor": "RDIMM", "ecc": True, "ddr_type": "DDR4"},
    ),
    ("KSM32RD4/32", {"capacity_gb": 32, "speed_mhz": 3200, "form_factor": "RDIMM", "ecc": True, "ddr_type": "DDR4"}),
    # Crucial — CT<cap>G<gen><form>…<speed>
    (
        "CT16G4RFD8266",
        {"capacity_gb": 16, "ddr_type": "DDR4", "form_factor": "RDIMM", "ecc": True, "speed_mhz": 2666},
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


def test_micron_non_module_not_misdecoded():
    # Bare MT<digit> SDRAM components / legacy modules must NOT decode (no DDR3 default).
    assert decode_mpn("MT40A512M16") is None  # DDR4 SDRAM component
    assert decode_mpn("MT9HTF12872AY") is None  # legacy module — no clean generation token


# ── Round 2: rank / registered / voltage (+ capacity where the org block pins it) ──

RANK_CASES = [
    # Samsung DDR4 org-token table (density digit → capacity, verified tokens → rank)
    (
        "M393A2K43DB3-CWE",  # 16GB 2Rx8 DDR4-3200 RDIMM
        {"rank": "2Rx8", "capacity_gb": 16, "registered": "Registered", "voltage": 1.2},
    ),
    (
        "M393A2K40CB2-CTD",  # 16GB 1Rx4 DDR4-2666 RDIMM
        {"rank": "1Rx4", "capacity_gb": 16, "registered": "Registered", "voltage": 1.2},
    ),
    (
        "M393A4K40CB2-CTD",  # 32GB 2Rx4 DDR4-2666 RDIMM
        {"rank": "2Rx4", "capacity_gb": 32, "registered": "Registered", "voltage": 1.2},
    ),
    (
        "M386A8K40CM2-CVF",  # 64GB 4Rx4 DDR4-2933 LRDIMM
        {"rank": "4Rx4", "capacity_gb": 64, "registered": "Load-Reduced", "voltage": 1.2},
    ),
    (
        "M471A1K43CB1-CTD",  # 8GB 1Rx8 DDR4-2666 SO-DIMM
        {"rank": "1Rx8", "capacity_gb": 8, "registered": "Unbuffered", "voltage": 1.2},
    ),
    # SK Hynix DDR4 — density chars give capacity; die × width math gives rank
    (
        "HMA84GR7AFR4N-UH",  # 32GB 2Rx4 DDR4-2400 RDIMM
        {"rank": "2Rx4", "capacity_gb": 32, "registered": "Registered", "voltage": 1.2},
    ),
    (
        "HMA81GU7AFR8N-UH",  # 8GB 1Rx8 DDR4-2400 ECC UDIMM
        {"rank": "1Rx8", "capacity_gb": 8, "registered": "Unbuffered", "voltage": 1.2, "ecc": True},
    ),
    (
        "HMA82GR7CJR8N-VK",  # 16GB 2Rx8 DDR4-2666 RDIMM
        {"rank": "2Rx8", "capacity_gb": 16, "registered": "Registered", "voltage": 1.2},
    ),
    (
        "HMAA8GR7AJR4N-WM",  # 64GB 2Rx4 DDR4-3200 RDIMM (16Gb die)
        {"rank": "2Rx4", "capacity_gb": 64, "registered": "Registered", "voltage": 1.2},
    ),
    # Micron — device count × bus width; module letter → form/registered; n×8 → capacity
    (
        "MTA18ASF2G72PZ-2G6E1",  # 16GB 1Rx4 DDR4-2666 RDIMM
        {"rank": "1Rx4", "capacity_gb": 16, "form_factor": "RDIMM", "registered": "Registered", "voltage": 1.2},
    ),
    (
        "MTA9ASF1G72PZ-2G6D1",  # 8GB 1Rx8 DDR4-2666 RDIMM
        {"rank": "1Rx8", "capacity_gb": 8, "registered": "Registered", "ecc": True},
    ),
    (
        "MTA36ASF4G72PZ-2G9E2",  # 32GB 2Rx4 DDR4-2933 RDIMM
        {"rank": "2Rx4", "capacity_gb": 32, "registered": "Registered"},
    ),
    (
        "MTA8ATF1G64AZ-2G6E1",  # 8GB 1Rx8 DDR4-2666 non-ECC UDIMM
        {"rank": "1Rx8", "capacity_gb": 8, "form_factor": "UDIMM", "registered": "Unbuffered", "ecc": False},
    ),
    (
        "MTA16ATF2G64HZ-2G6E1",  # 16GB 2Rx8 DDR4-2666 SO-DIMM
        {"rank": "2Rx8", "capacity_gb": 16, "form_factor": "SO-DIMM", "registered": "Unbuffered"},
    ),
    (
        "MT36KSF2G72PZ-1G6M1",  # 16GB 2Rx4 DDR3L-1600 RDIMM (KSF = 1.35 V)
        {"rank": "2Rx4", "capacity_gb": 16, "ddr_type": "DDR3", "voltage": 1.35, "registered": "Registered"},
    ),
    # Kingston — S/D/Q rank token; speed code pins the generation; L = DDR3L flag
    (
        "KVR21R15D4/16",  # 16GB 2Rx4 DDR4-2133 RDIMM
        {"rank": "2Rx4", "registered": "Registered", "voltage": 1.2, "ddr_type": "DDR4"},
    ),
    (
        "KSM32RD4/32",  # 32GB 2Rx4 DDR4-3200 RDIMM
        {"rank": "2Rx4", "registered": "Registered", "voltage": 1.2},
    ),
    (
        "KVR16LR11D4/16",  # 16GB 2Rx4 DDR3L-1600 RDIMM — L is low voltage, NOT LRDIMM
        {
            "rank": "2Rx4",
            "form_factor": "RDIMM",
            "registered": "Registered",
            "voltage": 1.35,
            "ddr_type": "DDR3",
            "speed_mhz": 1600,
            "capacity_gb": 16,
        },
    ),
    (
        "KVR16N11/8",  # 8GB DDR3-1600 non-ECC UDIMM, standard 1.5 V
        {"registered": "Unbuffered", "voltage": 1.5, "ddr_type": "DDR3"},
    ),
    (
        "KSM26ES8/8ME",  # 8GB 1Rx8 DDR4-2666 ECC UDIMM (die-rev suffix after capacity)
        {"rank": "1Rx8", "capacity_gb": 8, "registered": "Unbuffered", "ecc": True, "voltage": 1.2},
    ),
    # Crucial — explicit F<S|D|Q><4|8> token right after the form letter
    ("CT16G4RFD8266", {"rank": "2Rx8", "registered": "Registered", "voltage": 1.2}),
    ("CT8G4RFS4266", {"rank": "1Rx4", "registered": "Registered", "voltage": 1.2}),
    (
        "CT64G4LFQ4266",  # 64GB 4Rx4 DDR4-2666 LRDIMM
        {"rank": "4Rx4", "form_factor": "LRDIMM", "registered": "Load-Reduced", "ecc": True, "capacity_gb": 64},
    ),
    ("CT8G4SFS8266", {"rank": "1Rx8", "form_factor": "SO-DIMM", "registered": "Unbuffered"}),
]


@pytest.mark.parametrize("mpn,expected", RANK_CASES)
def test_rank_registered_voltage(mpn, expected):
    result = decode_mpn(mpn)
    assert result is not None, f"{mpn} did not decode"
    assert result.commodity == "dram"
    for key, val in expected.items():
        assert result.specs.get(key) == val, f"{mpn}: {key} expected {val!r}, got {result.specs.get(key)!r}"


def test_ambiguous_org_codes_omit_rank():
    # Samsung 8G40 is 2Rx4 (16Gb die) or 4Rx4 (3DS) depending on vintage → capacity decodes
    # (density digit 8 = 64GB) but rank must be ABSENT, never guessed.
    result = decode_mpn("M393A8G40MB2-CVF")
    assert result is not None
    assert result.specs.get("capacity_gb") == 64
    assert "rank" not in result.specs
    # Micron two-letter module codes (…PDZ = 2Rx8 on 18 devices) break the device-count
    # rule → form factor still decodes, rank does not.
    result = decode_mpn("MTA18ASF2G72PDZ-2G6")
    assert result is not None
    assert result.specs.get("form_factor") == "RDIMM"
    assert "rank" not in result.specs


def test_ddr3_and_ddr5_voltage_handling():
    # Samsung DDR3: suffix -C/-H = 1.5 V, -Y = 1.35 V (DDR3L).
    assert decode_mpn("M378B5273DH0-CK0").specs.get("voltage") == 1.5
    assert decode_mpn("M393B1K70DH0-YH9").specs.get("voltage") == 1.35
    # Hynix DDR3: the voltage mark is not safely positional → omitted.
    assert "voltage" not in decode_mpn("HMT351R7CFR8C-H9").specs
    # DDR5 runs at 1.1 V — outside the seeded vocabulary → omitted (Samsung DDR5 RDIMM).
    result = decode_mpn("M321R4GA3BB6-CQK")
    assert result.specs.get("ddr_type") == "DDR5"
    assert "voltage" not in result.specs


def test_kingston_ddr3_rank_token_is_not_a_generation():
    # Regression: "D4" in KVR16R11D4/16 is the dual-rank-x4 token — the part is DDR3
    # (speed code 16), not DDR4 as the old substring read claimed.
    result = decode_mpn("KVR16R11D4/16")
    assert result.specs.get("ddr_type") == "DDR3"
    assert result.specs.get("rank") == "2Rx4"
    assert result.specs.get("voltage") == 1.5
