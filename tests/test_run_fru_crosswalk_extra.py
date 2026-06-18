"""test_run_fru_crosswalk_extra.py — Coverage for app/management/run_fru_crosswalk.py.

Covers missing lines:
- 213: fru_descs path (description on fru link)
- 227: skip non-enrichable fru
- 265: limit applied in run_create
- 286-290: IntegrityError path in run_create
- 334: break in decode sample loop
- 372-410: main() CLI function
- 414: __main__ guard

Called by: pytest
Depends on: conftest db_session, FruLink, MaterialCard models
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import FruLinkKind, MaterialEnrichmentStatus
from app.management.run_fru_crosswalk import (
    collect_creatable_cards,
    measure_drive_pn_misreads,
    run_create,
    run_drain,
)
from app.models import FruLink, MaterialCard


def _fru_link(
    db: Session,
    *,
    fru_raw: str,
    fru_norm: str,
    related_raw: str,
    related_norm: str,
    rel_kind: str = FruLinkKind.MFG_MODEL.value,
    manufacturer: str | None = None,
    description: str | None = None,
    source_sheet: str = "test",
) -> FruLink:
    link = FruLink(
        fru_raw=fru_raw,
        fru_norm=fru_norm,
        related_raw=related_raw,
        related_norm=related_norm,
        rel_kind=rel_kind,
        manufacturer=manufacturer,
        description=description,
        source_sheet=source_sheet,
    )
    db.add(link)
    db.flush()
    return link


def _card(db: Session, normalized_mpn: str) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=normalized_mpn,
        display_mpn=normalized_mpn.upper(),
        enrichment_status=MaterialEnrichmentStatus.UNENRICHED.value,
    )
    db.add(card)
    db.flush()
    return card


# ── line 213: fru_descs with description ─────────────────────────────────────


class TestCollectCreatableCards:
    def test_fru_desc_path_used_when_decode_fails(self, db_session: Session):
        """Line 213: description appended to fru_descs when description present."""
        _fru_link(
            db_session,
            fru_raw="FRUTEST01",
            fru_norm="frutest01",
            related_raw="RELATED01",
            related_norm="related01",
            rel_kind=FruLinkKind.MFG_MODEL.value,
            description='HD, 1TB, 3.5", SATA 6Gb/s, 7200RPM',
        )
        db_session.flush()
        # extract_desc returns something for this description → enrichable via desc path
        with patch("app.management.run_fru_crosswalk._decodes", return_value=False):
            with patch(
                "app.management.run_fru_crosswalk.extract_desc", return_value=MagicMock(specs={"capacity": "1TB"})
            ):
                plan = collect_creatable_cards(db_session)
        # frutest01 should be in plan as enrichable via desc path (line 213 + 224)
        assert "frutest01" in plan

    def test_non_enrichable_fru_skipped(self, db_session: Session):
        """Line 227: fru_norm is skipped when neither _decodes nor extract_desc succeeds."""
        _fru_link(
            db_session,
            fru_raw="NOTRICH01",
            fru_norm="notrich01",
            related_raw="RELATED02",
            related_norm="related02",
            rel_kind=FruLinkKind.MFG_MODEL.value,
            description="unrecognized widget blaarg zorp froop",
        )
        db_session.flush()
        with patch("app.management.run_fru_crosswalk._decodes", return_value=False):
            with patch("app.management.run_fru_crosswalk.extract_desc", return_value=None):
                plan = collect_creatable_cards(db_session)
        assert "notrich01" not in plan


# ── line 265: limit applied in run_create ────────────────────────────────────


class TestRunCreateLimit:
    def test_limit_caps_keys(self, db_session: Session):
        """Line 265: when limit > 0, only that many keys are processed."""
        for i in range(5):
            _fru_link(
                db_session,
                fru_raw=f"LIMFRU{i:02d}",
                fru_norm=f"limfru{i:02d}",
                related_raw=f"LIMREL{i:02d}",
                related_norm=f"limrel{i:02d}",
                rel_kind=FruLinkKind.MFG_MODEL.value,
            )
        db_session.flush()
        # Make all 5 appear enrichable
        with patch("app.management.run_fru_crosswalk._decodes", return_value=True):
            result = run_create(db_session, apply=False, limit=2)
        # Capped at 2 (dry-run, nothing written)
        assert result["creatable"] == 2

    def test_no_limit_uses_all(self, db_session: Session):
        """Without limit, all creatable keys are counted."""
        for i in range(3):
            _fru_link(
                db_session,
                fru_raw=f"NOLIM{i:02d}",
                fru_norm=f"nolim{i:02d}",
                related_raw=f"RELNO{i:02d}",
                related_norm=f"relno{i:02d}",
                rel_kind=FruLinkKind.MFG_MODEL.value,
            )
        db_session.flush()
        with patch("app.management.run_fru_crosswalk._decodes", return_value=True):
            result = run_create(db_session, apply=False, limit=0)
        assert result["creatable"] >= 3


# ── lines 286-290: IntegrityError path in run_create (apply=True) ─────────────


class TestRunCreateIntegrityError:
    def test_integrity_error_counted_as_skipped(self, db_session: Session):
        """Lines 286-290: a duplicate normalized_mpn causes IntegrityError → skipped_existing += 1."""
        _fru_link(
            db_session,
            fru_raw="DUPFRU01",
            fru_norm="dupfru01",
            related_raw="DUPREL01",
            related_norm="duprel01",
            rel_kind=FruLinkKind.MFG_MODEL.value,
        )
        db_session.flush()

        with patch("app.management.run_fru_crosswalk._decodes", return_value=True):
            with patch("app.management.run_fru_crosswalk.collect_creatable_cards") as mock_plan:
                mock_plan.return_value = {
                    "dupfru01": {
                        "display_mpn": "DUPFRU01",
                        "manufacturer": None,
                        "kinds": {"fru"},
                        "reason": "enrichable_fru",
                    }
                }
                # Make db.flush() raise IntegrityError
                original_flush = db_session.flush

                call_count = {"n": 0}

                def patched_flush(*args, **kwargs):
                    call_count["n"] += 1
                    if call_count["n"] == 1:
                        raise IntegrityError("UNIQUE", {}, None)
                    return original_flush(*args, **kwargs)

                with patch.object(db_session, "flush", side_effect=patched_flush):
                    result = run_create(db_session, apply=True)

        assert result["skipped_existing"] >= 1
        assert result["created"] == 0


# ── line 334: break in measure_drive_pn_misreads sample loop ─────────────────


class TestMeasureDrivePnBreak:
    def test_break_when_sample_reached(self, db_session: Session):
        """Line 334: loop breaks when decoded >= sample."""
        for i in range(5):
            link = FruLink(
                fru_raw=f"FRU{i:03d}",
                fru_norm=f"fru{i:03d}",
                related_raw=f"DRIVE{i:03d}",
                related_norm=f"drive{i:03d}",
                rel_kind=FruLinkKind.DRIVE_PN.value,
                manufacturer="Seagate",
                description="18TB 3.5 HDD 7.2K 12Gb/s SAS",
                source_sheet="test",
            )
            db_session.add(link)
        db_session.flush()

        mock_result = MagicMock()
        mock_result.specs = {"capacity": "18TB", "form_factor": "3.5in"}
        mock_result.commodity = "hdd"

        mock_truth = MagicMock()
        mock_truth.specs = {"capacity": "18TB", "form_factor": "3.5in"}
        mock_truth.commodity = "hdd"

        with patch("app.management.run_fru_crosswalk.decode_mpn", return_value=mock_result):
            with patch("app.management.run_fru_crosswalk.extract_desc", return_value=mock_truth):
                # sample=2: should stop after decoding 2 links (break at line 334)
                result = measure_drive_pn_misreads(db_session, sample=2)

        assert result["decoded"] == 2
        assert result["scanned"] == 2


# ── lines 372-410: main() CLI ─────────────────────────────────────────────────


class TestMainCli:
    def _mock_db(self):
        return MagicMock()

    def test_main_dry_run_both_phases(self):
        """Lines 372-410: main() dry-run (no --apply) calls run_drain + run_create."""
        mock_db = MagicMock()
        import app.management.run_fru_crosswalk as mod

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch.object(
                mod, "run_drain", return_value={"mode": "dry-run", "candidates": 0, "stats": {}}
            ) as mock_drain:
                with patch.object(
                    mod,
                    "run_create",
                    return_value={
                        "mode": "dry-run",
                        "creatable": 0,
                        "by_reason": {},
                        "created": 0,
                        "skipped_existing": 0,
                    },
                ) as mock_create:
                    with patch("sys.argv", ["run_fru_crosswalk"]):
                        mod.main()

        mock_drain.assert_called_once()
        mock_create.assert_called_once()
        mock_db.rollback.assert_called()
        mock_db.close.assert_called()

    def test_main_apply_both_phases(self):
        """--apply passes apply=True to both phases."""
        mock_db = MagicMock()
        import app.management.run_fru_crosswalk as mod

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch.object(
                mod, "run_drain", return_value={"mode": "apply", "candidates": 0, "stats": {}}
            ) as mock_drain:
                with patch.object(
                    mod,
                    "run_create",
                    return_value={
                        "mode": "apply",
                        "creatable": 0,
                        "by_reason": {},
                        "created": 0,
                        "skipped_existing": 0,
                    },
                ) as mock_create:
                    with patch("sys.argv", ["run_fru_crosswalk", "--apply"]):
                        mod.main()

        _, drain_kwargs = mock_drain.call_args
        assert drain_kwargs["apply"] is True
        _, create_kwargs = mock_create.call_args
        assert create_kwargs["apply"] is True
        mock_db.close.assert_called()

    def test_main_drain_only(self):
        """phase='drain' calls run_drain but not run_create."""
        mock_db = MagicMock()
        import app.management.run_fru_crosswalk as mod

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch.object(
                mod, "run_drain", return_value={"mode": "dry-run", "candidates": 0, "stats": {}}
            ) as mock_drain:
                with patch.object(mod, "run_create") as mock_create:
                    with patch("sys.argv", ["run_fru_crosswalk", "drain"]):
                        mod.main()

        mock_drain.assert_called_once()
        mock_create.assert_not_called()

    def test_main_create_only(self):
        """phase='create' calls run_create but not run_drain."""
        mock_db = MagicMock()
        import app.management.run_fru_crosswalk as mod

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch.object(mod, "run_drain") as mock_drain:
                with patch.object(
                    mod,
                    "run_create",
                    return_value={
                        "mode": "dry-run",
                        "creatable": 0,
                        "by_reason": {},
                        "created": 0,
                        "skipped_existing": 0,
                    },
                ) as mock_create:
                    with patch("sys.argv", ["run_fru_crosswalk", "create"]):
                        mod.main()

        mock_drain.assert_not_called()
        mock_create.assert_called_once()

    def test_main_measure_drive_pn(self):
        """--measure-drive-pn calls measure_drive_pn_misreads and returns early."""
        mock_db = MagicMock()
        import app.management.run_fru_crosswalk as mod

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch.object(mod, "measure_drive_pn_misreads", return_value={}) as mock_measure:
                with patch.object(mod, "run_drain") as mock_drain:
                    with patch("sys.argv", ["run_fru_crosswalk", "--measure-drive-pn"]):
                        mod.main()

        mock_measure.assert_called_once()
        mock_drain.assert_not_called()
        mock_db.rollback.assert_called()
        mock_db.close.assert_called()

    def test_main_with_limit(self):
        """--limit passes limit to both phases."""
        mock_db = MagicMock()
        import app.management.run_fru_crosswalk as mod

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch.object(
                mod, "run_drain", return_value={"mode": "dry-run", "candidates": 0, "stats": {}}
            ) as mock_drain:
                with patch.object(
                    mod,
                    "run_create",
                    return_value={
                        "mode": "dry-run",
                        "creatable": 0,
                        "by_reason": {},
                        "created": 0,
                        "skipped_existing": 0,
                    },
                ):
                    with patch("sys.argv", ["run_fru_crosswalk", "--limit", "50"]):
                        mod.main()

        _, drain_kwargs = mock_drain.call_args
        assert drain_kwargs["limit"] == 50


# ── run_drain helpers ─────────────────────────────────────────────────────────


class TestRunDrain:
    def test_no_candidates_returns_zero(self, db_session: Session):
        """run_drain returns candidates=0 when select_drain_card_ids returns empty."""
        with patch("app.management.run_fru_crosswalk.select_drain_card_ids", return_value=[]):
            result = run_drain(db_session, apply=False)
        assert result["candidates"] == 0

    def test_dryrun_rolls_back_savepoint(self, db_session: Session):
        """Dry-run wraps the crosswalk in a savepoint and rolls it back."""
        with patch("app.management.run_fru_crosswalk.select_drain_card_ids", return_value=[1, 2]):
            with patch(
                "app.management.run_fru_crosswalk.crosswalk_and_record_specs", return_value={"categorized": 2}
            ) as mock_crosswalk:
                result = run_drain(db_session, apply=False)
        mock_crosswalk.assert_called_once()
        assert result["candidates"] == 2
        assert result["mode"] == "dry-run"
