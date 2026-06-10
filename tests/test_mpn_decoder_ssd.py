"""Accuracy guard for the SSD MPN decoders — known part numbers → exact expected specs.

Each vendor table is real shipping MPNs; the non-matching lookalikes pin the gates shut
(an OEM spare or sibling scheme must return None / a different commodity, never a
guess).
"""

import pytest

from app.services.mpn_decoder import decode_mpn

SAMSUNG_CASES = [
    # Retail dashed scheme MZ-<family><capacity>…
    (
        "MZ-V8V1T0B/AM",  # 980 1TB
        {"capacity_gb": 1000, "form_factor": "M.2 2280", "interface": "NVMe PCIe 3.0", "nand_type": "TLC"},
    ),
    (
        "MZ-V8P2T0B/AM",  # 980 PRO 2TB
        {"capacity_gb": 2000, "form_factor": "M.2 2280", "interface": "NVMe PCIe 4.0", "nand_type": "TLC"},
    ),
    (
        "MZ-77E1T0B/AM",  # 870 EVO 1TB
        {"capacity_gb": 1000, "form_factor": '2.5"', "interface": "SATA", "nand_type": "TLC"},
    ),
    (
        "MZ-77Q8T0B/AM",  # 870 QVO 8TB
        {"capacity_gb": 8000, "form_factor": '2.5"', "interface": "SATA", "nand_type": "QLC"},
    ),
    (
        "MZ-N6E500BW",  # 860 EVO M.2 SATA 500GB
        {"capacity_gb": 500, "form_factor": "M.2 2280", "interface": "SATA", "nand_type": "TLC"},
    ),
    # OEM compact scheme MZ<family><capacity>…
    ("MZVL21T0HCLR", {"capacity_gb": 1000, "form_factor": "M.2 2280", "interface": "NVMe PCIe 4.0"}),  # PM9A1
    ("MZ7LH1T9HMLT", {"capacity_gb": 1920, "form_factor": '2.5"', "interface": "SATA"}),  # PM883
    ("MZQL21T9HCJR", {"capacity_gb": 1920, "form_factor": "U.2", "interface": "NVMe PCIe 4.0"}),  # PM9A3
    ("MZQLB960HAJR", {"capacity_gb": 960, "form_factor": "U.2", "interface": "NVMe PCIe 3.0"}),  # PM983
    ("MZNLN256HMHQ", {"capacity_gb": 256, "form_factor": "M.2 2280", "interface": "SATA"}),  # PM871b
    ("MZILT3T8HBLS", {"capacity_gb": 3840, "form_factor": '2.5"', "interface": "SAS"}),  # PM1643a
    ("MZ1LB960HAJQ", {"capacity_gb": 960, "form_factor": "M.2 22110", "interface": "NVMe PCIe 3.0"}),  # PM983
]

MICRON_CASES = [
    ("MTFDDAK1T9TDS", {"capacity_gb": 1920, "form_factor": '2.5"', "interface": "SATA"}),  # 5300 PRO
    ("MTFDDAK480TDS", {"capacity_gb": 480, "form_factor": '2.5"', "interface": "SATA"}),  # 5300 PRO
    ("MTFDDAV240TCB", {"capacity_gb": 240, "form_factor": "M.2 2280", "interface": "SATA"}),  # 1100
    ("MTFDKBA960TFR", {"capacity_gb": 960, "form_factor": "M.2 2280", "interface": "NVMe PCIe 4.0"}),  # 7450 PRO
    ("MTFDKCB3T8TDZ", {"capacity_gb": 3840, "form_factor": "U.3", "interface": "NVMe PCIe 4.0"}),  # 7400 PRO
    ("MTFDKCC15T3TFR", {"capacity_gb": 15360, "form_factor": "U.3", "interface": "NVMe PCIe 4.0"}),  # 7450 PRO
    ("MTFDHAL3T8TDP", {"capacity_gb": 3840, "form_factor": "U.2", "interface": "NVMe PCIe 3.0"}),  # 9300 PRO
]

