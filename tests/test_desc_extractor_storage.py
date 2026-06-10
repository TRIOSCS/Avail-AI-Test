"""Accuracy guard for the storage description extractor — REAL corpus strings → exact
specs.

Every description below is verbatim from TRIO's part master
(/root/source_ingest/LSC1__Material__c.csv, Material_Description__c) or the staged
inventory sheets (Inventory_2.12.26 / Firesale_inventory). Expectations are FULL
equality — a new key appearing unexpectedly is as much a failure as a missing one.
"""

import pytest

from app.services.desc_extractor import extract_desc

# (real description, expected commodity, exact expected specs)
CASES = [
    # ── TRIO part-master "<Label>, …" grammar ────────────────────────────
    (
        'HD, 450GB, 15KRPM, 3.5", Fibre Channel',
        "hdd",
        {"capacity_gb": 450, "rpm": "15000", "form_factor": '3.5"', "interface": "FC"},
    ),
    (
        'HDD, 146GB, 15,000 RPM, 3.5", SCSI',
        "hdd",
        {"capacity_gb": 146, "rpm": "15000", "form_factor": '3.5"', "interface": "SCSI"},
    ),
    (
        'HDD, 36GB, 3.5", 10K, U320 SCSI',
        "hdd",
        {"capacity_gb": 36, "rpm": "10000", "form_factor": '3.5"', "interface": "SCSI"},
    ),
    (
        'HDD, 640GB, 2.5", 5400R, SATA, 9.5, Hitachi',  # "5400R" rpm form
        "hdd",
        {"capacity_gb": 640, "rpm": "5400", "form_factor": '2.5"', "interface": "SATA"},
    ),
    (
        "HDD, 160GB SATA 3.5'' 7200RPM, Seagate",  # double-apostrophe inch mark
        "hdd",
        {"capacity_gb": 160, "rpm": "7200", "form_factor": '3.5"', "interface": "SATA"},
    ),
    (
        'HDD, 300 GB, 10K RPM, 3.5", FC, IBM',  # spaced unit + bare FC token
        "hdd",
        {"capacity_gb": 300, "rpm": "10000", "form_factor": '3.5"', "interface": "FC"},
    ),
    (
        'HDD, 73.4GB, 15K, 3.5", FC H-SWAP 2Gbps w/Tray',  # fractional legacy capacity
        "hdd",
        {"capacity_gb": 73.4, "rpm": "15000", "form_factor": '3.5"', "interface": "FC"},
    ),
    # ── inventory-sheet grammar (link speeds, SFF/LFF, NL-SAS, bare G) ───
    (
        "HDD, 6Gbps 1.2TB 10K 2.5 Inch HDD, IBM",  # 6Gbps dropped; no interface token
        "hdd",
        {"capacity_gb": 1200, "rpm": "10000", "form_factor": '2.5"'},
    ),
    (
        "4TB 7.2K Rpm 3.5inch 12gbps Sas HDD",  # no lead label — trailing HDD token routes
        "hdd",
        {"capacity_gb": 4000, "rpm": "7200", "form_factor": '3.5"', "interface": "SAS"},
    ),
    (
        "HDD, 300GB 10K SFF 6G SAS HDD for VNX, EMC",  # SFF ⇒ 2.5"; bare 6G link discarded
        "hdd",
        {"capacity_gb": 300, "rpm": "10000", "form_factor": '2.5"', "interface": "SAS"},
    ),
    (
        'HDD, 4 TB 6GB 3.5" 7,200 RPM SAS, IBM',  # "6GB" is the SAS link, not a size
        "hdd",
        {"capacity_gb": 4000, "rpm": "7200", "form_factor": '3.5"', "interface": "SAS"},
    ),
    (
        "HDD, IBM 600G 15K Sas 12gbps, IBM",  # bare-G capacity
        "hdd",
        {"capacity_gb": 600, "rpm": "15000", "interface": "SAS"},
    ),
    (
        "HDD, 2TB 7,200 rpm 12Gb SAS NL UBM, IBM",  # 12Gb link discarded; NL SAS ⇒ SAS
        "hdd",
        {"capacity_gb": 2000, "rpm": "7200", "interface": "SAS"},
    ),
    (
        "HDD, 2TB 7200RPM NL SAS 6GB, 3.5-inch, H/S",  # "-inch" form-factor spelling
        "hdd",
        {"capacity_gb": 2000, "rpm": "7200", "form_factor": '3.5"', "interface": "SAS"},
    ),
    (
        "HDD,300G,SFF,10K,SGT,10K8,SAS,WS,512n",  # "10K8" family token must not parse as rpm
        "hdd",
        {"capacity_gb": 300, "rpm": "10000", "form_factor": '2.5"', "interface": "SAS"},
    ),
    (
        'HDD, ULTRASTAR C10K900 2.5" 450GB 10000RPM SAS TCG, Hitachi',  # "C10K900" ≠ rpm
        "hdd",
        {"capacity_gb": 450, "rpm": "10000", "form_factor": '2.5"', "interface": "SAS"},
    ),
    (
        'HDD, 450GB 15000RPM 16MB 3.5" SAS, N-Series',  # 16MB cache ignored
        "hdd",
        {"capacity_gb": 450, "rpm": "15000", "form_factor": '3.5"', "interface": "SAS"},
    ),
    (
        "300gb 15000rpm SAS Hard Drive (IBM)",  # lowercase; "Hard Drive" body token routes
        "hdd",
        {"capacity_gb": 300, "rpm": "15000", "interface": "SAS"},
    ),
    # ── SSD grammar (per-commodity vocabulary gating) ────────────────────
    (
        'SSD, 400GB, SATA, 2.5", IBM',
        "ssd",
        {"capacity_gb": 400, "form_factor": '2.5"', "interface": "SATA"},
    ),
    (
        'SSD, 600GB, 3.5", FC 4Gb/s, STEC Hikari MLC 41nm',  # 3.5"/FC not seeded for ssd
        "ssd",
        {"capacity_gb": 600},
    ),
    (
        "SSD, 800GB, 1.8in, SATA 6Gb/s, 20nm, Intel",  # 1.8" not seeded for ssd; Gb/s dropped
        "ssd",
        {"capacity_gb": 800, "interface": "SATA"},
    ),
    (
        'SSD 480GB 7mmH Intel 2.5"CV-480 6Gb/s, Lenovo',  # comma-less lead token
        "ssd",
        {"capacity_gb": 480, "form_factor": '2.5"'},
    ),
    (
        "Storage SSD CV8 128G M.2 Liteon",  # body SSD token; bare M.2 has no seeded member
        "ssd",
        {"capacity_gb": 128},
    ),
    (
        # Re-audit 2026-06-10 class 3: NAND-die context turns a bare "512G" into a
        # gigaBIT die density (512 Gbit) — never a drive capacity. Deliberate NO-WRITE.
        "SSD, Nand, 512G, MLC, Micron",
        "ssd",
        {},
    ),
    (
        # …but an EXPLICIT GB token stays gigabytes even when the desc names its flash
        # type — real drives advertise TLC/MLC constantly.
        "SSD, 960GB, TLC, SATA, Samsung",
        "ssd",
        {"capacity_gb": 960, "interface": "SATA"},
    ),
    (
        # …and so does a BARE-G capacity next to a flash-type token: TLC/MLC alone is
        # ordinary SSD product copy, not proof of a NAND die (the die guard requires
        # the NAND word or an MT29 die MPN) — broker shorthand "480G" is 480 GB.
        "SSD, 480G, MLC, SATA, Intel",
        "ssd",
        {"capacity_gb": 480, "interface": "SATA"},
    ),
    (
        "SSD 256G TLC SATA",  # same class, comma-less grammar
        "ssd",
        {"capacity_gb": 256, "interface": "SATA"},
    ),
    (
        # A spaced PCIe lane-width token ("X8") must not be read as NAND-die context
        # either — the bare-G capacity survives (NVMe is not seeded for ssd interface).
        "SSD, 800G, NVME, PCIE X8",
        "ssd",
        {"capacity_gb": 800},
    ),
]


