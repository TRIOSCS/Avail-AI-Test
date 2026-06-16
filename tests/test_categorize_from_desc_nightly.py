"""tests/test_categorize_from_desc_nightly.py — Extra coverage for missing lines.

Covers: _alnum_norm(None/empty) guard, no-grammar-match counter, exception
handler, _print_report output, and main() CLI paths (dry-run + --apply).
"""

import os
import sys
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"

import pytest
from sqlalchemy.orm import Session

from app.management.categorize_from_desc import _alnum_norm, _print_report, main, run
from app.models import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas

# ── _alnum_norm edge cases (line 60) ────────────────────────────────────────


def test_alnum_norm_returns_empty_for_none():
    assert _alnum_norm(None) == ""


def test_alnum_norm_returns_empty_for_empty_string():
    assert _alnum_norm("") == ""


def test_alnum_norm_strips_non_alnum():
    assert _alnum_norm("AB-CD 12") == "abcd12"


# ── no_grammar_match counter (line 180) ─────────────────────────────────────


def _card(db: Session, mpn: str, description: str | None, category=None) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=category, description=description)
    db.add(card)
    db.flush()
    return card


def test_run_dry_run_counts_no_grammar_match(db_session: Session):
    """A real description that the grammar can't classify increments no_grammar_match."""
    seed_commodity_schemas(db_session)
    # This description is long enough to be "real" (>= 15 chars, differs from MPN)
    # but matches no commodity category grammar.
    _card(db_session, "WIDG001", "Generic widget adapter board rev2 x17 industrial")
    db_session.commit()

    summary = run(db_session, apply=False)
    db_session.rollback()

    assert summary["no_grammar_match"] >= 1
    assert summary["categorized"] == 0


# ── exception handler (lines 181-183) ────────────────────────────────────────


def test_run_counts_failed_on_exception(db_session: Session):
    """Exceptions per-card are caught; card is counted in failed, run continues."""
    seed_commodity_schemas(db_session)
    _card(db_session, "ERRCARD", 'HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    db_session.commit()

    with patch("app.management.categorize_from_desc._has_real_own_desc", side_effect=RuntimeError("boom")):
        summary = run(db_session, apply=False)
    db_session.rollback()

    assert summary["failed"] >= 1


# ── _print_report (lines 205-217) ────────────────────────────────────────────


def test_print_report_runs_without_error():
    """_print_report only calls logger.info — verify it completes with any summary."""
    summary = {
        "mode": "dry-run",
        "cards_examined": 10,
        "categorized": 5,
        "specs_written": 3,
        "no_grammar_match": 2,
        "skipped_no_desc": 1,
        "failed": 0,
        "by_channel": {"own_desc": 4, "fru_desc": 1},
        "by_category": {"hdd": 3, "ssd": 2},
    }
    _print_report(summary)  # must not raise


def test_print_report_handles_empty_by_category():
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
    _print_report(summary)  # must not raise


# ── main() CLI entry point (lines 221-243, 247) ───────────────────────────────


def _make_mock_db(summary: dict) -> MagicMock:
    mock_db = MagicMock()
    return mock_db


def test_main_dry_run_calls_run_and_print_report(monkeypatch):
    """main() without --apply calls run(apply=False) and _print_report, then rollback."""
    monkeypatch.setattr(sys, "argv", ["categorize_from_desc"])

    mock_db = MagicMock()
    fake_summary = {
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

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.management.categorize_from_desc.run", return_value=fake_summary) as mock_run,
        patch("app.management.categorize_from_desc._print_report") as mock_print,
    ):
        main()

    mock_run.assert_called_once_with(mock_db, apply=False, limit=0)
    mock_print.assert_called_once_with(fake_summary)
    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


def test_main_apply_calls_run_without_rollback(monkeypatch):
    """main() with --apply calls run(apply=True) and does NOT call rollback."""
    monkeypatch.setattr(sys, "argv", ["categorize_from_desc", "--apply"])

    mock_db = MagicMock()
    fake_summary = {
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

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.management.categorize_from_desc.run", return_value=fake_summary) as mock_run,
        patch("app.management.categorize_from_desc._print_report"),
    ):
        main()

    mock_run.assert_called_once_with(mock_db, apply=True, limit=0)
    mock_db.rollback.assert_not_called()
    mock_db.close.assert_called_once()


def test_main_with_limit_passes_limit_to_run(monkeypatch):
    """main() --limit N forwards the limit to run()."""
    monkeypatch.setattr(sys, "argv", ["categorize_from_desc", "--limit", "50"])

    mock_db = MagicMock()
    fake_summary = {
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

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.management.categorize_from_desc.run", return_value=fake_summary) as mock_run,
        patch("app.management.categorize_from_desc._print_report"),
    ):
        main()

    mock_run.assert_called_once_with(mock_db, apply=False, limit=50)


def test_main_closes_session_even_on_exception(monkeypatch):
    """main() calls db.close() in finally even if run() raises."""
    monkeypatch.setattr(sys, "argv", ["categorize_from_desc"])

    mock_db = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.management.categorize_from_desc.run", side_effect=RuntimeError("test error")),
        pytest.raises(RuntimeError),
    ):
        main()

    mock_db.close.assert_called_once()