INTEL_CASES = [
    ("SSDSC2KB960G8", {"capacity_gb": 960, "form_factor": '2.5"', "interface": "SATA"}),  # D3-S4510
    ("SSDSC2BB120G4", {"capacity_gb": 120, "form_factor": '2.5"', "interface": "SATA"}),  # DC S3500
    ("SSDSC2KB019T8", {"capacity_gb": 1920, "form_factor": '2.5"', "interface": "SATA"}),  # D3-S4510 1.92TB
    ("SSDSCKKB240G8", {"capacity_gb": 240, "form_factor": "M.2 2280", "interface": "SATA"}),  # D3-S4510 M.2
    ("SSDPE2KX040T8", {"capacity_gb": 4000, "form_factor": "U.2", "interface": "NVMe PCIe 3.0"}),  # DC P4510
    ("SSDPE2KE032T8", {"capacity_gb": 3200, "form_factor": "U.2", "interface": "NVMe PCIe 3.0"}),  # DC P4610
    ("SSDPEKNW010T8", {"capacity_gb": 1000, "form_factor": "M.2 2280", "interface": "NVMe PCIe 3.0"}),  # 660p
    ("SSDPF2KX038T1", {"capacity_gb": 3840, "form_factor": "U.2", "interface": "NVMe PCIe 4.0"}),  # D7-P5520
]

KIOXIA_CASES = [
    ("KXG50ZNV256G", {"capacity_gb": 256, "form_factor": "M.2 2280", "interface": "NVMe PCIe 3.0"}),  # XG5
    ("KXG60ZNV1T02", {"capacity_gb": 1024, "form_factor": "M.2 2280", "interface": "NVMe PCIe 3.0"}),  # XG6
    ("KXG80ZNV1T02", {"capacity_gb": 1024, "form_factor": "M.2 2280", "interface": "NVMe PCIe 4.0"}),  # XG8
    ("KPM51RUG1T92", {"capacity_gb": 1920, "form_factor": '2.5"', "interface": "SAS"}),  # PM5-R
    ("KPM61RUG3T84", {"capacity_gb": 3840, "form_factor": '2.5"', "interface": "SAS"}),  # PM6-R
    ("KCD51LUG960G", {"capacity_gb": 960, "form_factor": "U.2", "interface": "NVMe PCIe 3.0"}),  # CD5
    ("KCD61LUL3T84", {"capacity_gb": 3840, "form_factor": "U.3", "interface": "NVMe PCIe 4.0"}),  # CD6-R
    ("KCM61RUL3T84", {"capacity_gb": 3840, "form_factor": "U.3", "interface": "NVMe PCIe 4.0"}),  # CM6-R
]

WD_CASES = [
    ("WDS500G2B0A", {"capacity_gb": 500, "form_factor": '2.5"', "interface": "SATA"}),  # Blue 3D
    ("WDS100T2B0A", {"capacity_gb": 1000, "form_factor": '2.5"', "interface": "SATA"}),  # Blue 3D 1TB
    ("WDS100T1R0A", {"capacity_gb": 1000, "form_factor": '2.5"', "interface": "SATA"}),  # Red SA500
    ("WDS250G2B0B", {"capacity_gb": 250, "form_factor": "M.2 2280", "interface": "SATA"}),  # Blue M.2 SATA
    ("WDS500G3X0C", {"capacity_gb": 500, "form_factor": "M.2 2280", "interface": "NVMe PCIe 3.0"}),  # SN750
    ("WDS100T1X0E", {"capacity_gb": 1000, "form_factor": "M.2 2280", "interface": "NVMe PCIe 4.0"}),  # SN850
    ("WDS200T2X0E", {"capacity_gb": 2000, "form_factor": "M.2 2280", "interface": "NVMe PCIe 4.0"}),  # SN850X
]

ALL_CASES = (
    [(m, e, "Samsung") for m, e in SAMSUNG_CASES]
    + [(m, e, "Micron") for m, e in MICRON_CASES]
    + [(m, e, "Intel") for m, e in INTEL_CASES]
    + [(m, e, "Kioxia") for m, e in KIOXIA_CASES]
    + [(m, e, "Western Digital") for m, e in WD_CASES]
)


@pytest.mark.parametrize("mpn,expected,vendor", ALL_CASES)
def test_ssd_decode(mpn, expected, vendor):
    result = decode_mpn(mpn)
    assert result is not None, f"{mpn} did not decode"
    assert result.commodity == "ssd"
    assert result.vendor == vendor
    assert result.specs == expected, f"{mpn}: expected {expected!r}, got {result.specs!r}"