@pytest.mark.parametrize("description,commodity,expected", CASES)
def test_storage_extract_exact(description, commodity, expected):
    result = extract_desc(description)
    assert result is not None, f"{description!r} did not extract"
    assert result.commodity == commodity
    assert result.specs == expected
    assert result.confidence == 0.90


def test_conflicting_capacities_omit_the_key():
    # Real part-master string carrying two different sizes — capacity must be ABSENT,
    # the unambiguous keys still extract.
    result = extract_desc('HDD, 146GB (70GB), 15K RPM, 3.5", SCSI')
    assert result is not None
    assert result.specs == {"rpm": "15000", "form_factor": '3.5"', "interface": "SCSI"}


def test_conflicting_form_factors_omit_the_key():
    # Drive sold with a hot-swap kit: 2.5" AND 3.5" both present — form_factor omitted.
    result = extract_desc('SSD, 50GB, 2.5" W/TRAY W/3.5" HOT-SWAP HIGH Kit')
    assert result is not None
    assert result.specs == {"capacity_gb": 50}


def test_lead_label_arbitrates_mixed_storage_tokens():
    # "HDD," lead wins over a body "SSD" token (both storage family) — and the IBM
    # part really is HDD-labeled flash in the part master.
    result = extract_desc('HDD, 128GB, SSD 2.5"')
    assert result is not None
    assert result.commodity == "hdd"
    assert result.specs == {"capacity_gb": 128, "form_factor": '2.5"'}


def test_no_commodity_token_returns_none_without_hint():
    # Real string with full drive grammar but no HDD/SSD token anywhere — too risky
    # without the caller's category hint.
    assert extract_desc("500GB 5400RPM SATA THIN") is None


def test_hint_routes_tokenless_drive_grammar():
    result = extract_desc("500GB 5400RPM SATA THIN", commodity_hint="hdd")
    assert result is not None
    assert result.commodity == "hdd"
    assert result.specs == {"capacity_gb": 500, "rpm": "5400", "interface": "SATA"}
