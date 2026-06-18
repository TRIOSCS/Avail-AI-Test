"""tests/test_import_demand_telemetry.py — one-shot demand-telemetry backfill CLI.

Covers app/management/import_demand_telemetry.py (plan 1.4): streaming + per-MPN-key
aggregation of the SFDC export, dry-run vs apply, normalized-MPN matching against live
cards, column-wise MAX on duplicate keys, IsDeleted/soft-delete/unparseable handling.
Depends on: conftest.py (db_session), a tmp_path fixture CSV.
"""

from datetime import timezone

from app.constants import MaterialEnrichmentStatus
from app.management import import_demand_telemetry as cli
from app.models import MaterialCard

# Header carries the three columns the importer reads + IsDeleted; extra SFDC columns
# are ignored by csv.DictReader.
_HEADER = "LSC1__Material_Number__c,Sourced_Qty_Last_90_Days__c,Most_Recent_Source_TS__c,IsDeleted,Junk__c"


def _write_csv(tmp_path, rows: list[str]):
    path = tmp_path / "telemetry.csv"
    path.write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def _mk(db, mpn: str):
    card = MaterialCard(
        normalized_mpn=cli.normalize_mpn_key(mpn),
        display_mpn=mpn,
        enrichment_status=MaterialEnrichmentStatus.UNENRICHED,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def test_parse_helpers():
    assert cli._parse_qty("14") == 14
    assert cli._parse_qty("14.0") == 14
    assert cli._parse_qty("") is None
    assert cli._parse_qty(None) is None
    assert cli._parse_qty("junk") is None
    # In-range values pass through; out-of-range SFDC artifacts clamp to INT4 max so
    # they can't overflow the sourced_qty_90d column (it's a ranking signal).
    assert cli._parse_qty("2147483647") == cli._SOURCED_QTY_MAX
    assert cli._parse_qty("37216300001") == cli._SOURCED_QTY_MAX
    assert cli._parse_qty("8188453743.0") == cli._SOURCED_QTY_MAX

    ts = cli._parse_ts("2/5/2020 17:24")
    assert ts is not None and ts.tzinfo == timezone.utc and ts.year == 2020
    assert cli._parse_ts("") is None
    assert cli._parse_ts("not-a-date") is None


def test_read_telemetry_aggregates_max_and_skips(tmp_path):
    """Duplicate normalized keys collapse to the column-wise MAX; IsDeleted rows and
    signal-less rows are excluded from the map."""
    path = _write_csv(
        tmp_path,
        [
            "LM-2596S,3,1/1/2020 10:00,false,x",  # -> key lm2596s, qty 3
            "LM2596S,7,3/1/2020 10:00,false,x",  # same key -> MAX qty 7, later ts
            "DELETED-1,99,1/1/2020 10:00,true,x",  # IsDeleted -> skipped
            "NOSIGNAL,,,,x",  # no qty, no ts -> not in map
            "TS-ONLY,,5/5/2021 08:00,false,x",  # ts-only row still counts
        ],
    )
    telemetry, stats = cli.read_telemetry(path)

    assert stats["csv_rows"] == 5
    assert stats["skipped_deleted"] == 1
    # lm2596s (2 source rows -> 1 key), ts-only -> 2 keys with signal
    assert "lm2596s" in telemetry
    qty, ts = telemetry["lm2596s"]
    assert qty == 7  # column-wise MAX across the duplicate
    assert ts is not None and ts.month == 3  # later of the two timestamps
    assert telemetry["tsonly"] == (None, telemetry["tsonly"][1])
    assert telemetry["tsonly"][1] is not None
    assert "nosignal" not in telemetry  # signal-less row excluded


def test_dry_run_matches_without_writing(tmp_path, db_session):
    card = _mk(db_session, "LM2596S")
    path = _write_csv(tmp_path, ["LM2596S,42,2/2/2020 09:00,false,x"])

    stats = cli.run_import(db_session, csv_path=path, apply=False)

    assert stats["apply"] is False
    assert stats["matched_cards"] == 1
    assert stats["updated"] == 0  # dry-run wrote nothing
    db_session.refresh(card)
    assert card.sourced_qty_90d is None
    assert card.last_sourced_at is None


def test_apply_backfills_matched_cards(tmp_path, db_session):
    matched = _mk(db_session, "LM2596S")
    unmatched = _mk(db_session, "STM32F407")  # no telemetry row -> stays NULL
    path = _write_csv(
        tmp_path,
        [
            "LM2596S,42,2/2/2020 09:00,false,x",
            "NO-SUCH-CARD,99,2/2/2020 09:00,false,x",  # telemetry with no live card
        ],
    )

    stats = cli.run_import(db_session, csv_path=path, apply=True)

    assert stats["apply"] is True
    assert stats["matched_cards"] == 1  # only LM2596S has a live card
    assert stats["updated"] == 1
    db_session.refresh(matched)
    db_session.refresh(unmatched)
    assert matched.sourced_qty_90d == 42
    assert matched.last_sourced_at is not None and matched.last_sourced_at.year == 2020
    assert unmatched.sourced_qty_90d is None  # untouched


def test_apply_skips_soft_deleted_cards(tmp_path, db_session):
    """Soft-deleted cards (deleted_at set) are excluded from the match/update query."""
    from datetime import datetime

    card = _mk(db_session, "LM2596S")
    card.deleted_at = datetime.now(timezone.utc)
    db_session.commit()
    path = _write_csv(tmp_path, ["LM2596S,42,2/2/2020 09:00,false,x"])

    stats = cli.run_import(db_session, csv_path=path, apply=True)

    assert stats["matched_cards"] == 0  # soft-deleted card not matched
    db_session.refresh(card)
    assert card.sourced_qty_90d is None


def test_main_defaults_to_dry_run(tmp_path, db_session, monkeypatch):
    """The CLI entry point defaults to dry-run (no --apply) and exits 0."""
    _mk(db_session, "LM2596S")
    path = _write_csv(tmp_path, ["LM2596S,42,2/2/2020 09:00,false,x"])

    # main() opens its own session via app.database.SessionLocal then close()s it;
    # hand it a no-op-close shim over the test session so conftest cleanup is unaffected.
    class _Shim:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def close(self):  # don't close the shared test session
            pass

    import app.database

    monkeypatch.setattr(app.database, "SessionLocal", lambda: _Shim(db_session))

    rc = cli.main(["--csv", str(path)])
    assert rc == 0
    # dry-run: nothing written
    card = db_session.query(MaterialCard).filter_by(normalized_mpn="lm2596s").one()
    assert card.sourced_qty_90d is None
