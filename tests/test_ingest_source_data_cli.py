"""tests/test_ingest_source_data_cli.py — SP-Ingest CLI orchestration (pipeline wiring).

Covers: app/management/ingest_source_data.py — run() wires parse → clean → consolidate →
ingest end-to-end (every stage is unit-tested elsewhere; THIS file pins the composition:
records flow through clean_record before consolidate, the SFDC-name-hint routes the master
through the streaming parser while sheets go through the header detector, --limit stops
parsing early, and the report shape feeds _print_report). Uses tmp_path fixture files and
the test session (SessionLocal patched).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.management.ingest_source_data import _discover_files, run
from app.services.commodity_registry import seed_commodity_schemas

_INVENTORY_CSV = (
    "Part Number,ProductDescription,Condition,Commodity,On Hand\n"
    'ST4000NM0035,"HDD, 4TB 7.2K SAS, Seagate",Pull,Hard Drive,5\n'
)

_SFDC_CSV = (
    "Id,IsDeleted,LSC1__Material_Number__c,Material_Description__c,"
    "LSC1__Category__c,Commodity_Code__c,LSC1__Total_Available_Inventory__c\n"
    "a1,0,ST4000NM0035,Enterprise HDD 4TB 7.2K SAS 3.5in,,Hard Drive,2\n"
    "a2,0,M393A2K40CB2-CTD,16GB DDR4 2666 RDIMM,,Memory,1\n"
)


class _NoCloseSession:
    """Run() closes its session in a finally — the test session must survive that."""

    def __init__(self, session: Session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:  # keep the fixture session usable for assertions
        pass


def _patch_session(monkeypatch, db_session: Session) -> None:
    import app.database as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _NoCloseSession(db_session))


async def test_run_wires_parse_clean_consolidate_ingest(tmp_path, db_session: Session, monkeypatch):
    seed_commodity_schemas(db_session)
    (tmp_path / "Inventory 2.12.26.csv").write_text(_INVENTORY_CSV, encoding="utf-8")
    (tmp_path / "LSC1__Material__c.csv").write_text(_SFDC_CSV, encoding="utf-8")
    _patch_session(monkeypatch, db_session)

    report = await run(pattern=f"{tmp_path}/*", ai_correct_flag=False, apply=False, limit=None)

    assert sorted(report["files"]) == ["Inventory 2.12.26.csv", "LSC1__Material__c.csv"]
    assert report["raw_rows"] == 3  # 1 sheet row + 2 master rows
    assert report["cleaned_rows"] == 3  # all clean (a wiring bug dropping normalized_mpn → 0)
    assert report["distinct_mpns"] == 2  # ST4000NM0035 consolidated across both files
    assert report["apply"] is False
    assert report["ai_stats"] is None

    stats = report["stats"]
    assert stats["parts_seen"] == 2
    assert stats["would_create"] == 2
    assert stats["failed"] == 0
    # The TRIO-scoped vocabulary engaged through clean_record: "Hard Drive" → hdd and the
    # SFDC master's bare "Memory" → dram (both counted as trio_source category fills).
    assert stats["categories_set"] == 2
    assert stats["fields_by_source"]["trio_source"] >= 2
    # The sheet's "Pull" canonicalized to "Pulled" and survived consolidation with the
    # condition-less master.
    assert stats["conditions_filled"] == 1
    sample_by_mpn = {row["normalized_mpn"]: row for row in stats["sample"]}
    assert sample_by_mpn["st4000nm0035"]["category"] == "hdd"
    assert sample_by_mpn["st4000nm0035"]["condition"] == "Pulled"
    assert sample_by_mpn[[k for k in sample_by_mpn if k.startswith("m393a")][0]]["category"] == "dram"


async def test_run_limit_stops_parsing_early(tmp_path, db_session: Session, monkeypatch):
    seed_commodity_schemas(db_session)
    (tmp_path / "Inventory 2.12.26.csv").write_text(_INVENTORY_CSV, encoding="utf-8")
    (tmp_path / "LSC1__Material__c.csv").write_text(_SFDC_CSV, encoding="utf-8")
    _patch_session(monkeypatch, db_session)

    report = await run(pattern=f"{tmp_path}/*", ai_correct_flag=False, apply=False, limit=1)

    assert report["raw_rows"] == 1  # early-stop after the first raw row
    assert report["distinct_mpns"] == 1


def test_discover_files_routes_master_lookup_and_sheets(tmp_path):
    (tmp_path / "Inventory 2.12.26.csv").write_text("x", encoding="utf-8")
    (tmp_path / "LSC1__Material__c.csv").write_text("x", encoding="utf-8")
    (tmp_path / "LSC1__Manufacturers__c.csv").write_text("x", encoding="utf-8")
    (tmp_path / "CATALOG.md").write_text("docs", encoding="utf-8")

    files, manufacturers = _discover_files(f"{tmp_path}/*")

    names = [f.name for f in files]
    assert "LSC1__Material__c.csv" in names  # master parses (streamed)
    assert "Inventory 2.12.26.csv" in names  # sheet parses
    assert "CATALOG.md" not in names  # docs skipped
    assert manufacturers is not None and manufacturers.name == "LSC1__Manufacturers__c.csv"
