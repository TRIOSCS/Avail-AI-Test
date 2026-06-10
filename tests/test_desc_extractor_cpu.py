"""CPU description→spec extraction — REAL corpus strings (SFDC CPU bucket).

Verbatim positives from /root/source_ingest/analysis/cpu_rows.csv and the grammar
samples in CPU_DECODE_FEASIBILITY.md §2: the HP board-IC ``IC,uP,…`` grammar (comma
and space forms), the HP spares ``SPS-CPU``/``SPS-PROC`` grammar (underscore
decimals, ``Gz`` misspellings, glued ``E52650Lv2``, ``Xeon-G/-S/-P/-B`` letter
forms), generic model strings (E3/E5/E7, Scalable, Core iN, EPYC, Ryzen), and the
curated model→spec table merge (table UNDER desc tokens). Negatives pin the step-0
pollution deny-list (report §0/§6) and every per-grammar false-positive guard.
"""

import pytest

from app.services.desc_extractor import extract_desc
from app.services.desc_extractor.cpu import extract_cpu, is_cpu_pollution, load_model_specs

# ── verbatim corpus positives: (description, expected specs) ──────────────
CORPUS_CASES = [
    # HP board-IC grammar "IC,uP,…" — comma form, with codename
    (
        "IC,uP,CFL,i5-8400,2.8GHz,65W,9MB",
        {
            "family": "Core i-series",
            "socket": "LGA1151",
            "core_count": 6,
            "clock_speed_ghz": 2.8,
            "tdp_watts": 65,
            "architecture": "Coffee Lake",
        },
    ),
    (
        "IC,uP,CFL,i7-8700T,2.4GHz,35W,12MB",
        {
            "family": "Core i-series",
            "socket": "LGA1151",
            "core_count": 6,
            "clock_speed_ghz": 2.4,
            "tdp_watts": 35,
            "architecture": "Coffee Lake",
        },
    ),
    # "IC, uP," space-after-comma form; table fills cores/socket/architecture
    (
        "IC, uP,i5-7500T,2.7GHz,35W,6MB",
        {
            "family": "Core i-series",
            "socket": "LGA1151",
            "core_count": 4,
            "clock_speed_ghz": 2.7,
            "tdp_watts": 35,
            "architecture": "Kaby Lake",
        },
    ),
    (
        "IC, uP,i5-4690,3.5GHz,84W,6MB,C-0",
        {
            "family": "Core i-series",
            "socket": "LGA1150",
            "core_count": 4,
            "clock_speed_ghz": 3.5,
            "tdp_watts": 84,
            "architecture": "Haswell",
        },
    ),
    # "Xeon SKL" codename field + E3 v5 model
    (
        "IC,uP,Xeon SKL,E3-1225V5,3.30GHz, 80W",
        {
            "family": "Xeon",
            "socket": "LGA1151",
            "core_count": 4,
            "clock_speed_ghz": 3.3,
            "tdp_watts": 80,
            "architecture": "Skylake",
        },
    ),
    # Pentium G-models: spec tokens extract, family/model deliberately absent
    ("IC, uP,G4400,3.3GHz, 65W,3MB", {"clock_speed_ghz": 3.3, "tdp_watts": 65}),
    (
        "IC,uP,CFL,G4900,3.1GHz,54W,2MB",
        {"clock_speed_ghz": 3.1, "tdp_watts": 54, "architecture": "Coffee Lake"},
    ),
    # "x.xGHz" placeholder never parses — the table rescues the clock
    (
        "IC,uP,CFL-R,i5-9500T,x.xGHz,35W",
        {
            "family": "Core i-series",
            "socket": "LGA1151",
            "core_count": 6,
            "clock_speed_ghz": 2.2,
            "tdp_watts": 35,
            "architecture": "Coffee Lake",
        },
    ),
    # space-delimited "IC uP" form (feasibility report §2 sample)
    (
        "IC uP KBL i7-7600U 2.8GHz 15W BGA H-0: Spec code: SR3RZ",
        {"family": "Core i-series", "clock_speed_ghz": 2.8, "tdp_watts": 15, "architecture": "Kaby Lake"},
    ),
    ("IC uP i7-7800X 3.5GHz 140W M-0", {"family": "Core i-series", "clock_speed_ghz": 3.5, "tdp_watts": 140}),
    # "IC CPU" rows route via the strong CPU body token (bare "IC," stays foreign)
    (
        "IC CPU  WHL-U I5-8265U 1.6GHZ 4C BGA",
        {"family": "Core i-series", "core_count": 4, "clock_speed_ghz": 1.6},
    ),
    ("IC CPU SKL-Y  M3-6Y30 0.9GHZ 4.5W BGA D-1", {"clock_speed_ghz": 0.9, "architecture": "Skylake"}),
    # HP spares grammar — underscore decimal "1_7GHZ"
    (
        "SPS-CPU BDW E5-2650L V4 14C 1_7GHZ 65W - 835609-001",
        {
            "family": "Xeon",
            "socket": "LGA2011-3",
            "core_count": 14,
            "clock_speed_ghz": 1.7,
            "tdp_watts": 65,
            "architecture": "Broadwell",
        },
    ),
    # "Gz" misspelling + glued "E5-2673v4" generation
    (
        "SPS-CPU BDW E5-2673v4 20C 2.3Gz 50M 135W",
        {
            "family": "Xeon",
            "socket": "LGA2011-3",
            "core_count": 20,
            "clock_speed_ghz": 2.3,
            "tdp_watts": 135,
            "architecture": "Broadwell",
        },
    ),
    # composite codename "INTCFL-R"
    (
        "SPS-CPU INTCFL-R i7-9700K 8C 3.6GHz 95W",
        {
            "family": "Core i-series",
            "socket": "LGA1151",
            "core_count": 8,
            "clock_speed_ghz": 3.6,
            "tdp_watts": 95,
            "architecture": "Coffee Lake",
        },
    ),
    (
        "SPS-CPU BDW E5-2609 v4 8C 1.7GHz 85W",
        {
            "family": "Xeon",
            "socket": "LGA2011-3",
            "core_count": 8,
            "clock_speed_ghz": 1.7,
            "tdp_watts": 85,
            "architecture": "Broadwell",
        },
    ),
    (
        "SPS-CPU BDW E7-4820v4 10C 2.0GHz 115W",
        {
            "family": "Xeon",
            "core_count": 10,
            "clock_speed_ghz": 2.0,
            "tdp_watts": 115,
            "architecture": "Broadwell",
        },
    ),
    # bare Scalable model number after a CLX codename — tokens only, no model/table
    (
        "SPS-CPU CLX 4210 - 2.2GHz 85W 10C",
        {"core_count": 10, "clock_speed_ghz": 2.2, "tdp_watts": 85, "architecture": "Cascade Lake"},
    ),
    # HP "Xeon-B/-G/-P" letter forms (lowercase glued "6c", bare-G clocks)
    (
        "SPS-CPU SKL Xeon-B 3104 1.7G 6c 85W",
        {
            "family": "Xeon",
            "socket": "LGA3647",
            "core_count": 6,
            "clock_speed_ghz": 1.7,
            "tdp_watts": 85,
            "architecture": "Skylake",
        },
    ),
    (
        "SPS-CPU SKL Xeon-G 6138 20c 2.0G 125W",
        {
            "family": "Xeon",
            "socket": "LGA3647",
            "core_count": 20,
            "clock_speed_ghz": 2.0,
            "tdp_watts": 125,
            "architecture": "Skylake",
        },
    ),
    (
        "SPS-CPU SKL Xeon-P 8160 24C 2.1G 150W",
        {
            "family": "Xeon",
            "socket": "LGA3647",
            "core_count": 24,
            "clock_speed_ghz": 2.1,
            "tdp_watts": 150,
            "architecture": "Skylake",
        },
    ),
    # no clock token at all — the table supplies it (Gold 6146 base 3.2)
    (
        "SPS-CPU SKL Xeon-G 6146 12c 165W",
        {
            "family": "Xeon",
            "socket": "LGA3647",
            "core_count": 12,
            "clock_speed_ghz": 3.2,
            "tdp_watts": 165,
            "architecture": "Skylake",
        },
    ),
    (
        "SPS-PROC HSW E5-1630v3 4C 3.7GHz 140W",
        {
            "family": "Xeon",
            "socket": "LGA2011-3",
            "core_count": 4,
            "clock_speed_ghz": 3.7,
            "tdp_watts": 140,
            "architecture": "Haswell",
        },
    ),
    # bare "2.6" carries no unit — clock comes from the table; "2400" (DDR speed) ignored
    (
        "SPS-PROC XEON E5-2690V4 2.6 2400 14C",
        {
            "family": "Xeon",
            "socket": "LGA2011-3",
            "core_count": 14,
            "clock_speed_ghz": 2.6,
            "tdp_watts": 135,
            "architecture": "Broadwell",
        },
    ),
    # Xeon W: model unparsed (out of scope) but SKL-W codename + tokens extract
    (
        "SPS-PROC SKL-W W-2155 10C 3.3GHz 140W",
        {"core_count": 10, "clock_speed_ghz": 3.3, "tdp_watts": 140, "architecture": "Skylake"},
    ),
    # glued model "E52650Lv2" (no hyphen)
    (
        "SPS-PROC E52650Lv2 10C 1.7GHz 25M 70W LP",
        {
            "family": "Xeon",
            "socket": "LGA2011",
            "core_count": 10,
            "clock_speed_ghz": 1.7,
            "tdp_watts": 70,
            "architecture": "Ivy Bridge",
        },
    ),
    (
        "SPS-PROC E5-2690v2 10C 3.0GHz 25M 130W",
        {
            "family": "Xeon",
            "socket": "LGA2011",
            "core_count": 10,
            "clock_speed_ghz": 3.0,
            "tdp_watts": 130,
            "architecture": "Ivy Bridge",
        },
    ),
    # SNB codename (corpus-verified addition to the report's 7-codename map)
    (
        "SPS-Proc SNB E5-4650L 8C 2.6GHz 20M 115W",
        {
            "family": "Xeon",
            "socket": "LGA2011",
            "core_count": 8,
            "clock_speed_ghz": 2.6,
            "tdp_watts": 115,
            "architecture": "Sandy Bridge",
        },
    ),
    # glued "150W22C" compound matches NOTHING — the table fills tdp/cores instead
    (
        "SPS-CPU:2.2GHz 55M/150W22C E7-8880v4 BDW",
        {
            "family": "Xeon",
            "core_count": 22,
            "clock_speed_ghz": 2.2,
            "tdp_watts": 150,
            "architecture": "Broadwell",
        },
    ),
    # generic model strings without an SPS/IC lead
    (
        "Xeon GOLD 6134 3.2G 8C 130W",
        {
            "family": "Xeon",
            "socket": "LGA3647",
            "core_count": 8,
            "clock_speed_ghz": 3.2,
            "tdp_watts": 130,
            "architecture": "Skylake",
        },
    ),
    # glued "12CORE" + full architecture name "SKYLAKE"
    (
        "GOLD 6126 SKYLAKE CPU 2.6GHZ 19.25MB 12CORE 125W, MM 956004",
        {
            "family": "Xeon",
            "socket": "LGA3647",
            "core_count": 12,
            "clock_speed_ghz": 2.6,
            "tdp_watts": 125,
            "architecture": "Skylake",
        },
    ),
    (
        "CPU, Intel Xeon E5-2680 V3/12C/12",
        {
            "family": "Xeon",
            "socket": "LGA2011-3",
            "core_count": 12,
            "clock_speed_ghz": 2.5,
            "tdp_watts": 120,
            "architecture": "Haswell",
        },
    ),
    (
        "CPU 6 Core E5-2640 15M Cache - 2.50 GHZ 00D0017, IBM",
        {
            "family": "Xeon",
            "socket": "LGA2011",
            "core_count": 6,
            "clock_speed_ghz": 2.5,
            "tdp_watts": 95,
            "architecture": "Sandy Bridge",
        },
    ),
    # turbo parenthetical dropped — base 2.4 survives; "24 threads" ignored
    (
        "CPU, E5-2695v2, 2.4GHZ (3.2GHz Turbo) 12 cores/24 threads",
        {
            "family": "Xeon",
            "socket": "LGA2011",
            "core_count": 12,
            "clock_speed_ghz": 2.4,
            "tdp_watts": 115,
            "architecture": "Ivy Bridge",
        },
    ),
    # Microsoft OEM SKUs (corpus-corroborated table entries)
    (
        "Dell - Xeon E5-2673 v4, 2.3GHz, 20-core, 30MB Cache, 135W",
        {
            "family": "Xeon",
            "socket": "LGA2011-3",
            "core_count": 20,
            "clock_speed_ghz": 2.3,
            "tdp_watts": 135,
            "architecture": "Broadwell",
        },
    ),
    (
        "Dell - GGJDY - Xeon E7-8890 v3, 18 Core, 2.5GHz, 165W",
        {
            "family": "Xeon",
            "core_count": 18,
            "clock_speed_ghz": 2.5,
            "tdp_watts": 165,
            "architecture": "Haswell",
        },
    ),
    # "9.6GT/S" never reads as a clock; "14-CORE" hyphen form; "FCLGA2011" untouched
    (
        "INTEL XEON E5-2690V4 14-CORE 2.6GHZ 35MB L3 CACHE 9.6GT/S QPI SPEED SOCKET FCLGA2011 135W 14NM",
        {
            "family": "Xeon",
            "socket": "LGA2011-3",
            "core_count": 14,
            "clock_speed_ghz": 2.6,
            "tdp_watts": 135,
            "architecture": "Broadwell",
        },
    ),
    (
        "HP - HPE CL G3 E5-2673 v3 FIO Kit",
        {
            "family": "Xeon",
            "socket": "LGA2011-3",
            "core_count": 12,
            "clock_speed_ghz": 2.4,
            "tdp_watts": 105,
            "architecture": "Haswell",
        },
    ),
    (
        "Intel Xeon E5-1650v2 3.50Ghz 12MB 1866 6C CPU",
        {
            "family": "Xeon",
            "socket": "LGA2011",
            "core_count": 6,
            "clock_speed_ghz": 3.5,
            "tdp_watts": 130,
            "architecture": "Ivy Bridge",
        },
    ),
    # AMD
    (
        "EPYC 7402 24 CORE 2.8GHZ PROCESSOR",
        {
            "family": "EPYC",
            "socket": "SP3",
            "core_count": 24,
            "clock_speed_ghz": 2.8,
            "tdp_watts": 180,
            "architecture": "Zen 2",
        },
    ),
    # newer EPYC outside the table — family + tokens only ("(96 Threads)" ignored)
    (
        "CPU, 2.30GHz AMD EPYC 7643P 3rd Generation 48-Core (96 Threads)",
        {"family": "EPYC", "core_count": 48, "clock_speed_ghz": 2.3},
    ),
    (
        "SP AMD Ryzen 5 2400GE 3.2GHz/4C/4M/35W",
        {"family": "Ryzen", "core_count": 4, "clock_speed_ghz": 3.2, "tdp_watts": 35},
    ),
    (
        "SP AMD Ryzen 3 2200GE 3.2G/4C/4M/AM4/35W",
        {"family": "Ryzen", "core_count": 4, "clock_speed_ghz": 3.2, "tdp_watts": 35},
    ),
    # "Ryzen 3 PRO" without a model number — family word still emits
    ("CPU, AMD Ryzen 3 PRO 3.8G 4C", {"family": "Ryzen", "core_count": 4, "clock_speed_ghz": 3.8}),
    # plain model-led rows
    (
        "SP Intel i5-9400 2.9GHz/6C/9M 65W U0",
        {
            "family": "Core i-series",
            "socket": "LGA1151",
            "core_count": 6,
            "clock_speed_ghz": 2.9,
            "tdp_watts": 65,
            "architecture": "Coffee Lake",
        },
    ),
    # mobile model outside the table — family only
    ("I7-7600U PROCESSOR", {"family": "Core i-series"}),
    # TRIO lead grammar variants
    ("CPU,Exchange,1.3GHz, 3.0M Cache", {"clock_speed_ghz": 1.3}),
    ("HP PROLIANT DL360 G7  12 Core CPU, 32GB RAM", {"core_count": 12}),
    # "PROC," lead (material 589MT) — bare-G clock + table merge
    (
        "PROC,I3-7100T,3M,3.4G,35W, 3050",
        {
            "family": "Core i-series",
            "socket": "LGA1151",
            "core_count": 2,
            "clock_speed_ghz": 3.4,
            "tdp_watts": 35,
            "architecture": "Kaby Lake",
        },
    ),
    # HTML blob rows (real Material_Detail_Description__c content)
    (
        "<h2><b>Intel® Xeon® Gold 6148 Processor (27.5M Cache, 2.40 GHz) FC-LGA14B, Tray</b></h2><p><br></p>",
        {
            "family": "Xeon",
            "socket": "LGA3647",
            "core_count": 20,
            "clock_speed_ghz": 2.4,
            "tdp_watts": 150,
            "architecture": "Skylake",
        },
    ),
    (
        "<h2><b>Intel® Xeon® Silver 4208 Processor (11M Cache, 2.10 GHz) FC-LGA14B, Tray</b></h2><p><br></p>",
        {
            "family": "Xeon",
            "socket": "LGA3647",
            "core_count": 8,
            "clock_speed_ghz": 2.1,
            "tdp_watts": 85,
            "architecture": "Cascade Lake",
        },
    ),
    # "up to 4.40 GHz" is a turbo figure — clock omitted (model not in the table)
    (
        "Intel® Core? i5-12400 Processor (18M Cache, up to 4.40 GHz) FC-LGA16A, Tray",
        {"family": "Core i-series"},
    ),
]


