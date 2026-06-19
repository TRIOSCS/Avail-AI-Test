"""test_inventory_jobs_nightly.py — Additional coverage for app/jobs/inventory_jobs.py.

Targets the gaps not covered by test_jobs_inventory.py:
  - register_inventory_jobs (lines 23-35)
  - _job_buyplan_nudge (lines 130-192): buyer/ops nudge paths, last_nudge_at stamp,
    per-line exception handling, outer exception rollback
  - _scan_stock_list_attachments OSError/ValueError/KeyError branch (line 228)
  - _download_and_import_stock_list: norm_key=None skip (line 356),
    no-reqs continue (line 495), zero/negative qty gate (line 500),
    dedup key already-seen continue (line 513),
    SQLAlchemy sightings-phase error (lines 565-567),
    generic Exception sightings-phase error (lines 568-570)

Called by: pytest autodiscovery
Depends on: app.jobs.inventory_jobs, app.database, app.services.buyplan_notifications
"""

import asyncio
import base64
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

# ── register_inventory_jobs ───────────────────────────────────────────


def test_register_inventory_jobs_adds_three_jobs():
    """register_inventory_jobs must register exactly 3 jobs."""
    from app.jobs.inventory_jobs import register_inventory_jobs

    mock_scheduler = MagicMock()
    mock_settings = MagicMock()
    mock_settings.po_verify_interval_min = 30
    mock_settings.buyplan_auto_complete_hour = 18
    mock_settings.buyplan_auto_complete_tz = "America/New_York"

    register_inventory_jobs(mock_scheduler, mock_settings)

    assert mock_scheduler.add_job.call_count == 3
    job_ids = [call[1]["id"] for call in mock_scheduler.add_job.call_args_list]
    assert "po_verification" in job_ids
    assert "stock_autocomplete" in job_ids
    assert "buyplan_nudge" in job_ids


# ── _job_buyplan_nudge ────────────────────────────────────────────────


@pytest.fixture()
def scheduler_db(db_session: Session):
    """Patch SessionLocal so scheduler jobs use the test DB session."""
    original_close = db_session.close
    db_session.close = lambda: None
    with patch("app.database.SessionLocal", return_value=db_session):
        yield db_session
    db_session.close = original_close


def _make_awaiting_po_line(db, test_user, test_requisition, test_quote):
    """Create an active buy plan with one awaiting_po line eligible for buyer nudge."""
    from app.models.buy_plan import BuyPlanLineStatus

    plan = MagicMock()
    plan.id = 999

    line = MagicMock()
    line.id = 1
    line.buy_plan = plan
    line.buy_plan_id = 999
    line.status = BuyPlanLineStatus.AWAITING_PO.value
    line.buyer_id = test_user.id
    line.last_nudge_at = None
    return plan, line


def test_job_buyplan_nudge_buyer_line_stamped(
    scheduler_db, test_user, test_requisition, test_company, test_customer_site, test_quote
):
    """Buyer line nudge: last_nudge_at is stamped when notify_nudge_buyer returns True."""
    from app.models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus

    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="active",
        submitted_by_id=test_user.id,
        approved_at=datetime.now(timezone.utc) - timedelta(hours=10),
    )
    scheduler_db.add(plan)
    scheduler_db.flush()

    line = BuyPlanLine(
        buy_plan_id=plan.id,
        quantity=10,
        status=BuyPlanLineStatus.AWAITING_PO.value,
        buyer_id=test_user.id,
    )
    scheduler_db.add(line)
    scheduler_db.commit()

    with patch(
        "app.services.buyplan_notifications.notify_nudge_buyer",
        new_callable=AsyncMock,
        return_value=True,
    ):
        from app.jobs.inventory_jobs import _job_buyplan_nudge

        asyncio.run(_job_buyplan_nudge())

    scheduler_db.refresh(line)
    assert line.last_nudge_at is not None


