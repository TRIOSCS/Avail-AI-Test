"""tests/test_source_ingest_clean.py — SP-Ingest clean_record.

Covers: app/services/source_ingest/clean.py — MPN suffix strip + normalize_mpn_key,
trailing-OEM extraction, _x000D_/control scrub, "DO NOT USE" drop, <3-char drop, condition
canon, and source→canonical category mapping.
"""

from __future__ import annotations

from app.services.source_ingest.clean import (
    canonicalize_condition,
    clean_record,
    extract_trailing_oem,
    strip_mpn_suffix,
)
from app.services.source_ingest.models import (
    SOURCE_KIND_INVENTORY_SHEET,
    SourceRecord,
)


def _rec(**kw) -> SourceRecord:
    base = dict(raw_mpn="ABC123", source_kind=SOURCE_KIND_INVENTORY_SHEET, source_file="x.csv")
    base.update(kw)
    return SourceRecord(**base)


def test_strip_mpn_suffix_variants():
    assert strip_mpn_suffix("00AR327 - Pull") == "00AR327"
    assert strip_mpn_suffix("657239-001 - New") == "657239-001"
    assert strip_mpn_suffix("P13198-001 - PULL") == "P13198-001"
    assert strip_mpn_suffix("ABC-x") == "ABC"
    assert strip_mpn_suffix("ST4000NM0035") == "ST4000NM0035"  # no suffix


def test_clean_strips_suffix_and_normalizes_key():
    c = clean_record(_rec(raw_mpn="00AR327 - Pull"))
    assert c is not None
    assert c.normalized_mpn == "00ar327"  # normalize_mpn_key: lowercase, strip non-alnum
    assert c.raw_mpn == "00AR327"  # display form (normalize_mpn) — suffix gone


def test_clean_drops_short_mpn():
    assert clean_record(_rec(raw_mpn="AB")) is None  # < 3 chars after normalize
    assert clean_record(_rec(raw_mpn="-x")) is None  # empties out


def test_clean_drops_do_not_use():
    assert clean_record(_rec(raw_mpn="REAL123", description="DO NOT USE - obsolete")) is None
    assert clean_record(_rec(raw_mpn="REAL123", description="do not use, scrap")) is None


def test_clean_scrubs_x000d_and_control_chars():
    c = clean_record(_rec(raw_mpn="REAL123", description="HDD line1_x000D_line2\twith\x07ctrl"))
    assert c is not None
    assert "_x000D_" not in c.description
    assert "\x07" not in c.description
    assert c.description == "HDD line1 line2 with ctrl"


def test_extract_trailing_oem():
    assert extract_trailing_oem("HDD, 6Gbps 1.2TB 10K 2.5 Inch HDD, IBM") == "IBM"
    assert extract_trailing_oem("SSD, 100GB SFF SAS SSD, EMC") == "EMC"
    assert extract_trailing_oem("Other, Power Cable Cord, HP") == "HP"
    # Multi-word OEM token still accepted (<=4 words).
    assert extract_trailing_oem("Ferrite Beads 330 OHM, Murata Electronics") == "Murata Electronics"
    # No comma → no embedded OEM.
    assert extract_trailing_oem("4TB 7.2K Rpm 3.5inch 12gbps Sas HDD") is None
    # Trailing token is pure measurement → not an OEM.
    assert extract_trailing_oem('Cable, 3.5"') is None


def test_extract_trailing_oem_toshiba_packing_suffix_never_leaks():
    # ROOT CAUSE of the live "F)" (360 cards) / "F" (111) manufacturer garbage: Toshiba
    # ordering codes used verbatim as descriptions carry a comma INSIDE the parenthesized
    # packing suffix — "TLP781(D4-GR-TP6,F)" must never mint manufacturer "F)".
    assert extract_trailing_oem("TLP781(D4-GR-TP6,F)") is None  # balanced suffix
    assert extract_trailing_oem("2SC4116-Y(T5LND,F)") is None
    assert extract_trailing_oem("TA78L18F(TE12L,F") is None  # truncated suffix (no close)
    assert extract_trailing_oem("TLP183(GB-TPL,E(T") is None  # the live "E(T" shape
    assert extract_trailing_oem("RN1302 T5RCANO,F") is None  # bare 1-char fragment
    # A comma-bearing parenthetical inside the trailing token is a spec list, not a maker.
    assert extract_trailing_oem("Lenovo - CBL-ASSY, 003.00 IN, LED (5P/3P/2P,3.3V,1A)") is None
    assert extract_trailing_oem("SSD, 480GB 6Gb SATA 2.5, No Tray (00LF232, SL10A28870)") is None


def test_extract_trailing_oem_splits_at_paren_depth_zero_only():
    # A parenthesized spec list mid-description must not hide the real trailing maker.
    assert extract_trailing_oem("HDD, 300GB (SED, FIPS), Seagate") == "Seagate"
    # Balanced parenthetical WITHOUT a comma is still a plausible maker form.
    assert extract_trailing_oem("Resistor 10K, Texas Instruments (TI)") == "Texas Instruments (TI)"


def test_clean_record_toshiba_ordering_code_leaves_manufacturer_empty():
    # End-to-end: the Firesale/Foxconn rows whose "description" is the bare Toshiba
    # ordering code must yield NO manufacturer (the pre-fix parser wrote "F").
    c = clean_record(_rec(raw_mpn="TLP781(D4-GR-TP6,F)", description="TLP781(D4-GR-TP6,F"))
    assert c is not None
    assert c.manufacturer is None
    assert c.brand is None


