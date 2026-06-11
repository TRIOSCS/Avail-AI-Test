"""tests/test_source_ingest_consolidate.py — SP-Ingest consolidate.

Covers: app/services/source_ingest/consolidate.py — longest-description wins, modal
manufacturer, first canonical category, most-common condition, quantity sum, and
SFDC-master > inventory-sheet spec priority.
"""

from __future__ import annotations

from app.services.source_ingest.consolidate import consolidate
from app.services.source_ingest.models import (
    SOURCE_KIND_INVENTORY_SHEET,
    SOURCE_KIND_SFDC_MASTER,
    SourceRecord,
)


def _rec(kind=SOURCE_KIND_INVENTORY_SHEET, **kw) -> SourceRecord:
    base = dict(raw_mpn="ST4000NM0035", normalized_mpn="st4000nm0035", source_kind=kind, source_file="f")
    base.update(kw)
    return SourceRecord(**base)


def test_consolidate_groups_by_normalized_mpn():
    parts = consolidate([_rec(), _rec(), _rec(normalized_mpn="other", raw_mpn="OTHER1")])
    assert len(parts) == 2
    assert {p.normalized_mpn for p in parts} == {"st4000nm0035", "other"}
    by_mpn = {p.normalized_mpn: p for p in parts}
    assert by_mpn["st4000nm0035"].record_count == 2


def test_consolidate_longest_description_wins():
    parts = consolidate(
        [
            _rec(description="4TB HDD"),
            _rec(description="4TB Enterprise 7.2K SAS 3.5in Hard Disk Drive"),
        ]
    )
    assert parts[0].description == "4TB Enterprise 7.2K SAS 3.5in Hard Disk Drive"
    assert parts[0].field_sources["description"] == SOURCE_KIND_INVENTORY_SHEET


def test_consolidate_modal_manufacturer():
    parts = consolidate(
        [
            _rec(manufacturer="IBM"),
            _rec(manufacturer="IBM"),
            _rec(manufacturer="Lenovo"),
        ]
    )
    assert parts[0].manufacturer == "IBM"  # modal


def test_consolidate_most_common_condition_and_qty_sum():
    parts = consolidate(
        [
            _rec(condition="Pulled", quantity=10),
            _rec(condition="Pulled", quantity=5),
            _rec(condition="New", quantity=2),
        ]
    )
    assert parts[0].condition == "Pulled"  # most common
    assert parts[0].quantity == 17  # summed
    assert parts[0].field_sources["quantity"] == "merged_sum"


def test_sfdc_master_without_condition_never_outvotes_real_sheet_condition():
    # The SFDC part master NEVER carries a condition (it is per-unit, not on the master) —
    # clean_record leaves it None, so the master contributes NO vote and the inventory
    # sheet's single real condition wins. A synthetic "Unknown" from the master would
    # otherwise win the modal tie-break via sfdc_master priority and discard the real value.
    parts = consolidate(
        [
            _rec(condition=None, kind=SOURCE_KIND_SFDC_MASTER),
            _rec(condition="Pulled", kind=SOURCE_KIND_INVENTORY_SHEET),
        ]
    )
    assert parts[0].condition == "Pulled"
    assert parts[0].field_sources["condition"] == SOURCE_KIND_INVENTORY_SHEET


def test_consolidate_first_canonical_category():
    parts = consolidate([_rec(category="hdd"), _rec(category="ssd")])
    assert parts[0].category == "hdd"


def test_consolidate_sfdc_master_specs_win_over_sheet():
    # Same spec_key from both kinds → SFDC master value wins.
    parts = consolidate(
        [
            _rec(kind=SOURCE_KIND_INVENTORY_SHEET, specs={"capacity_gb": "3000"}),
            _rec(kind=SOURCE_KIND_SFDC_MASTER, specs={"capacity_gb": "4000", "rpm": "7200"}),
        ]
    )
    assert parts[0].specs["capacity_gb"] == "4000"  # master wins
    assert parts[0].specs["rpm"] == "7200"
    assert parts[0].field_sources["spec:capacity_gb"] == SOURCE_KIND_SFDC_MASTER


def test_consolidate_manufacturer_tiebreak_prefers_master():
    # 1 vote each → tie broken by source-kind priority (master > sheet).
    parts = consolidate(
        [
            _rec(kind=SOURCE_KIND_INVENTORY_SHEET, manufacturer="IBM"),
            _rec(kind=SOURCE_KIND_SFDC_MASTER, manufacturer="Seagate"),
        ]
    )
    assert parts[0].manufacturer == "Seagate"


def test_consolidate_warns_and_counts_uncleaned_records():
    # A record with an empty normalized_mpn bypassed clean_record (pipeline wiring bug:
    # parse → consolidate directly). It must be skipped WITH a WARNING, never silently.
    from loguru import logger as loguru_logger

    raw = SourceRecord(raw_mpn="ST4000NM0035")  # parser-shaped: normalized_mpn == ""
    cleaned = _rec()

    warnings: list[str] = []
    sink_id = loguru_logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        parts = consolidate([raw, cleaned])
    finally:
        loguru_logger.remove(sink_id)

    assert len(parts) == 1  # the un-cleaned record never becomes a part
    assert any("bypassed clean_record" in w for w in warnings), warnings


def test_consolidate_modal_brand_with_provenance():
    # Dual-brand: brand (OEM label) is picked exactly like manufacturer — modal
    # non-empty with source-kind priority tie-break — and recorded in field_sources.
    parts = consolidate(
        [
            _rec(brand="IBM"),
            _rec(brand="IBM"),
            _rec(brand="Dell"),
        ]
    )
    assert parts[0].brand == "IBM"  # modal
    assert parts[0].field_sources["brand"] == SOURCE_KIND_INVENTORY_SHEET


def test_consolidate_brand_absent_stays_none():
    parts = consolidate([_rec(), _rec()])
    assert parts[0].brand is None
    assert "brand" not in parts[0].field_sources