@pytest.mark.parametrize("description,expected", CORPUS_CASES)
def test_corpus_extraction(description, expected):
    result = extract_desc(description)
    assert result is not None, f"{description!r} did not route"
    assert result.commodity == "cpu"
    assert result.specs == expected


# ── step-0 pollution deny-list: every report-documented class pinned ───────
# These are verbatim MPN-echo rows from the SFDC CPU bucket (desc == MPN). A
# cpu-categorized card with such a description must extract NOTHING — not even
# the commodity hint (extract_desc returns None outright).
POLLUTION_CASES = [
    "GRM155R71C104MA88D",  # Murata GRM MLCC
    "EEEFK1E471GP",  # Panasonic cap
    "B72220P3271K102",  # EPCOS/TDK varistor
    "06035A101JAT2A",  # AVX MLCC
    "SN74ALVC244PWR",  # TI logic
    "SMAJ24CA-13-F",  # TVS diode
    "640456-9",  # TE connector
    "1-640456-0",  # TE connector (prefixed form)
    "STK/SL500/BASE/ROHS",  # StorageTek SL500 tape-library part
    "LTO4-HP4FC-SL85Z",  # tape drive mis-bucketed as CPU
    "SL500/POWER/SUPPLY",
]


@pytest.mark.parametrize("description", POLLUTION_CASES)
def test_pollution_denied_even_with_cpu_hint(description):
    assert is_cpu_pollution(description.upper())
    assert extract_desc(description, commodity_hint="cpu") is None, f"{description!r} must be denied"