def test_job_buyplan_nudge_buyer_returns_false_no_stamp(
    scheduler_db, test_user, test_requisition, test_company, test_customer_site, test_quote
):
    """Buyer nudge: when notify_nudge_buyer returns False, last_nudge_at is NOT set."""
    from app.models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus

    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="active",
        submitted_by_id=test_user.id,
        approved_at=datetime.now(timezone.utc) - timedelta(hours=10),
    )
    scheduler_db.add(plan)
    scheduler_db.flush()

    line = BuyPlanLine(
        buy_plan_id=plan.id,
        quantity=10,
        status=BuyPlanLineStatus.AWAITING_PO.value,
        buyer_id=test_user.id,
    )
    scheduler_db.add(line)
    scheduler_db.commit()

    with patch(
        "app.services.buyplan_notifications.notify_nudge_buyer",
        new_callable=AsyncMock,
        return_value=False,
    ):
        from app.jobs.inventory_jobs import _job_buyplan_nudge

        asyncio.run(_job_buyplan_nudge())

    scheduler_db.refresh(line)
    assert line.last_nudge_at is None


def test_job_buyplan_nudge_ops_line_stamped(
    scheduler_db, test_user, test_requisition, test_company, test_customer_site, test_quote
):
    """Ops line nudge: last_nudge_at is stamped when notify_nudge_ops returns True."""
    from app.models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus

    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="active",
        submitted_by_id=test_user.id,
        approved_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    scheduler_db.add(plan)
    scheduler_db.flush()

    line = BuyPlanLine(
        buy_plan_id=plan.id,
        quantity=10,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_confirmed_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    scheduler_db.add(line)
    scheduler_db.commit()

    with patch(
        "app.services.buyplan_notifications.notify_nudge_ops",
        new_callable=AsyncMock,
        return_value=True,
    ):
        from app.jobs.inventory_jobs import _job_buyplan_nudge

        asyncio.run(_job_buyplan_nudge())

    scheduler_db.refresh(line)
    assert line.last_nudge_at is not None


def test_job_buyplan_nudge_per_line_exception_does_not_crash(
    scheduler_db, test_user, test_requisition, test_company, test_customer_site, test_quote
):
    """Per-line notify exception is swallowed; job continues for remaining lines."""
    from app.models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus

    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="active",
        submitted_by_id=test_user.id,
        approved_at=datetime.now(timezone.utc) - timedelta(hours=10),
    )
    scheduler_db.add(plan)
    scheduler_db.flush()

    line = BuyPlanLine(
        buy_plan_id=plan.id,
        quantity=10,
        status=BuyPlanLineStatus.AWAITING_PO.value,
        buyer_id=test_user.id,
    )
    scheduler_db.add(line)
    scheduler_db.commit()

    with patch(
        "app.services.buyplan_notifications.notify_nudge_buyer",
        new_callable=AsyncMock,
        side_effect=Exception("Teams API down"),
    ):
        from app.jobs.inventory_jobs import _job_buyplan_nudge

        # Should NOT raise — per-line exceptions are caught internally
        asyncio.run(_job_buyplan_nudge())


