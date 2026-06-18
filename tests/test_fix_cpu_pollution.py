"""test_fix_cpu_pollution.py — Tests for app/management/fix_cpu_pollution.py.

Covers: reclassify_cpu_pollution (dry-run, apply, limit, exception handling),
        main() (argparse integration).

Called by: pytest autodiscovery
Depends on: conftest.py db_session, MaterialCard model,
            mocked classify_polluted_mpn and set_category
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard


def _make_cpu_card(db: Session, mpn: str) -> MaterialCard:
    """Create a MaterialCard in the 'cpu' category via Core UPDATE to bypass the ORM
    guard."""
    from sqlalchemy import update as _sa_update

    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer="Generic",
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    # Bypass @validates("category") — write raw SQL like a legacy row
    db.execute(_sa_update(MaterialCard).where(MaterialCard.id == card.id).values(category="cpu"))
    db.commit()
    db.expire(card, ["category"])
    return card


class TestReclassifyCpuPollutionDryRun:
    def test_no_cards_returns_zero_tally(self, db_session: Session):
        from app.management.fix_cpu_pollution import reclassify_cpu_pollution

        result = reclassify_cpu_pollution(db_session, apply=False)

        assert result["scanned"] == 0
        assert result["reclassified"] == 0
        assert result["by_commodity"] == {}

    def test_dry_run_does_not_commit(self, db_session: Session):
        _make_cpu_card(db_session, "DDR4-1234")

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value="memory"):
            with patch("app.management.fix_cpu_pollution.set_category") as mock_set:
                from app.management.fix_cpu_pollution import reclassify_cpu_pollution

                result = reclassify_cpu_pollution(db_session, apply=False)

        # set_category should NOT be called in dry-run
        mock_set.assert_not_called()
        assert result["reclassified"] == 1
        assert result["by_commodity"]["memory"] == 1

    def test_dry_run_no_match_not_counted(self, db_session: Session):
        _make_cpu_card(db_session, "INTEL-CORE-9999")

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value=None):
            from app.management.fix_cpu_pollution import reclassify_cpu_pollution

            result = reclassify_cpu_pollution(db_session, apply=False)

        assert result["scanned"] == 1
        assert result["reclassified"] == 0

    def test_scans_only_cpu_category(self, db_session: Session):
        """Cards with other categories are ignored."""
        # Create a non-cpu card (no category set → NULL, won't be scanned)
        card = MaterialCard(
            normalized_mpn="cap1234",
            display_mpn="CAP1234",
            manufacturer="Generic",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn") as mock_cls:
            from app.management.fix_cpu_pollution import reclassify_cpu_pollution

            result = reclassify_cpu_pollution(db_session, apply=False)

        mock_cls.assert_not_called()
        assert result["scanned"] == 0

    def test_deleted_cards_skipped(self, db_session: Session):
        """Soft-deleted cards (deleted_at IS NOT NULL) must not be scanned."""
        card = _make_cpu_card(db_session, "DELETED-CPU")
        from sqlalchemy import update as _sa_update

        db_session.execute(
            _sa_update(MaterialCard).where(MaterialCard.id == card.id).values(deleted_at=datetime.now(timezone.utc))
        )
        db_session.commit()

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn") as mock_cls:
            from app.management.fix_cpu_pollution import reclassify_cpu_pollution

            result = reclassify_cpu_pollution(db_session, apply=False)

        mock_cls.assert_not_called()
        assert result["scanned"] == 0


class TestReclassifyCpuPollutionApply:
    def test_apply_true_calls_set_category(self, db_session: Session):
        _make_cpu_card(db_session, "DDR4-2666")

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value="memory"):
            with patch("app.management.fix_cpu_pollution.set_category") as mock_set:
                from app.management.fix_cpu_pollution import reclassify_cpu_pollution

                result = reclassify_cpu_pollution(db_session, apply=True)

        mock_set.assert_called_once()
        assert result["reclassified"] == 1

    def test_apply_multiple_cards(self, db_session: Session):
        _make_cpu_card(db_session, "DDR4-A")
        _make_cpu_card(db_session, "DDR4-B")

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value="memory"):
            with patch("app.management.fix_cpu_pollution.set_category"):
                from app.management.fix_cpu_pollution import reclassify_cpu_pollution

                result = reclassify_cpu_pollution(db_session, apply=True)

        assert result["scanned"] == 2
        assert result["reclassified"] == 2

    def test_apply_mixed_match_and_no_match(self, db_session: Session):
        _make_cpu_card(db_session, "DDR4-MATCH")
        _make_cpu_card(db_session, "INTEL-REAL-CPU")

        def side_effect(mpn):
            return "memory" if "DDR4" in mpn else None

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", side_effect=side_effect):
            with patch("app.management.fix_cpu_pollution.set_category"):
                from app.management.fix_cpu_pollution import reclassify_cpu_pollution

                result = reclassify_cpu_pollution(db_session, apply=True)

        assert result["scanned"] == 2
        assert result["reclassified"] == 1
        assert result["by_commodity"]["memory"] == 1

    def test_set_category_exception_continues(self, db_session: Session):
        """An exception during set_category is logged and the loop continues."""
        _make_cpu_card(db_session, "FAILS-SET")
        _make_cpu_card(db_session, "SUCCEEDS-SET")

        call_count = {"n": 0}

        def side_effect(card, commodity, source, confidence):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated db error")

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value="memory"):
            with patch("app.management.fix_cpu_pollution.set_category", side_effect=side_effect):
                from app.management.fix_cpu_pollution import reclassify_cpu_pollution

                result = reclassify_cpu_pollution(db_session, apply=True)

        # Second card should still be counted
        assert result["scanned"] == 2
        assert result["reclassified"] == 1  # Only the successful one


class TestReclassifyCpuPollutionLimit:
    def test_limit_caps_results(self, db_session: Session):
        for i in range(5):
            _make_cpu_card(db_session, f"DDR4-{i:03d}")

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value="memory"):
            with patch("app.management.fix_cpu_pollution.set_category"):
                from app.management.fix_cpu_pollution import reclassify_cpu_pollution

                result = reclassify_cpu_pollution(db_session, apply=False, limit=2)

        assert result["scanned"] == 2

    def test_limit_none_scans_all(self, db_session: Session):
        for i in range(4):
            _make_cpu_card(db_session, f"DDR4-ALL-{i:03d}")

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value=None):
            from app.management.fix_cpu_pollution import reclassify_cpu_pollution

            result = reclassify_cpu_pollution(db_session, apply=False, limit=None)

        assert result["scanned"] == 4


class TestFixCpuPollutionMain:
    def test_main_dry_run_default(self):
        import app.management.fix_cpu_pollution as mod

        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)
        mock_reclassify = MagicMock(return_value={"scanned": 0, "reclassified": 0, "by_commodity": {}})

        with patch.object(mod, "SessionLocal", mock_session_local):
            with patch.object(mod, "reclassify_cpu_pollution", mock_reclassify):
                with patch("sys.argv", ["fix_cpu_pollution"]):
                    mod.main()

        mock_reclassify.assert_called_once_with(mock_db, apply=False, limit=None)
        mock_db.close.assert_called_once()

    def test_main_apply_flag(self):
        import app.management.fix_cpu_pollution as mod

        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)
        mock_reclassify = MagicMock(return_value={"scanned": 0, "reclassified": 0, "by_commodity": {}})

        with patch.object(mod, "SessionLocal", mock_session_local):
            with patch.object(mod, "reclassify_cpu_pollution", mock_reclassify):
                with patch("sys.argv", ["fix_cpu_pollution", "--apply"]):
                    mod.main()

        mock_reclassify.assert_called_once_with(mock_db, apply=True, limit=None)

    def test_main_limit_flag(self):
        import app.management.fix_cpu_pollution as mod

        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)
        mock_reclassify = MagicMock(return_value={"scanned": 0, "reclassified": 0, "by_commodity": {}})

        with patch.object(mod, "SessionLocal", mock_session_local):
            with patch.object(mod, "reclassify_cpu_pollution", mock_reclassify):
                with patch("sys.argv", ["fix_cpu_pollution", "--limit", "50"]):
                    mod.main()

        mock_reclassify.assert_called_once_with(mock_db, apply=False, limit=50)

    def test_main_closes_session_on_exception(self):
        import app.management.fix_cpu_pollution as mod

        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)

        with patch.object(mod, "SessionLocal", mock_session_local):
            with patch.object(mod, "reclassify_cpu_pollution", side_effect=RuntimeError("boom")):
                with patch("sys.argv", ["fix_cpu_pollution"]):
                    with pytest.raises(RuntimeError):
                        mod.main()

        mock_db.close.assert_called_once()