def test_nand_type_omitted_when_not_encoded():
    # OEM schemes don't encode NAND type; retail families outside the digit+E/Q rule
    # (e.g. 850 PRO MZ-7KE…) must omit it too — omission is correct, a guess is not.
    for mpn in ("MZVL21T0HCLR", "MZ7LH1T9HMLT", "MZ-7KE256BW", "MTFDDAK1T9TDS", "WDS100T1X0E"):
        result = decode_mpn(mpn)
        assert result is not None and "nand_type" not in result.specs, mpn


PARTIAL_CASES = [
    # The "omit, never guess" fallback edges: each branch keeps only what the scheme still
    # pins and emits NOTHING else (asserted exactly — a guessed extra key fails the test).
    # WD suffix outside _WD_SSD_SUFFIX → capacity only (a regression to dict indexing
    # would KeyError here and crash the dry-run script).
    ("WDS100T3Z0Z", {"capacity_gb": 1000}),
    # Kioxia enterprise generation outside the pinned table (CM7) → capacity only.
    ("KCM71RUL3T84", {"capacity_gb": 3840}),
    # Intel T-token outside _INTEL_TB → form factor + interface kept, capacity omitted.
    ("SSDPE2KX050T8", {"form_factor": "U.2", "interface": "NVMe PCIe 3.0"}),
    # Samsung *retail* V family outside _SAMSUNG_RETAIL_V (950 PRO) → form factor (+ the
    # always-positional capacity), no guessed interface/nand (retail branch, distinct
    # from the OEM-side MZVPW… case below).
    ("MZ-V5P512BW", {"capacity_gb": 512, "form_factor": "M.2 2280"}),
]


@pytest.mark.parametrize("mpn,expected", PARTIAL_CASES)
def test_conservative_partial_decode(mpn, expected):
    result = decode_mpn(mpn)
    assert result is not None, f"{mpn} did not decode"
    assert result.commodity == "ssd"
    assert result.specs == expected, f"{mpn}: expected exactly {expected!r}, got {result.specs!r}"


def test_unknown_pcie_gen_omits_interface():
    # Samsung OEM family not in the pinned-generation table → form factor only, no
    # guessed NVMe generation (the seeded interface enum has no bare "NVMe").
    result = decode_mpn("MZVPW256HEGL")  # SM961 — not in the gen table
    assert result is not None
    assert result.specs["form_factor"] == "M.2 2280"
    assert "interface" not in result.specs


@pytest.mark.parametrize(
    "mpn",
    [
        "MZ12345",  # MZ prefix but no valid family+capacity structure
        "MTFDXYZ960TDS",  # MTFD gate, unverified form/interface code → None, no partial guess
        "SSDPEL1K100GA",  # Optane P4801X M.2 22110 — prefix outside the verified five
        "KXGABC123",  # KXG without a generation digit
        "WDS100X",  # WDS without the capacity+suffix structure
        "WDSSD1234",  # capacity digits missing entirely
    ],
)
def test_ssd_lookalikes_return_none(mpn):
    assert decode_mpn(mpn) is None, f"{mpn} must not decode"


def test_ssd_gates_do_not_shadow_hdd_or_dram():
    # WD HDD (WD\d…) and WD SSD (WDS…) coexist; Samsung DRAM (M393…) never hits the
    # Samsung SSD gate (MZ…); Micron DRAM (MTA…) never hits MTFD; Kingston modules
    # (KCP…) never hit Kioxia (KCM/KCD).
    assert decode_mpn("WD40EFRX").commodity == "hdd"
    assert decode_mpn("WDS500G2B0A").commodity == "ssd"
    assert decode_mpn("M393A2K43DB3-CWE").commodity == "dram"
    assert decode_mpn("MTA18ASF2G72PZ-2G6E1").commodity == "dram"
    assert decode_mpn("MG08ACA16TE").commodity == "hdd"
    assert decode_mpn("KCD61LUL3T84").commodity == "ssd"
    assert decode_mpn("KCP426ND8/16").commodity == "dram"