def test_job_buyplan_nudge_outer_exception_rollback(scheduler_db):
    """Outer exception in nudge job triggers rollback and re-raises."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crash")):
        from app.jobs.inventory_jobs import _job_buyplan_nudge

        with pytest.raises(Exception, match="DB crash"):
            asyncio.run(_job_buyplan_nudge())


def test_job_buyplan_nudge_no_lines_no_commit(scheduler_db):
    """With no eligible lines, db.commit is NOT called."""
    original_commit = scheduler_db.commit
    commit_called = [False]

    def _track_commit():
        commit_called[0] = True
        return original_commit()

    scheduler_db.commit = _track_commit

    from app.jobs.inventory_jobs import _job_buyplan_nudge

    asyncio.run(_job_buyplan_nudge())
    # No lines were nudged, so commit should not have been triggered
    assert not commit_called[0]
    scheduler_db.commit = original_commit


# ── _scan_stock_list_attachments OSError branch (line 228) ────────────


def test_scan_stock_list_attachments_oserror_caught(db_session, test_user):
    """OSError during per-attachment import is caught, not re-raised."""
    test_user.access_token = "at_oserr"
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_for_stock_lists = AsyncMock(
        return_value=[
            {
                "vendor_name": "Arrow",
                "from_email": "sales@arrow.com",
                "stock_files": [{"message_id": "m1", "attachment_id": "a1", "filename": "stock.csv"}],
            }
        ]
    )

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch(
            "app.jobs.inventory_jobs._download_and_import_stock_list",
            new_callable=AsyncMock,
            side_effect=OSError("disk full"),
        ),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 30

        from app.jobs.inventory_jobs import _scan_stock_list_attachments

        # Must not raise — OSError is caught per attachment
        asyncio.run(_scan_stock_list_attachments(test_user, db_session, is_backfill=False))


def test_scan_stock_list_attachments_valueerror_caught(db_session, test_user):
    """ValueError during per-attachment import is caught, not re-raised."""
    test_user.access_token = "at_valerr"
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_for_stock_lists = AsyncMock(
        return_value=[
            {
                "vendor_name": "Mouser",
                "from_email": "sales@mouser.com",
                "stock_files": [{"message_id": "m2", "attachment_id": "a2", "filename": "parts.xlsx"}],
            }
        ]
    )

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch(
            "app.jobs.inventory_jobs._download_and_import_stock_list",
            new_callable=AsyncMock,
            side_effect=ValueError("bad file"),
        ),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 30

        from app.jobs.inventory_jobs import _scan_stock_list_attachments

        asyncio.run(_scan_stock_list_attachments(test_user, db_session, is_backfill=False))


# ── _download_and_import_stock_list: norm_key=None skip (line 356) ────


def test_download_import_skips_row_when_norm_key_is_none(db_session, test_user):
    """Rows where normalize_mpn_key returns None are skipped (no MaterialCard created)."""
    from app.models import MaterialCard

    test_user.access_token = "at_normkey"
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    # Row has MPN >= 3 chars, but normalize_mpn_key will return None
    rows = [{"mpn": "???", "qty": 100}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="testvendor"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.utils.normalization.normalize_mpn_key", return_value=None),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                db_session,
                message_id="msg1",
                attachment_id="att1",
                filename="test.csv",
                vendor_name="TestVendor",
                vendor_email="sales@testvendor.com",
            )
        )

    # No MaterialCard should have been created since norm_key was None
    cards = db_session.query(MaterialCard).all()
    assert len(cards) == 0


# ── Sighting dedup paths (lines 495, 500, 513) ────────────────────────


def test_download_import_sighting_no_reqs_continue(db_session, test_user, test_requisition):
    """Items whose MPN has no open requirements hit the 'no reqs' continue (line 495)."""
    from app.models import Sighting

    test_user.access_token = "at_noreqs"
    test_requisition.status = "active"
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    # MPN that does NOT match the test_requisition requirement (LM317T)
    rows = [{"mpn": "ZZZNOMATCH999", "qty": 100, "unit_price": 1.0}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                db_session,
                message_id="msg1",
                attachment_id="att1",
                filename="test.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    # No sightings created since the MPN has no matching open requirements
    sightings = db_session.query(Sighting).all()
    assert len(sightings) == 0


def test_download_import_sighting_zero_qty_skipped(db_session, test_user, test_requisition):
    """Items with qty=0 are skipped by the quality gate (line 500)."""
    from app.models import Sighting

    test_user.access_token = "at_zeroqty"
    test_requisition.status = "active"
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "LM317T", "qty": 0, "unit_price": 0.50}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                db_session,
                message_id="msg1",
                attachment_id="att1",
                filename="test.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    sightings = db_session.query(Sighting).all()
    assert len(sightings) == 0


def test_download_import_sighting_dedup_key_skips_duplicate(db_session, test_user, test_requisition):
    """Second identical row is skipped by dedup key check (line 513)."""
    from app.models import MaterialCard, MaterialVendorHistory, Sighting

    test_user.access_token = "at_dedup"
    test_requisition.status = "active"

    # Pre-create card + MVH so both rows update (not insert) — avoids unique constraint
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI")
    db_session.add(card)
    db_session.flush()
    mvh = MaterialVendorHistory(
        material_card_id=card.id,
        vendor_name="arrow",
        vendor_name_normalized="arrow",
        source_type="email_auto_import",
        times_seen=1,
        last_qty=50,
    )
    db_session.add(mvh)
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    # Two identical rows — only one sighting should be created
    rows = [
        {"mpn": "LM317T", "qty": 100, "unit_price": 0.50},
        {"mpn": "LM317T", "qty": 100, "unit_price": 0.50},
    ]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                db_session,
                message_id="msg1",
                attachment_id="att1",
                filename="test.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    req_id = test_requisition.requirements[0].id
    sightings = db_session.query(Sighting).filter_by(requirement_id=req_id).all()
    # Dedup must collapse two identical rows to one sighting
    assert len(sightings) == 1


# ── Sighting-phase SQLAlchemy error handler (lines 565-567) ──────────


def test_download_import_sighting_sqlalchemy_error_rolled_back(db_session, test_user, test_requisition):
    """SQLAlchemyError during sighting creation phase rolls back without re-raising."""
    import sqlalchemy.exc

    test_user.access_token = "at_sqlaerr"
    test_requisition.status = "active"
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "LM317T", "qty": 50, "unit_price": 1.0}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch(
            "app.services.vendor_unavailability.apply_to_fresh_sightings",
            side_effect=sqlalchemy.exc.SQLAlchemyError("sighting insert failed"),
        ),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        # Must NOT raise — SQLAlchemyError in sighting phase is caught and rolled back
        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                db_session,
                message_id="msg1",
                attachment_id="att1",
                filename="test.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


# ── Sighting-phase generic Exception handler (lines 568-570) ─────────


def test_download_import_sighting_generic_exception_rolled_back(db_session, test_user, test_requisition):
    """Generic Exception during sighting creation phase rolls back without re-raising."""

    test_user.access_token = "at_generr"
    test_requisition.status = "active"
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "LM317T", "qty": 75, "unit_price": 2.0}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch(
            "app.services.vendor_unavailability.apply_to_fresh_sightings",
            side_effect=RuntimeError("unexpected error"),
        ),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        # Must NOT raise — generic Exception in sighting phase is caught and rolled back
        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                db_session,
                message_id="msg1",
                attachment_id="att1",
                filename="test.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


# ── Ops per-line exception handler (lines 181-182) ────────────────────


def test_job_buyplan_nudge_ops_per_line_exception_does_not_crash(
    scheduler_db, test_user, test_requisition, test_company, test_customer_site, test_quote
):
    """Ops per-line notify exception is caught; job does not re-raise."""
    from app.models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus

    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="active",
        submitted_by_id=test_user.id,
        approved_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    scheduler_db.add(plan)
    scheduler_db.flush()

    line = BuyPlanLine(
        buy_plan_id=plan.id,
        quantity=10,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_confirmed_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    scheduler_db.add(line)
    scheduler_db.commit()

    with patch(
        "app.services.buyplan_notifications.notify_nudge_ops",
        new_callable=AsyncMock,
        side_effect=Exception("Graph API down"),
    ):
        from app.jobs.inventory_jobs import _job_buyplan_nudge

        # Must not raise — per-line ops exceptions are caught internally
        asyncio.run(_job_buyplan_nudge())


# ── Sighting: item in imported_for_matching with no match in req_map (line 495) ──


def test_download_import_sighting_mixed_mpns_one_with_no_req(db_session, test_user, test_requisition):
    """When import has two MPNs and only one matches an open req, the other
    hits the 'no reqs' continue at line 495."""
    from app.models import MaterialCard, MaterialVendorHistory, Sighting

    test_user.access_token = "at_mixed"
    test_requisition.status = "active"

    # Pre-create cards + MVH for both MPNs to avoid unique constraint on MVH
    for mpn_norm, mpn_disp in [("lm317t", "LM317T"), ("zzznomatch", "ZZZNOMATCH")]:
        c = MaterialCard(normalized_mpn=mpn_norm, display_mpn=mpn_disp, manufacturer="TI")
        db_session.add(c)
        db_session.flush()
        db_session.add(
            MaterialVendorHistory(
                material_card_id=c.id,
                vendor_name="arrow",
                vendor_name_normalized="arrow",
                source_type="email_auto_import",
                times_seen=1,
                last_qty=50,
            )
        )
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [
        {"mpn": "LM317T", "qty": 100, "unit_price": 0.50},
        {"mpn": "ZZZNOMATCH", "qty": 200, "unit_price": 1.00},
    ]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                db_session,
                message_id="msg1",
                attachment_id="att1",
                filename="test.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    req_id = test_requisition.requirements[0].id
    sightings = db_session.query(Sighting).filter_by(requirement_id=req_id).all()
    # Only LM317T matched the open requirement; ZZZNOMATCH hit the 'no reqs' continue
    assert len(sightings) == 1
    assert sightings[0].mpn_matched == "LM317T"
