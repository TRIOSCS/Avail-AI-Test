"""tests/test_management_small_nightly.py — Coverage tests for small management modules.

Targets:
- app/management/fix_cpu_pollution.py  (71%, 13 missing)
- app/management/backfill_cadence_clocks.py (42%, 11 missing)
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

# ── fix_cpu_pollution ─────────────────────────────────────────────────


class TestReclassifyCpuPollution:
    def test_dry_run_does_not_commit(self, db_session: Session):
        from app.management.fix_cpu_pollution import reclassify_cpu_pollution
        from app.models import MaterialCard

        card = MaterialCard(normalized_mpn="cpu123", display_mpn="CPU123", category="cpu")
        db_session.add(card)
        db_session.commit()

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value="memory") as mock_clf:
            result = reclassify_cpu_pollution(db_session, apply=False)

        assert result["scanned"] >= 1
        assert result["reclassified"] >= 1
        assert "memory" in result["by_commodity"]
        # card should NOT be reclassified in the DB (dry run)
        db_session.expire(card)
        fresh = db_session.get(MaterialCard, card.id)
        assert fresh.category == "cpu"

    def test_apply_commits_reclassification(self, db_session: Session):
        from app.management.fix_cpu_pollution import reclassify_cpu_pollution
        from app.models import MaterialCard

        card = MaterialCard(normalized_mpn="cpu456", display_mpn="CPU456", category="cpu")
        db_session.add(card)
        db_session.commit()

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value="memory"):
            result = reclassify_cpu_pollution(db_session, apply=True)

        assert result["scanned"] >= 1
        assert result["reclassified"] >= 1

    def test_no_match_skips(self, db_session: Session):
        from app.management.fix_cpu_pollution import reclassify_cpu_pollution
        from app.models import MaterialCard

        card = MaterialCard(normalized_mpn="cpu789", display_mpn="CPU789", category="cpu")
        db_session.add(card)
        db_session.commit()

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value=None):
            result = reclassify_cpu_pollution(db_session, apply=True)

        assert result["reclassified"] == 0

    def test_with_limit(self, db_session: Session):
        from app.management.fix_cpu_pollution import reclassify_cpu_pollution
        from app.models import MaterialCard

        for i in range(5):
            card = MaterialCard(normalized_mpn=f"cputest{i}", display_mpn=f"CPUTEST{i}", category="cpu")
            db_session.add(card)
        db_session.commit()

        with patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value="memory"):
            result = reclassify_cpu_pollution(db_session, apply=False, limit=2)

        assert result["scanned"] == 2

    def test_apply_exception_continues(self, db_session: Session):
        from app.management.fix_cpu_pollution import reclassify_cpu_pollution
        from app.models import MaterialCard

        card = MaterialCard(normalized_mpn="cpuerr1", display_mpn="CPUERR1", category="cpu")
        db_session.add(card)
        db_session.commit()

        with (
            patch("app.management.fix_cpu_pollution.classify_polluted_mpn", return_value="memory"),
            patch("app.management.fix_cpu_pollution.set_category", side_effect=Exception("set_category failed")),
        ):
            # Should not raise — exception is caught and continues
            result = reclassify_cpu_pollution(db_session, apply=True)

        # reclassified stays 0 because set_category raised
        assert result["reclassified"] == 0


class TestCpuPollutionMain:
    def test_main_dry_run(self):
        from app.management.fix_cpu_pollution import main

        mock_db = MagicMock()
        with (
            patch("app.management.fix_cpu_pollution.SessionLocal", return_value=mock_db),
            patch("app.management.fix_cpu_pollution.reclassify_cpu_pollution", return_value={}) as mock_fn,
            patch("sys.argv", ["fix_cpu_pollution"]),
        ):
            main()
        mock_fn.assert_called_once_with(mock_db, apply=False, limit=None)
        mock_db.close.assert_called_once()

    def test_main_apply_flag(self):
        from app.management.fix_cpu_pollution import main

        mock_db = MagicMock()
        with (
            patch("app.management.fix_cpu_pollution.SessionLocal", return_value=mock_db),
            patch("app.management.fix_cpu_pollution.reclassify_cpu_pollution", return_value={}) as mock_fn,
            patch("sys.argv", ["fix_cpu_pollution", "--apply", "--limit", "50"]),
        ):
            main()
        mock_fn.assert_called_once_with(mock_db, apply=True, limit=50)


# ── backfill_cadence_clocks ───────────────────────────────────────────


class TestBackfillForSession:
    def test_backfill_for_session_calls_materialize(self, db_session: Session):
        from app.management.backfill_cadence_clocks import backfill_for_session

        with patch("app.management.backfill_cadence_clocks.materialize_all_clocks", return_value=7) as mock_mat:
            result = backfill_for_session(db_session)

        mock_mat.assert_called_once_with(db_session)
        assert result == 7

    def test_backfill_for_session_sets_tier_for_strategic(self, db_session: Session):
        from app.management.backfill_cadence_clocks import backfill_for_session
        from app.models.crm import Company

        co = Company(name="Strategic Corp", is_active=True, is_strategic=True)
        db_session.add(co)
        db_session.flush()

        with patch("app.management.backfill_cadence_clocks.materialize_all_clocks", return_value=1):
            backfill_for_session(db_session)

        db_session.expire(co)
        fresh = db_session.get(Company, co.id)
        assert fresh.tier == "key"

    def test_backfill_for_session_skips_already_tiered(self, db_session: Session):
        from app.management.backfill_cadence_clocks import backfill_for_session
        from app.models.crm import Company

        co = Company(name="Already Tiered Corp", is_active=True, is_strategic=True, tier="premium")
        db_session.add(co)
        db_session.flush()

        with patch("app.management.backfill_cadence_clocks.materialize_all_clocks", return_value=1):
            backfill_for_session(db_session)

        db_session.expire(co)
        fresh = db_session.get(Company, co.id)
        # Should NOT be changed from "premium" to "key"
        assert fresh.tier == "premium"


class TestBackfillCadenceClocksFunction:
    def test_backfill_cadence_clocks_commits_on_success(self):
        from app.management.backfill_cadence_clocks import backfill_cadence_clocks

        mock_db = MagicMock()
        import app.management.backfill_cadence_clocks as mod

        with (
            patch.object(mod, "backfill_for_session", return_value=5),
            patch("app.database.SessionLocal", return_value=mock_db),
        ):
            result = backfill_cadence_clocks()

        assert result == 5
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    def test_backfill_cadence_clocks_rollback_on_error(self):
        from app.management.backfill_cadence_clocks import backfill_cadence_clocks

        mock_db = MagicMock()
        import app.management.backfill_cadence_clocks as mod

        with (
            patch.object(mod, "backfill_for_session", side_effect=RuntimeError("test error")),
            patch("app.database.SessionLocal", return_value=mock_db),
        ):
            with pytest.raises(RuntimeError, match="test error"):
                backfill_cadence_clocks()

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()