# ── per-grammar false-positive guards ──────────────────────────────────────
def test_bare_ic_lead_stays_foreign():
    # "IC,uP," is the cpu lead; a bare "IC," label is the SFDC general components
    # bin and must NOT extract (unhandled-label FOREIGN rule).
    assert extract_desc("IC, MAX3232 RS-232 Transceiver, Maxim") is None
    # The space form requires the uP token too.
    assert extract_desc("IC AMD  A8 Pro-8600B") is None


def test_ocr_junk_clocks_never_parse():
    # Real corpus rows: "2.lGHZ CPU" / "2.SGHZ CPU" (OCR-mangled digits).
    for desc in ("2.lGHZ CPU", "2.SGHZ CPU"):
        result = extract_desc(desc)
        assert result is not None and result.commodity == "cpu"
        assert result.specs == {}


def test_turbo_only_clock_keeps_base():
    # "2.3 Ghz (2.8 Ghz Turbo)" — base survives, turbo dropped (cpu hint routes
    # the otherwise token-less row).
    result = extract_desc("2.3 Ghz (2.8 Ghz Turbo)", commodity_hint="cpu")
    assert result is not None
    assert result.specs == {"clock_speed_ghz": 2.3}


def test_conflicting_models_omit_table_fields():
    # Two DIFFERENT model strings ⇒ no table lookup, family still unique (Xeon).
    result = extract_desc("CPU, E5-2620 V3 or E5-2640 V3 options", commodity_hint="cpu")
    assert result is not None
    assert "socket" not in result.specs and "core_count" not in result.specs
    assert result.specs.get("family") == "Xeon"


