"""Accuracy guard for the GPU description extractor — REAL corpus strings → exact specs.

Every description below is verbatim from TRIO's part master
(/root/source_ingest/LSC1__Material__c.csv, Material_Description__c). Expectations are
FULL equality. The GC bucket is heavily contaminated (NICs, RAID flash modules), so the
memory_gb GPU-context-token guard gets its own negative block.
"""

import pytest

from app.services.desc_extractor import extract_desc

# (real description, commodity_hint or None, exact expected specs)
CASES = [
    # ── family + memory ──────────────────────────────────────────────────
    ("PCA Quadro GP100 16GB HBM2", "gpu", {"gpu_family": "Quadro", "memory_gb": 16}),
    (
        "SPS-PCA, NVIDIA Tesla V100 32GB Module",  # SPS- prefixed lead is neutral, not foreign
        "gpu",
        {"gpu_family": "Tesla", "memory_gb": 32},
    ),
    (
        "MSI, RTX3080, 10G/D6X/3DP/H",  # neutral brand lead; glued 10G/D6X memory grammar
        "gpu",
        {"gpu_family": "RTX", "memory_gb": 10},
    ),
    ("BLD RTX3060 12GB G6 3DP+H", "gpu", {"gpu_family": "RTX", "memory_gb": 12}),
    (
        "GTX1660Super@6G/D6/DP/H/DVI",  # GTX is definitionally GeForce
        "gpu",
        {"gpu_family": "GeForce", "memory_gb": 6},
    ),
    ("SPS-GRAPHICS NVIDIA Quadro P4200 8GB", "gpu", {"gpu_family": "Quadro", "memory_gb": 8}),
    (
        "SPS-PCA NVIDIA T1000 8GB",  # T1000 unmapped — NVIDIA context still allows memory
        "gpu",
        {"memory_gb": 8},
    ),
    (
        # Real bandwidth grammar: 608GB/S is skipped by the trailing-"/S" check and
        # 320W/9500MHz never match — only the 10GB memory token survives.
        "VGA CARD.GEFORCE RTX 3080.10GB GDDR6.256B.9500MHz.(BUY-LC).HDMI+DP*3..320W.608GB/S.W/ATX BKT.ASPM.SV11B20562....LEAD-FREE.MSI",
        "gpu",
        {"gpu_family": "GeForce", "memory_gb": 10},
    ),
    # ── body-token routing (no hint needed) ──────────────────────────────
    ("RX550 CMIT FH 4GB GFX card", None, {"gpu_family": "Radeon", "memory_gb": 4}),
    ("SXM ASSY,K20X GPU 20 FINS", None, {"gpu_family": "Tesla"}),  # neutral "SXM ASSY," lead
    ("CRD,GRPHC,NV,GTX,1060", None, {"gpu_family": "GeForce"}),  # bare 1060 has no GB token
    # ── conflicts and unmapped vocabulary ────────────────────────────────
    (
        "GPU, NVIDIA GeForce GTX, Quadro P2000, GP106-875, 1024, 1480MHz. 7Gbps. 5GB GD5",
        None,
        {"memory_gb": 5},  # GeForce×Quadro conflict omits the family; 5GB still emits
    ),
    ("GPU CARD,PASCAL GP100 PASSIVE", None, {}),  # PASCAL spans Tesla/Quadro — unmapped
    ("2080TI Founders edition", "gpu", {}),  # bare model, no family token
    (
        # "-A2" is an NVIDIA silicon-stepping suffix (N17M = GeForce MX150-class
        # laptop chip), NOT an Ampere A2 — the (?<![\w-]) lookbehind rejects it.
        # 908P/A31 carry no GB token, so nothing else emits either.
        "S IC N17M-Q3-A2 BGA 908P GPU A31 !",
        None,
        {},
    ),
]


@pytest.mark.parametrize("description,hint,expected", CASES)
def test_gpu_extract_exact(description, hint, expected):
    result = extract_desc(description, commodity_hint=hint)
    assert result is not None, f"{description!r} did not extract"
    assert result.commodity == "gpu"
    assert result.specs == expected
    assert result.confidence == 0.90


# ── the cross-commodity GB guard: no GPU-context token ⇒ no memory_gb ────
GUARDED_GB_CASES = [
    "Emulex, 10GB, SFP+Mezza Card",  # NIC mezzanine inside the GC bucket
    "NVIDIA ConnectX-7 Dual-Port 100GbE Ethernet Adapter",  # 100GbE never matches \bGB\b
    "Flash card for Xseries ThinkServer RAID 720i 4GB Modular Flash & Supercapacitor",
    # NVIDIA-branded Mellanox NIC: the spaced "25Gb" link speed uppercases into the
    # \bGB\b shape, but the NIC clause (ConnectX/SFP/dual-port) disqualifies memory_gb.
    "NVIDIA ConnectX-4 Lx 25Gb dual-port SFP28 adapter",
    # Decimal memory bandwidth: "14.4GB/S" would capture its fractional digit (4)
    # without the trailing-"/S" skip — nothing may emit.
    "GPU CARD, GT710 PASSIVE 14.4GB/S GDDR5",
]


@pytest.mark.parametrize("description", GUARDED_GB_CASES)
def test_memory_gb_requires_gpu_context_token(description):
    result = extract_desc(description, commodity_hint="gpu")
    assert result is not None
    assert result.specs == {}, f"{description!r} must not emit GPU specs"


def test_dram_module_row_under_gpu_hint_is_contradicted():
    # DRAM-module row on a gpu-categorized card: the SODIMM/DDR4 strong body
    # tokens contradict the gpu hint, so the router's body-token contradiction
    # guard suppresses extraction OUTRIGHT (None) — the 16GB belongs to the
    # SODIMM, not a GPU. (Before the guard this row routed gpu and relied on the
    # gpu module's internal dram-token defense to emit nothing; the guard now
    # stops it one layer earlier, same protected outcome.)
    assert extract_desc("SODIMM 16GB DDR4 for NVIDIA DGX Station", commodity_hint="gpu") is None


def test_t4_emits_tesla_family():
    # Spec §3.4 maps the T4 datacenter chip to Tesla; the word boundary keeps
    # workstation "T400"/"T4000" cards unmatched (no boundary before the 0).
    result = extract_desc("NVIDIA T4 16GB PCIe", commodity_hint="gpu")
    assert result is not None
    assert result.specs == {"gpu_family": "Tesla", "memory_gb": 16}
    result = extract_desc("Nvidia T400 4GB GDDR6", commodity_hint="gpu")
    assert result is not None
    assert result.specs == {"memory_gb": 4}  # T400 is NOT T4 — no family


def test_two_distinct_gb_values_omit_memory():
    # The unique-survivor contract: two different in-range GB candidates ⇒ the
    # memory_gb key is omitted, never max()/first-match picked.
    result = extract_desc("GPU, NVIDIA 8GB or 16GB configurations", commodity_hint="gpu")
    assert result is not None
    assert result.specs == {}
