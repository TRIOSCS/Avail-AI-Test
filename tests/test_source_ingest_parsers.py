"""tests/test_source_ingest_parsers.py — SP-Ingest parsers (sheet + SFDC master).

Covers: app/services/source_ingest/parsers.py — header-detected inventory-sheet parsing
across .csv/.txt and SFDC part-master streaming (IsDeleted skip, OEM/description/category
fallbacks, deep-facet emission). Fixtures are tiny fake CSVs + a staged-style .txt capture.
"""

from __future__ import annotations

from pathlib import Path

from app.services.source_ingest.models import (
    SOURCE_KIND_INVENTORY_SHEET,
    SOURCE_KIND_SFDC_MASTER,
)
from app.services.source_ingest.parsers import (
    parse_inventory_sheet,
    parse_sfdc_material_master,
)

_SFDC_HEADER = (
    "Id,IsDeleted,LSC1__Material_Number__c,LSC1__OEM__c,Brand__c,"
    "LSC1__Manufacturer_Brand__c,Material_Description__c,"
    "LSC1__Material_Detail_Description__c,LSC1__Material_Short_Description__c,"
    "LSC1__Common_Name__c,LSC1__Category__c,Commodity_Code__c,"
    "LSC1__Total_Available_Inventory__c,Capacity__c,Legacy_RPM__c,Form_Factor__c"
)


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_inventory_csv_maps_columns(tmp_path):
    csv_path = _write(
        tmp_path / "inv.csv",
        [
            "Part Number,ProductDescription,Whse,Condition,Commodity,On Hand,Last Cost",
            '00AR327 - Pull,"HDD, 6Gbps 1.2TB 10K 2.5 Inch HDD, IBM",4SALE,Pull,HDD,354,$4.00',
            "005052089 - Pull,4TB 7.2K Rpm 3.5inch 12gbps Sas HDD,4SALE,Pull,HDD,142,$0.00",
        ],
    )
    recs = list(parse_inventory_sheet(csv_path))
    assert len(recs) == 2
    r0 = recs[0]
    assert r0.raw_mpn == "00AR327 - Pull"  # suffix still present (clean.py strips it)
    assert r0.description == "HDD, 6Gbps 1.2TB 10K 2.5 Inch HDD, IBM"
    assert r0.condition == "Pull"
    assert r0.category == "HDD"
    assert r0.quantity == 354
    assert r0.source_kind == SOURCE_KIND_INVENTORY_SHEET
    assert r0.source_file == "inv.csv"


def test_parse_inventory_firesale_description_header(tmp_path):
    # Firesale uses "Description" (not "ProductDescription") and a "On Hand" with thousands sep.
    csv_path = _write(
        tmp_path / "fire.csv",
        [
            "Part Number,Description,Whse,Condition,Commodity,On Hand",
            'BLM21SP331SH1D,"Ferrite Beads 330 OHM, Murata Electronics",4SALE,New,Other,"2,936"',
        ],
    )
    recs = list(parse_inventory_sheet(csv_path))
    assert len(recs) == 1
    assert recs[0].quantity == 2936
    assert recs[0].category == "Other"


def test_parse_inventory_txt_capture_skips_preamble(tmp_path):
    # The staged .txt captures carry a prose preamble before the real tab-delimited header.
    txt = _write(
        tmp_path / "Inventory_sample.txt",
        [
            "SOURCE: OneDrive — Inventory 2.12.26.xlsx",
            "Fetched via MCP read_resource — TRUNCATED.",
            "Columns: Part Number | ProductDescription | Whse | Condition | Commodity | On Hand",
            "",
            "Part Number\tProductDescription\tWhse\tCondition\tCommodity\tOn Hand\tInventory Days\tLast Cost",
            "00AJ660 - Pull\tMemory, 16GB MEMORY DDR3 1600MHZ, IBM\t4SALE\tPull\tMEMORY\t6\t20\t$7.62",
            "(... continues; tool truncated.)",
        ],
    )
    recs = list(parse_inventory_sheet(txt))
    assert len(recs) == 1  # preamble + trailing note ignored; one real data row
    assert recs[0].raw_mpn == "00AJ660 - Pull"
    assert recs[0].quantity == 6


def test_parse_sfdc_master_maps_and_emits_specs(tmp_path):
    csv_path = _write(
        tmp_path / "LSC1__Material__c.csv",
        [
            _SFDC_HEADER,
            'a01,false,ST4000NM0035,Seagate,,,4TB Enterprise HDD,,,,hdd,,12,4000,7200,"3.5"""',
        ],
    )
    recs = list(parse_sfdc_material_master(csv_path))
    assert len(recs) == 1
    r = recs[0]
    assert r.raw_mpn == "ST4000NM0035"
    assert r.manufacturer == "Seagate"  # LSC1__OEM__c primary
    assert r.description == "4TB Enterprise HDD"
    assert r.category == "hdd"
    assert r.quantity == 12
    assert r.source_kind == SOURCE_KIND_SFDC_MASTER
    # Deep facets mapped to app spec_keys, only non-empty emitted.
    assert r.specs["capacity_gb"] == "4000"
    assert r.specs["rpm"] == "7200"
    assert r.specs["form_factor"] == '3.5"'


def test_parse_sfdc_master_skips_isdeleted(tmp_path):
    csv_path = _write(
        tmp_path / "LSC1__Material__c.csv",
        [
            _SFDC_HEADER,
            "a01,true,DELETED-001,IBM,,,Should be skipped,,,,hdd,,,,,",
            "a02,false,KEEP-002,IBM,,,Keep me,,,,hdd,,,,,",
        ],
    )
    recs = list(parse_sfdc_material_master(csv_path))
    assert [r.raw_mpn for r in recs] == ["KEEP-002"]


def test_parse_sfdc_oem_and_description_fallbacks(tmp_path):
    # OEM falls back to Brand__c; description falls back to detail/short/common name.
    csv_path = _write(
        tmp_path / "LSC1__Material__c.csv",
        [
            _SFDC_HEADER,
            "a01,false,FB-001,,DellBrand,,,Detail desc here,,,memory,,,,,",
        ],
    )
    r = list(parse_sfdc_material_master(csv_path))[0]
    assert r.manufacturer == "DellBrand"
    assert r.description == "Detail desc here"
    assert r.category == "memory"