def test_psu_grade_platinum_never_reads_as_scalable():
    # 80-PLUS "PLATINUM"/"GOLD" + wattage shapes must not produce a cpu route or a
    # Xeon family ("PLATINUM 1100W" fails the 3-9xxx model gate; the W suffix is
    # excluded from Scalable suffix letters).
    result = extract_desc("HPE 800W FLEX SLOT PLATINUM HOT PLUG POWER SUPPLY")
    assert result is not None and result.commodity == "power_supplies"
    cpu_specs = extract_cpu("SPS-PS PLATINUM 1100W HOT PLUG")
    assert cpu_specs == {}


def test_cpu_model_words_are_subordinate_routing_tokens():
    # A motherboard row naming its CPU stays a motherboard (the i7 model must not
    # out-vote the MB token) — mirrors the GPU-vocabulary routing rule.
    result = extract_desc("SPS-MB DSC GTX1050 4GB i7-7700HQ WIN")
    assert result is not None and result.commodity == "motherboards"
    # And a server-accessory row with a foreign lead never reaches the cpu grammar.
    assert extract_desc("Fan, CPU Fan & Heatsink 12V Optiplex Dell") is None
    assert extract_desc("Cell Board, CPU and Memory") is None
    assert extract_desc("EXCHG, ASSY, CPU 1.6GHZ/18MB CACHE") is None


