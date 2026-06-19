"""test_categorize_from_desc_extra.py — Additional coverage for categorize_from_desc.py.

Covers missing lines:
- 174: no_grammar_match path in dry-run
- 175-177: exception path in run()
- 199-211: _print_report() function
- 215-237: main() CLI function (dry-run + apply paths)
- 241: if __name__ == "__main__" guard

Called by: pytest
Depends on: conftest db_session, app/management/categorize_from_desc.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.management.categorize_from_desc import _print_report, run
from app.models import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas
from app.utils.normalization import normalize_mpn_key


def _card(db: Session, mpn: str, description: str | None, category=None) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=normalize_mpn_key(mpn) or mpn.lower(),
        display_mpn=mpn,
        category=category,
        description=description,
    )
    db.add(card)
    db.flush()
    return card


# ── no_grammar_match path (line 174) ──────────────────────────────────


class TestNoGrammarMatch:
    def test_no_grammar_match_counted_in_dryrun(self, db_session: Session):
        seed_commodity_schemas(db_session)
        # Description is real (not MPN, long enough) but not matched by grammar
        _card(db_session, "XYZ999", "Completely unrecognized widget froop zorp blaarg blip")
        db_session.commit()

        summary = run(db_session, apply=False)
        assert summary["no_grammar_match"] >= 1


# ── exception path (lines 175-177) ────────────────────────────────────


class TestExceptionPath:
    def test_exception_during_processing_counted_as_failed(self, db_session: Session):
        seed_commodity_schemas(db_session)
        _card(db_session, "CRASH01", 'HD, 1TB, 3.5", SATA 6Gb/s, 7200RPM')
        db_session.commit()

        with patch("app.management.categorize_from_desc._has_real_own_desc", side_effect=RuntimeError("boom")):
            summary = run(db_session, apply=False)

        assert summary["failed"] >= 1


# ── _print_report (lines 199-211) ─────────────────────────────────────


class TestPrintReport:
    def test_print_report_with_categories(self):
        summary = {
            "mode": "dry-run",
            "cards_examined": 10,
            "categorized": 5,
            "specs_written": 3,
            "no_grammar_match": 2,
            "skipped_no_desc": 1,
            "failed": 0,
            "by_channel": {"own_desc": 4, "fru_desc": 1},
            "by_category": {"hdd": 3, "memory": 2},
        }
        # Should not raise
        _print_report(summary)

    def test_print_report_empty_categories(self):
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


# ── main() CLI function (lines 215-237) ───────────────────────────────


class TestMainCliFunction:
    def test_main_dry_run_path(self):
        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)
        mock_run = MagicMock(
            return_value={
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
        )

        import app.management.categorize_from_desc as mod

        with patch.object(mod, "run", mock_run):
            with patch("app.database.SessionLocal", mock_session_local):
                with patch("sys.argv", ["categorize_from_desc"]):
                    mod.main()

        # Dry-run: rollback should be called
        mock_db.rollback.assert_called()
        mock_db.close.assert_called()

    def test_main_apply_path_with_warning(self):
        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)
        mock_run = MagicMock(
            return_value={
                "mode": "apply",
                "cards_examined": 1,
                "categorized": 1,
                "specs_written": 2,
                "no_grammar_match": 0,
                "skipped_no_desc": 0,
                "failed": 0,
                "by_channel": {"own_desc": 1},
                "by_category": {"hdd": 1},
            }
        )

        import app.management.categorize_from_desc as mod

        with patch.object(mod, "run", mock_run):
            with patch("app.database.SessionLocal", mock_session_local):
                with patch("sys.argv", ["categorize_from_desc", "--apply"]):
                    mod.main()

        # Apply: commit not rollback (run does the commit internally)
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs[1]["apply"] is True
        mock_db.close.assert_called()
