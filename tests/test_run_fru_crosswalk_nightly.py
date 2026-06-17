"""Edge-case tests for app/management/run_fru_crosswalk.py.

Covers the 30 missing lines (213, 227, 265, 286-290, 334, 372-414):
  - FRU with description-only enrichability (lines 213, 227)
  - run_create limit slicing (line 265)
  - IntegrityError skipped-existing path (lines 286-290)
  - measure_drive_pn early break on sample (line 334)
  - main() CLI paths (lines 372-414)

Called by: pytest test suite
Depends on: tests/conftest.py (db_session fixture), app.management.run_fru_crosswalk
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import FruLinkKind
from app.management.run_fru_crosswalk import (
    collect_creatable_cards,
    main,
    measure_drive_pn_misreads,
    run_create,
)
from app.models import FruLink
from app.services.commodity_registry import seed_commodity_schemas
from app.utils.normalization import normalize_mpn_key


def _card(db: Session, mpn: str, category=None, **kw):
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn=normalize_mpn_key(mpn), display_mpn=mpn, category=category, **kw)
    db.add(card)
    db.flush()
    return card


def _link(
    db: Session,
    fru: str,
    related: str,
    *,
    mfg: str | None = "Seagate",
    kind: str = FruLinkKind.MFG_MODEL.value,
    sheet: str = "Main",
    description: str | None = None,
) -> FruLink:
    link = FruLink(
        fru_raw=fru,
        fru_norm=normalize_mpn_key(fru),
        related_raw=related,
        related_norm=normalize_mpn_key(related),
        rel_kind=kind,
        manufacturer=mfg,
        description=description,
        source_sheet=sheet,
    )
    db.add(link)
    db.flush()
    return link


# ── Line 213 + 227: description-only enrichable FRU ─────────────────────────


def test_collect_creatable_desc_only_fru(db_session: Session):
    """A FRU link with an extractable description but no decodable mfg_model model
    triggers fru_descs append (line 213) and the any(extract_desc...) branch (line 227).
    """
    seed_commodity_schemas(db_session)
    # drive_pn link with a rich description but no mfg (so _decodes returns False) —
    # the description "HDD; 1200GB; 2.5; 10K; SAS" should pass extract_desc.
    link = FruLink(
        fru_raw="49Y7443",
        fru_norm=normalize_mpn_key("49Y7443"),
        related_raw="00VN000",
        related_norm=normalize_mpn_key("00VN000"),
        rel_kind=FruLinkKind.MFG_MODEL.value,
        manufacturer=None,
        description="HDD; 1200GB; 2.5; 10K; SAS",
        source_sheet="test",
    )
    db_session.add(link)
    db_session.flush()

    plan = collect_creatable_cards(db_session)
    key = normalize_mpn_key("49Y7443")
    assert key in plan
    assert plan[key]["reason"] == "enrichable_fru"


# ── Line 265: run_create with limit ─────────────────────────────────────────


def test_run_create_with_limit(db_session: Session):
    """limit=1 slices the plan keys, so only 1 card is counted as creatable."""
    seed_commodity_schemas(db_session)
    _link(db_session, "00AAA01", "ST4000NM0035")
    db_session.flush()

    summary = run_create(db_session, apply=False, limit=1)
    assert summary["creatable"] == 1


# ── Lines 286-290: IntegrityError on flush skips card ───────────────────────


def test_run_create_integrity_error_skips(db_session: Session, monkeypatch):
    """When db.flush() raises IntegrityError the card is counted as skipped, not created."""
    seed_commodity_schemas(db_session)
    _link(db_session, "00AAA01", "ST4000NM0035")
    db_session.flush()

    monkeypatch.setattr(db_session, "flush", MagicMock(side_effect=IntegrityError("unique violation", None, None)))

    summary = run_create(db_session, apply=True)
    assert summary["skipped_existing"] >= 1
    assert summary["created"] == 0


# ── Line 334: early break when decoded >= sample ────────────────────────────


def test_measure_drive_pn_break_on_sample_limit(db_session: Session):
    """Two decodable drive_pn links with sample=1 hits the break (line 334) on the 2nd."""
    seed_commodity_schemas(db_session)
    # Both ST4000NM0035 and ST8000NM0055 decode (Seagate HDDs).
    _link(db_session, "49Y7001", "ST4000NM0035", kind=FruLinkKind.DRIVE_PN.value, mfg="Seagate")
    _link(db_session, "49Y7002", "ST8000NM0055", kind=FruLinkKind.DRIVE_PN.value, mfg="Seagate")
    db_session.flush()

    result = measure_drive_pn_misreads(db_session, sample=1)
    assert result["decoded"] == 1
    assert result["scanned"] >= 1


# ── Lines 372-410: main() CLI paths ─────────────────────────────────────────


def _mock_args(**kwargs):
    defaults = {"measure_drive_pn": False, "phase": "all", "apply": False, "limit": 0}
    defaults.update(kwargs)
    args = MagicMock()
    for k, v in defaults.items():
        setattr(args, k, v)
    return args


def test_main_measure_drive_pn_flag():
    """--measure-drive-pn calls measure_drive_pn_misreads and returns without drain/create."""
    mock_db = MagicMock()
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_mock_args(measure_drive_pn=True)),
        patch("app.management.run_fru_crosswalk.measure_drive_pn_misreads") as mock_measure,
        patch("app.management.run_fru_crosswalk.run_drain") as mock_drain,
        patch("app.management.run_fru_crosswalk.run_create") as mock_create,
        patch("app.database.SessionLocal", return_value=mock_db),
    ):
        main()

    mock_measure.assert_called_once_with(mock_db)
    mock_drain.assert_not_called()
    mock_create.assert_not_called()
    mock_db.close.assert_called_once()


def test_main_dry_run_all_phases():
    """Default dry-run all-phases calls both run_drain and run_create, then db.rollback()."""
    mock_db = MagicMock()
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_mock_args()),
        patch("app.management.run_fru_crosswalk.run_drain") as mock_drain,
        patch("app.management.run_fru_crosswalk.run_create") as mock_create,
        patch("app.database.SessionLocal", return_value=mock_db),
    ):
        main()

    mock_drain.assert_called_once_with(mock_db, apply=False, limit=0)
    mock_create.assert_called_once_with(mock_db, apply=False, limit=0)
    mock_db.rollback.assert_called()
    mock_db.close.assert_called_once()


def test_main_drain_only():
    """phase='drain' calls run_drain but NOT run_create."""
    mock_db = MagicMock()
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_mock_args(phase="drain")),
        patch("app.management.run_fru_crosswalk.run_drain") as mock_drain,
        patch("app.management.run_fru_crosswalk.run_create") as mock_create,
        patch("app.database.SessionLocal", return_value=mock_db),
    ):
        main()

    mock_drain.assert_called_once()
    mock_create.assert_not_called()


def test_main_closes_db_on_exception():
    """db.close() is called even when run_drain raises RuntimeError."""
    mock_db = MagicMock()
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_mock_args()),
        patch("app.management.run_fru_crosswalk.run_drain", side_effect=RuntimeError("boom")),
        patch("app.database.SessionLocal", return_value=mock_db),
        pytest.raises(RuntimeError),
    ):
        main()

    mock_db.close.assert_called_once()
