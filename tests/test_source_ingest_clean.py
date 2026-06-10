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


def test_clean_fills_manufacturer_from_trailing_oem():
    c = clean_record(_rec(raw_mpn="00AR327", description="HDD, 1.2TB 10K HDD, IBM"))
    assert c is not None
    assert c.manufacturer == "IBM"


def test_clean_keeps_explicit_manufacturer_over_trailing():
    c = clean_record(_rec(raw_mpn="ST4000NM0035", manufacturer="Seagate", description="4TB HDD, IBM"))
    assert c is not None
    assert c.manufacturer == "Seagate"  # explicit OEM wins; trailing token ignored


def test_canonicalize_condition():
    assert canonicalize_condition("New") == "New"
    assert canonicalize_condition("Pull") == "Pull"
    assert canonicalize_condition("Refurbished") == "Refurbished"
    assert canonicalize_condition("Used") == "Used"
    assert canonicalize_condition("Factory New") == "New"
    assert canonicalize_condition("Other") == "Unknown"
    assert canonicalize_condition(None) == "Unknown"
    assert canonicalize_condition("") == "Unknown"


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