def test_wiznet_w5100_style_models_never_decode():
    # Report §6: bare [XLEW]#### models (X5650-era) need Xeon context — out of
    # scope for the grammar, so "W5100" (WIZnet ethernet chip) extracts nothing.
    assert extract_cpu("W5100") == {}
    result = extract_desc("W5100", commodity_hint="cpu")
    assert result is not None and result.specs == {}


def test_tdp_is_tdp_watts_never_wattage():
    # The PSU-vs-CPU structural guard, post-promotion: the cpu route emits
    # tdp_watts; the wattage key remains psu-only.
    result = extract_desc("SPS-CPU BDW E5-2673v4 20C 2.3Gz 50M 135W", commodity_hint="cpu")
    assert result is not None
    assert result.specs["tdp_watts"] == 135
    assert "wattage" not in result.specs


def test_desc_tokens_beat_the_table():
    # I5-6500T table base clock is 2.5 — the desc states 3.1 (turbo printed as the
    # headline figure on this real row) and the desc-stated value wins by contract.
    result = extract_desc("CPU, I5-6500T, 6M 3.1G, SR2BZ, CM8066201920600, Intel")
    assert result is not None
    table = load_model_specs()["I5-6500T"]
    assert table["clock_speed_ghz"] == 2.5
    assert result.specs["clock_speed_ghz"] == 3.1  # desc wins
    assert result.specs["core_count"] == table["core_count"]  # table fills the gap
    assert result.specs["socket"] == table["socket"]


