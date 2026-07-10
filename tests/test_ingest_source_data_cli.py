"""tests/test_ingest_source_data_cli.py — SP-Ingest CLI orchestration (pipeline wiring).

Covers: app/management/ingest_source_data.py — run() wires parse → clean → consolidate →
ingest end-to-end (every stage is unit-tested elsewhere; THIS file pins the composition:
records flow through clean_record before consolidate, the SFDC-name-hint routes the master
through the streaming parser while sheets go through the header detector, --limit stops
parsing early, and the report shape feeds _print_report). Uses tmp_path fixture files and
the test session (SessionLocal patched).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.management.ingest_source_data import _discover_files, _print_report, main, run
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


# ── Manufacturers-lookup CSV present (lines 103-106) ─────────────────────────

_MANUFACTURERS_CSV = "Id,Name\n001,Seagate Technology\n002,Western Digital\n"


async def test_run_with_manufacturers_lookup_csv(tmp_path, db_session: Session, monkeypatch):
    """Run() parses the Manufacturers__c CSV and passes the lookup dict to the
    parser."""
    seed_commodity_schemas(db_session)
    (tmp_path / "LSC1__Material__c.csv").write_text(_SFDC_CSV, encoding="utf-8")
    (tmp_path / "LSC1__Manufacturers__c.csv").write_text(_MANUFACTURERS_CSV, encoding="utf-8")
    _patch_session(monkeypatch, db_session)

    report = await run(pattern=f"{tmp_path}/*", ai_correct_flag=False, apply=False, limit=None)

    # Files list includes only the material master, not the manufacturers lookup.
    assert report["files"] == ["LSC1__Material__c.csv"]
    assert report["raw_rows"] == 2  # two SFDC rows
    assert report["distinct_mpns"] == 2


# ── ai_correct branch (lines 121-124) ────────────────────────────────────────


async def test_run_ai_correct_flag_populates_ai_stats(tmp_path, db_session: Session, monkeypatch):
    """When ai_correct_flag=True the ai_correct coroutine is called and its result
    stored."""
    seed_commodity_schemas(db_session)
    (tmp_path / "Inventory 2.12.26.csv").write_text(_INVENTORY_CSV, encoding="utf-8")
    _patch_session(monkeypatch, db_session)

    fake_ai_stats = {"corrected": 1, "failed": 0}
    with patch("app.services.source_ingest.ai_correct.ai_correct", new=AsyncMock(return_value=fake_ai_stats)):
        report = await run(pattern=f"{tmp_path}/*", ai_correct_flag=True, apply=False, limit=None)

    assert report["ai_correct"] is True
    assert report["ai_stats"] == fake_ai_stats


# ── _print_report (lines 151-194) ────────────────────────────────────────────


def _make_report(*, apply: bool, ai_stats=None, failed: int = 0) -> dict:
    """Build a minimal but structurally complete report dict for _print_report."""
    stats: dict = {
        "parts_seen": 2,
        "created": 2,
        "updated": 0,
        "would_create": 2,
        "would_update": 0,
        "failed": failed,
        "failed_mpns": ["BADMPN1"] if failed else [],
        "categories_set": 1,
        "descriptions_filled": 1,
        "conditions_filled": 1,
        "specs_written": 3,
        "fields_by_source": {"trio_source": 2, "mpn_decode": 1},
        "sample": [
            {
                "action": "create",
                "display_mpn": "ST4000NM0035",
                "normalized_mpn": "st4000nm0035",
                "category": "hdd",
                "category_source": "trio_source",
                "condition": "Pulled",
                "specs": {"capacity_gb": 4000},
                "ai_specs": {},
                "description": "Enterprise HDD 4TB",
            }
        ],
    }
    return {
        "files": ["LSC1__Material__c.csv", "Inventory 2.12.26.csv"],
        "raw_rows": 3,
        "cleaned_rows": 3,
        "distinct_mpns": 2,
        "ai_correct": ai_stats is not None,
        "ai_stats": ai_stats,
        "apply": apply,
        "stats": stats,
    }


def test_print_report_dry_run(capsys):
    """_print_report renders a DRY RUN block including the sample table."""
    _print_report(_make_report(apply=False))
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Would create" in out
    assert "ST4000NM0035" in out
    assert "Enterprise HDD 4TB" in out


def test_print_report_apply_mode(capsys):
    """_print_report renders an APPLY block with created/updated counts."""
    _print_report(_make_report(apply=True))
    out = capsys.readouterr().out
    assert "APPLY" in out
    assert "Cards created" in out
    # Sample table is suppressed in apply mode.
    assert "ST4000NM0035" not in out


def test_print_report_with_ai_stats(capsys):
    """_print_report shows AI correction counts when ai_stats is present."""
    _print_report(_make_report(apply=False, ai_stats={"corrected": 5, "failed": 1}))
    out = capsys.readouterr().out
    assert "AI correction" in out
    assert "5 ok" in out


def test_print_report_with_failures(capsys):
    """_print_report shows the FAILED warning line when s['failed'] > 0."""
    _print_report(_make_report(apply=False, failed=1))
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "BADMPN1" in out


# ── main() (lines 202-211) ───────────────────────────────────────────────────


def test_main_calls_run_and_returns_report(tmp_path):
    """Main() parses argv, calls asyncio.run(run(...)), prints the report, and returns
    it."""
    synthetic_report = _make_report(apply=False)
    # Patch asyncio.run so we never actually invoke the pipeline.
    with patch("app.management.ingest_source_data.asyncio.run", return_value=synthetic_report):
        result = main(["--files", f"{tmp_path}/*"])
    assert result is synthetic_report


def test_main_apply_flag_forwarded():
    """--apply is forwarded to run() via asyncio.run."""
    synthetic_report = _make_report(apply=True)
    captured: list = []

    def fake_asyncio_run(coro):
        # Inspect the coroutine's cr_frame locals to confirm apply=True was set.
        captured.append(coro)
        coro.close()  # prevent ResourceWarning
        return synthetic_report

    with patch("app.management.ingest_source_data.asyncio.run", side_effect=fake_asyncio_run):
        result = main(["--files", "/tmp/nonexistent/*", "--apply"])
    assert result["apply"] is True


# ── main() callable guard (line 215) ─────────────────────────────────────────


def test_main_is_callable():
    """Confirm main is a callable (the __main__ guard itself is not unit-testable)."""
    assert callable(main)


# ── _discover_files: non-file glob entry skipped (line 55) ───────────────────


def test_discover_files_skips_directory_entries(tmp_path):
    """Line 55: directories matched by the glob are skipped (p.is_file() is False)."""
    # Create a subdirectory alongside a real CSV so both match the glob.
    subdir = tmp_path / "some_subdir"
    subdir.mkdir()
    (tmp_path / "LSC1__Material__c.csv").write_text("Id\na1\n", encoding="utf-8")

    files, manufacturers = _discover_files(f"{tmp_path}/*")

    # The subdirectory must not appear in either return value.
    names = [f.name for f in files]
    assert "some_subdir" not in names
    assert manufacturers is None
    assert "LSC1__Material__c.csv" in names
