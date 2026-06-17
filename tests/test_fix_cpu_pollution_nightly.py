"""tests/test_fix_cpu_pollution_nightly.py — Tests for exception handler and main() in
app/management/fix_cpu_pollution.py.

Covers: exception handler inside the reclassify loop (apply=True), main() happy path,
main() with apply=True, and main() finally-block on exception.

Depends on: conftest.py (db_session), app.services.commodity_registry.seed_commodity_schemas.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas


def _cpu_card(db: Session, mpn: str) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category="cpu")
    card.category_source = "trio_source"
    card.category_tier = 95
    db.add(card)
    db.flush()
    return card


def test_exception_in_set_category_skips_card(db_session: Session):
    """When set_category raises, the card is skipped and reclassified stays 0."""
    from app.management.fix_cpu_pollution import reclassify_cpu_pollution

    seed_commodity_schemas(db_session)
    _cpu_card(db_session, "5-1437720-3")  # TE connector — classifiable
    db_session.commit()

    with patch("app.management.fix_cpu_pollution.set_category", side_effect=Exception("db error")):
        stats = reclassify_cpu_pollution(db_session, apply=True)

    assert stats["scanned"] == 1
    assert stats["reclassified"] == 0


def test_main_dry_run_calls_reclassify_and_closes_db():
    """Main() with --apply omitted calls reclassify with apply=False and closes db."""
    mock_args = MagicMock()
    mock_args.apply = False
    mock_args.limit = None

    mock_db = MagicMock()

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=mock_args),
        patch("app.management.fix_cpu_pollution.SessionLocal", return_value=mock_db),
        patch("app.management.fix_cpu_pollution.reclassify_cpu_pollution") as mock_reclassify,
    ):
        from app.management.fix_cpu_pollution import main

        main()

    mock_reclassify.assert_called_once_with(mock_db, apply=False, limit=None)
    mock_db.close.assert_called_once()


def test_main_apply_passes_apply_true():
    """Main() with --apply=True forwards apply=True to reclassify_cpu_pollution."""
    mock_args = MagicMock()
    mock_args.apply = True
    mock_args.limit = None

    mock_db = MagicMock()

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=mock_args),
        patch("app.management.fix_cpu_pollution.SessionLocal", return_value=mock_db),
        patch("app.management.fix_cpu_pollution.reclassify_cpu_pollution") as mock_reclassify,
    ):
        from app.management.fix_cpu_pollution import main

        main()

    mock_reclassify.assert_called_once_with(mock_db, apply=True, limit=None)
    mock_db.close.assert_called_once()


def test_main_closes_db_even_on_exception():
    """Main() finally block closes db even when reclassify_cpu_pollution raises."""
    mock_args = MagicMock()
    mock_args.apply = False
    mock_args.limit = None

    mock_db = MagicMock()

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=mock_args),
        patch("app.management.fix_cpu_pollution.SessionLocal", return_value=mock_db),
        patch(
            "app.management.fix_cpu_pollution.reclassify_cpu_pollution",
            side_effect=RuntimeError("boom"),
        ),
    ):
        from app.management.fix_cpu_pollution import main

        with pytest.raises(RuntimeError, match="boom"):
            main()

    mock_db.close.assert_called_once()
