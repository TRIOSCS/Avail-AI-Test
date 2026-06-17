"""tests/test_categorize_from_desc_nightly.py — Covers exception handler, _print_report,
and main().

Targets lines 174-177, 199-211, 215-237, 241 in app/management/categorize_from_desc.py.

Called by: pytest test suite Depends on: conftest.db_session,
app.management.categorize_from_desc
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.management.categorize_from_desc import _print_report, run
from app.models import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas


def _card(db: Session, mpn: str, description: str | None, category=None) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=category, description=description)
    db.add(card)
    db.flush()
    return card


# ── Exception handler (lines 174-177) ───────────────────────────────────────


def test_exception_in_loop_increments_failed(db_session: Session):
    """Patch categorize_and_record to raise — verifies the except block increments
    failed."""
    seed_commodity_schemas(db_session)
    _card(db_session, "BOOM1", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    db_session.commit()

    with patch("app.management.categorize_from_desc.categorize_and_record", side_effect=RuntimeError("boom")):
        summary = run(db_session, apply=True)

    assert summary["failed"] == 1
    assert summary["categorized"] == 0


# ── _print_report (lines 199-211) ────────────────────────────────────────────


def test_print_report_runs_without_error():
    """Call _print_report with a complete summary dict — must not raise."""
    summary = {
        "mode": "dry-run",
        "cards_examined": 10,
        "categorized": 3,
        "specs_written": 5,
        "no_grammar_match": 2,
        "skipped_no_desc": 4,
        "failed": 1,
        "by_channel": {"own_desc": 2, "fru_desc": 1},
        "by_category": {"hdd": 2, "cables": 1},
    }
    # Should complete without raising; logger calls are side-effect only.
    _print_report(summary)


def test_print_report_empty_by_category():
    """_print_report with no categories — for-loop body (line 209-210) still runs
    cleanly."""
    summary = {
        "mode": "apply",
        "cards_examined": 0,
        "categorized": 0,
        "specs_written": 0,
        "no_grammar_match": 0,
        "skipped_no_desc": 0,
        "failed": 0,
        "by_channel": {},
        "by_category": {},
    }
    _print_report(summary)


# ── main() (lines 215-237, 241) ───────────────────────────────────────────────

_FAKE_SUMMARY = {
    "mode": "dry-run",
    "cards_examined": 0,
    "categorized": 0,
    "specs_written": 0,
    "no_grammar_match": 0,
    "skipped_no_desc": 0,
    "failed": 0,
    "by_channel": {},
    "by_category": {},
}


def test_main_dry_run_calls_rollback():
    """Main() with no --apply flag must call db.rollback() and db.close()."""
    fake_db = MagicMock()
    fake_session_cls = MagicMock(return_value=fake_db)

    fake_args = MagicMock()
    fake_args.apply = False
    fake_args.limit = 0

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=fake_args),
        patch("app.management.categorize_from_desc.run", return_value=_FAKE_SUMMARY) as mock_run,
        patch("app.database.SessionLocal", fake_session_cls),
    ):
        from app.management.categorize_from_desc import main

        main()

    mock_run.assert_called_once_with(fake_db, apply=False, limit=0)
    fake_db.rollback.assert_called_once()
    fake_db.close.assert_called_once()


def test_main_apply_skips_rollback():
    """Main() with --apply must NOT call db.rollback() and must call db.close()."""
    fake_db = MagicMock()
    fake_session_cls = MagicMock(return_value=fake_db)

    fake_args = MagicMock()
    fake_args.apply = True
    fake_args.limit = 5

    apply_summary = {**_FAKE_SUMMARY, "mode": "apply"}

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=fake_args),
        patch("app.management.categorize_from_desc.run", return_value=apply_summary) as mock_run,
        patch("app.database.SessionLocal", fake_session_cls),
    ):
        from app.management.categorize_from_desc import main

        main()

    mock_run.assert_called_once_with(fake_db, apply=True, limit=5)
    fake_db.rollback.assert_not_called()
    fake_db.close.assert_called_once()


def test_main_closes_db_on_exception():
    """Main() must call db.close() in finally even when run() raises."""
    fake_db = MagicMock()
    fake_session_cls = MagicMock(return_value=fake_db)

    fake_args = MagicMock()
    fake_args.apply = False
    fake_args.limit = 0

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=fake_args),
        patch("app.management.categorize_from_desc.run", side_effect=RuntimeError("db gone")),
        patch("app.database.SessionLocal", fake_session_cls),
        pytest.raises(RuntimeError),
    ):
        from app.management.categorize_from_desc import main

        main()

    fake_db.close.assert_called_once()