def test_clean_routes_trailing_oem_label_to_brand():
    # Dual-brand: a trailing token in the literal OEM-label list (OEM_TRAILING_RE) is
    # BRAND evidence, never a maker — manufacturer stays empty for B2/W4 to fill.
    c = clean_record(_rec(raw_mpn="00AR327", description="HDD, 1.2TB 10K HDD, IBM"))
    assert c is not None
    assert c.brand == "IBM"
    assert c.manufacturer is None


def test_clean_fills_manufacturer_from_non_oem_trailing_token():
    # A trailing token OUTSIDE the OEM-label list keeps the legacy behavior: it fills
    # manufacturer when the source carried none (EMC is not an OEM_BRANDS member).
    c = clean_record(_rec(raw_mpn="00AR327", description="SSD, 100GB SFF SAS SSD, EMC"))
    assert c is not None
    assert c.manufacturer == "EMC"
    assert c.brand is None


def test_clean_keeps_explicit_manufacturer_over_trailing():
    c = clean_record(_rec(raw_mpn="ST4000NM0035", manufacturer="Seagate", description="4TB HDD, IBM"))
    assert c is not None
    assert c.manufacturer == "Seagate"  # explicit maker kept
    assert c.brand == "IBM"  # the trailing OEM label still lands as brand evidence


def test_clean_explicit_brand_column_wins_over_trailing_token():
    c = clean_record(_rec(raw_mpn="00AR327", brand="Dell", description="HDD, 1.2TB, IBM"))
    assert c is not None
    assert c.brand == "Dell"  # explicit source column beats the description regex


def test_canonicalize_condition():
    # Canon is the MaterialCard.condition documented vocabulary (constants.MaterialCondition):
    # "Pull" maps to the column's canonical "Pulled", and "Recertified" is reachable.
    assert canonicalize_condition("New") == "New"
    assert canonicalize_condition("Pull") == "Pulled"
    assert canonicalize_condition("Pulled") == "Pulled"
    assert canonicalize_condition("Refurbished") == "Refurbished"
    assert canonicalize_condition("Recertified") == "Recertified"
    assert canonicalize_condition("Factory Recertified") == "Recertified"
    assert canonicalize_condition("Used") == "Used"
    assert canonicalize_condition("Factory New") == "New"
    # Absent / unrecognized input → None (the column stays NULL), NEVER a synthetic
    # "Unknown" — an Unknown would outvote real sheet conditions in consolidation and
    # permanently occupy the fill-only-when-empty card column.
    assert canonicalize_condition("Other") is None
    assert canonicalize_condition(None) is None
    assert canonicalize_condition("") is None


def test_clean_maps_category_canonical_or_none():
    # "HDD" → canonical "hdd"; an unmappable commodity → None (caller leaves untouched).
    assert clean_record(_rec(raw_mpn="REAL123", category="HDD")).category == "hdd"
    assert clean_record(_rec(raw_mpn="REAL123", category="VPD Card")).category is None
    assert clean_record(_rec(raw_mpn="REAL123", category=None)).category is None


def test_clean_resolves_trio_scoped_commodity_codes():
    # The ingest routes through normalize_trio_category: codes that are only unambiguous
    # inside TRIO's SFDC export (bare "Memory" is always a DRAM module there) resolve via
    # the TRIO-scoped map, and global codes still fall through to the shared alias path.
    assert clean_record(_rec(raw_mpn="REAL123", category="Memory")).category == "dram"
    assert clean_record(_rec(raw_mpn="REAL123", category="Hard Drive")).category == "hdd"
    assert clean_record(_rec(raw_mpn="REAL123", category="Main Board")).category == "motherboards"


def test_clean_blanks_other_commodity_code():
    # TRIO's 'Other' code carries no classification signal — a tier-95 "other" category
    # would permanently block decode/desc/AI re-homing, so it is blanked (card stays in
    # the no-commodity bucket). Real coarse codes (IC, OEM ASSY) still map.
    assert clean_record(_rec(raw_mpn="85Y6185", category="Other")).category is None
    assert clean_record(_rec(raw_mpn="REAL123", category="IC")).category == "ics_other"
    assert clean_record(_rec(raw_mpn="REAL123", category="OEM ASSY")).category == "oem_assemblies"


def test_clean_blanks_cpu_category_for_polluted_mpn_shapes():
    # CATALOG.md ingest warning: ~14% of the SFDC 'CPU' bucket is passives/connectors/logic.
    # Known non-CPU MPN shapes must get their category BLANKED (None — never a tier-95 'cpu'),
    # because only manual (100) outranks trio_source (95) on the ladder.
    polluted = [
        "GRM155R71C104MA88D",  # Murata MLCC
        "EEEFK1E471GP",  # Panasonic cap
        "SN74ALVC244PWR",  # TI logic
        "SMAJ24CA-13-F",  # TVS diode
        "B72220P3232S260",  # EPCOS varistor
        "06035A101JAT2A",  # AVX chip cap
        "640456-9",  # TE connector (single trailing digit)
        "1-640456-0",  # TE connector (prefixed form)
    ]
    for mpn in polluted:
        rec = clean_record(_rec(raw_mpn=mpn, category="CPU"))
        assert rec is not None and rec.category is None, mpn


def test_clean_keeps_cpu_category_for_plausible_cpu_mpns():
    # Real CPU-shaped MPNs keep the category: Intel s-spec, Intel ordering code, HP spare
    # (three-char dash suffix — distinct from TE's single trailing digit), IBM FRU.
    for mpn in ["SR3QS", "CM8068403358316", "732505-001", "01EF243"]:
        rec = clean_record(_rec(raw_mpn=mpn, category="CPU"))
        assert rec is not None and rec.category == "cpu", mpn
