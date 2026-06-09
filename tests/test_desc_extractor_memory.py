"""Accuracy guard for the DRAM description extractor — REAL corpus strings → exact
specs.

Strings are verbatim from TRIO's part master (Material_Description__c) and the staged
inventory sheets. Expectations are FULL equality on the specs dict.
"""

import pytest

from app.services.desc_extractor import extract_desc

# (real description, exact expected specs)
CASES = [
    (
        "Mem, 16GB DDR4 2Rx4 PC4-2400T RDIMM",
        {
            "capacity_gb": 16,
            "ddr_type": "DDR4",
            "speed_mhz": 2400,  # deterministic PC4-2400T speed grade
            "form_factor": "RDIMM",
            "ecc": True,  # RDIMM ⇒ ECC (JEDEC)
            "rank": "2Rx4",
        },
    ),
    (
        "Memory, 16GB MEMORY DDR3 1600MHZ, IBM",
        {"capacity_gb": 16, "ddr_type": "DDR3", "speed_mhz": 1600},
    ),
    (
        "Memory, 2GB 1x2gb 2Rx8 PC3-10600 DDR3 RDIMM, IBM",  # PC3-10600 bandwidth NOT decoded
        {"capacity_gb": 2, "ddr_type": "DDR3", "form_factor": "RDIMM", "ecc": True, "rank": "2Rx8"},
    ),
    (
        "Memory, 8GB 1RX4 PC4-2400T-R MEMORY MODULE (1X8GB), HP",  # (1X8GB) is not a 2nd size
        {"capacity_gb": 8, "ddr_type": "DDR4", "speed_mhz": 2400, "rank": "1Rx4"},
    ),
    (
        "Memory, BO FRU for 16GB DDR3L-1600 SODIMM, Lenovo",  # DDR3L + bare "1600" not decoded
        {"capacity_gb": 16, "ddr_type": "DDR3L", "form_factor": "SO-DIMM"},
    ),
    (
        "Memory, 4GB PC3-12800 DDR3-1600MHz ECC Unbuffered CL11 240-Pin DIMM "
        "Very Low Profile (VLP) Single Rank Memory Modu",
        {"capacity_gb": 4, "ddr_type": "DDR3", "speed_mhz": 1600, "form_factor": "DIMM", "ecc": True},
    ),
    (
        "DDR2, 2GB, DIMM, ECC Reg, 276 PIN, 533Mhz, CL4, IBM",  # 533 < seeded 800 floor
        {"capacity_gb": 2, "ddr_type": "DDR2", "form_factor": "DIMM", "ecc": True},
    ),
    (
        "MEM, 512MB, PC2700, DDR-333MHz, 184PIN ECC REG",  # MB size + legacy speed dropped
        {"ddr_type": "DDR", "ecc": True},
    ),
    (
        "MEM, 8GB, UDIMM, Memory",
        {"capacity_gb": 8, "form_factor": "UDIMM"},
    ),
    (
        "Memory, 16GB DDR4 2400 SoDIMM, Lenovo",  # bare "2400" (no MHz) deliberately not decoded
        {"capacity_gb": 16, "ddr_type": "DDR4", "form_factor": "SO-DIMM"},
    ),
    (
        "Memory, 16GB, PC3-10600R DDR3-1333Mhz 2RX4 Ecc, (Dual-Rank x4) 1.35 V, LP RDIMM, IBM",
        {
            "capacity_gb": 16,
            "ddr_type": "DDR3",
            "speed_mhz": 1333,
            "form_factor": "RDIMM",
            "ecc": True,
            "rank": "2Rx4",
        },
    ),
    (
        "Memory, 1 GB PC2-3200 200 PIN DDR2, SMART",  # spaced unit; PC2 prefix NOT inferred
        {"capacity_gb": 1, "ddr_type": "DDR2"},
    ),
]


@pytest.mark.parametrize("description,expected", CASES)
def test_memory_extract_exact(description, expected):
    result = extract_desc(description)
    assert result is not None, f"{description!r} did not extract"
    assert result.commodity == "dram"
    assert result.specs == expected
    assert result.confidence == 0.90


def test_ecc_is_a_real_bool():
    # record_spec's boolean path does bool(value): a string "true"/"false" would corrupt
    # ("false" is truthy). The extractor must emit Python bools, like the MPN decoders.
    result = extract_desc("Mem, 16GB DDR4 2Rx4 PC4-2400T RDIMM")
    assert result is not None
    assert result.specs["ecc"] is True


def test_memory_hint_with_storage_text_is_a_conflict():
    # Real part-master row: "Memory," lead but the body says it is an M.2 SSD. Never
    # pick a side — the cross-family conflict returns None even with a dram hint.
    assert extract_desc("Memory, 256GB, LiteOn SSD, M.2 2280") is None
    assert extract_desc("Memory, 256GB, LiteOn SSD, M.2 2280", commodity_hint="dram") is None


def test_module_with_no_extractable_grammar_returns_commodity_only():
    result = extract_desc("Memory, Memory module, IBM")
    assert result is not None
    assert result.commodity == "dram"
    assert result.specs == {}