def test_table_lookup_requires_unique_model_and_exact_key():
    specs = load_model_specs()
    assert "E5-2650L V4" in specs and "GOLD 6248" in specs and "EPYC 7551P" in specs
    # ~150+ curated entries (PR contract: the starter table must stay substantial)
    assert len(specs) >= 150


def test_spaced_mb_cache_is_a_known_conservative_loss():
    # "128 MB L3" hits the motherboards \bMB\b routing token alongside the cpu
    # PROCESSOR token — ambiguous body tokens with no lead to arbitrate return
    # None by design (the router never picks a side). Pinned so a future router
    # change that silently flips this row is a conscious decision.
    assert extract_desc("AMD EPYC 7443P processor 2.85 GHz 128 MB L3") is None
    # A cpu hint resolves it: the card category arbitrates and the row decodes.
    hinted = extract_desc("AMD EPYC 7443P processor 2.85 GHz 128 MB L3", commodity_hint="cpu")
    assert hinted is not None
    assert hinted.specs == {"family": "EPYC", "clock_speed_ghz": 2.85}


def test_glued_core_token_word_bounded():
    # "53C4"/"I2C" shapes never read as core counts; "150W22C" compounds match
    # neither the TDP nor the cores grammar (no word boundaries).
    assert extract_cpu("CARD 53C4 PSERIES") == {}
    assert extract_cpu("I2C CONTROLLER") == {}


def test_mpn_echo_rows_need_cpu_context_for_bare_tokens():
    # Verbatim hint-routed MPN-echo rows from the bucket: "-1C-" and "24W" shapes
    # match the bare grammars but carry NO cpu-context signal — emit nothing.
    for echo in ("812H-1C-CEF12VDC", "EPS4-24W"):
        result = extract_desc(echo, commodity_hint="cpu")
        assert result is not None and result.specs == {}, echo
    # With context the same shapes emit: "...DUAL CORE CPU" / GHz-bearing rows.
    result = extract_desc("1.6GHZ ITANIUM2 DUAL CORE CPU", commodity_hint="cpu")
    assert result is not None and result.specs == {"clock_speed_ghz": 1.6}


def test_threadripper_subsumes_ryzen_family_word():
    # "Ryzen Threadripper" is one brand, not a family conflict; Threadripper and
    # Atom are seeded family members emitted from their explicit words.
    result = extract_desc("CPU, AMD, Threadripper Pro 3955WX 3.9GHz")
    assert result is not None
    assert result.specs == {"family": "Threadripper", "clock_speed_ghz": 3.9}
    result = extract_desc("CPU, AMD Ryzen Threadripper 3960X 3.8GHz 24C")
    assert result is not None
    assert result.specs == {"family": "Threadripper", "core_count": 24, "clock_speed_ghz": 3.8}
