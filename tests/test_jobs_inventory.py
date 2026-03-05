"""test_jobs_inventory.py — Tests for inventory/stock background jobs

Covers: _job_po_verification, _job_stock_autocomplete, _parse_stock_file,
_scan_stock_list_attachments, _download_and_import_stock_list.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import BuyPlan
from app.scheduler import scheduler

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def scheduler_db(db_session: Session):
    """Patch SessionLocal so scheduler jobs use the test DB."""
    original_close = db_session.close
    db_session.close = lambda: None
    with patch("app.database.SessionLocal", return_value=db_session):
        yield db_session
    db_session.close = original_close


@pytest.fixture(autouse=True)
def _clear_scheduler_jobs():
    """Remove all jobs before/after each test to prevent leakage."""
    for job in scheduler.get_jobs():
        job.remove()
    yield
    for job in scheduler.get_jobs():
        job.remove()


# ── _job_po_verification() ────────────────────────────────────────────


def test_po_verification_verifies_po_entered_plans(
    scheduler_db, test_user, test_requisition, test_company, test_customer_site, test_quote
):
    """PO verification scans buy plans in po_entered status."""
    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="po_entered",
        line_items=[],
        submitted_by_id=test_user.id,
    )
    scheduler_db.add(plan)
    scheduler_db.commit()

    with patch(
        "app.services.buyplan_service.verify_po_sent",
        new_callable=AsyncMock,
    ) as mock_verify:
        from app.jobs.inventory_jobs import _job_po_verification

        asyncio.run(_job_po_verification())
        mock_verify.assert_called_once()
        call_args = mock_verify.call_args
        assert call_args[0][0].id == plan.id


def test_po_verification_skips_when_no_plans(scheduler_db):
    """No verification calls when there are no po_entered plans."""
    with patch(
        "app.services.buyplan_service.verify_po_sent",
        new_callable=AsyncMock,
    ) as mock_verify:
        from app.jobs.inventory_jobs import _job_po_verification

        asyncio.run(_job_po_verification())
        mock_verify.assert_not_called()


def test_po_verification_handles_per_plan_error(
    scheduler_db, test_user, test_requisition, test_company, test_customer_site, test_quote
):
    """Errors during per-plan verification do not crash the job."""
    plan = BuyPlan(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="po_entered",
        line_items=[],
        submitted_by_id=test_user.id,
    )
    scheduler_db.add(plan)
    scheduler_db.commit()

    with patch(
        "app.services.buyplan_service.verify_po_sent",
        new_callable=AsyncMock,
        side_effect=Exception("Verification failed"),
    ):
        from app.jobs.inventory_jobs import _job_po_verification

        asyncio.run(_job_po_verification())


def test_po_verification_outer_exception(scheduler_db):
    """Outer exception in PO verification is caught."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crash")):
        from app.jobs.inventory_jobs import _job_po_verification

        asyncio.run(_job_po_verification())


# ── _job_stock_autocomplete() ─────────────────────────────────────────


def test_stock_autocomplete_delegates(scheduler_db):
    """Stock auto-complete delegates to auto_complete_stock_sales."""
    with patch("app.services.buyplan_service.auto_complete_stock_sales") as mock_complete:
        mock_complete.return_value = 5
        from app.jobs.inventory_jobs import _job_stock_autocomplete

        asyncio.run(_job_stock_autocomplete())
        mock_complete.assert_called_once_with(scheduler_db)


def test_stock_autocomplete_handles_zero(scheduler_db):
    """Job runs cleanly when no plans to complete."""
    with patch("app.services.buyplan_service.auto_complete_stock_sales") as mock_complete:
        mock_complete.return_value = 0
        from app.jobs.inventory_jobs import _job_stock_autocomplete

        asyncio.run(_job_stock_autocomplete())
        mock_complete.assert_called_once()


def test_stock_autocomplete_error_handling(scheduler_db):
    """Stock auto-complete handles errors gracefully."""
    with patch(
        "app.services.buyplan_service.auto_complete_stock_sales",
        side_effect=Exception("DB error"),
    ):
        from app.jobs.inventory_jobs import _job_stock_autocomplete

        asyncio.run(_job_stock_autocomplete())


def test_stock_autocomplete_timeout(scheduler_db):
    """Stock auto-complete handles timeout gracefully."""

    async def _mock_wait_for(coro, timeout=None):
        try:
            coro.close()
        except Exception:
            pass
        raise asyncio.TimeoutError()

    with (
        patch("app.services.buyplan_service.auto_complete_stock_sales") as mock_complete,
        patch("asyncio.wait_for", side_effect=_mock_wait_for),
    ):
        from app.jobs.inventory_jobs import _job_stock_autocomplete

        asyncio.run(_job_stock_autocomplete())


# ── _parse_stock_file() ──────────────────────────────────────────────


def test_parse_stock_file_delegates_to_file_utils():
    """Stock file parser delegates to parse_tabular_file + normalize_stock_row."""
    with (
        patch("app.file_utils.parse_tabular_file") as mock_parse,
        patch("app.file_utils.normalize_stock_row") as mock_norm,
    ):
        mock_parse.return_value = [
            {"mpn": "LM317T", "qty": "100", "price": "0.50"},
            {"mpn": "NE555", "qty": "200", "price": "0.25"},
        ]
        mock_norm.side_effect = lambda r: r  # pass through
        from app.jobs.inventory_jobs import _parse_stock_file

        result = _parse_stock_file(b"csv data", "test.csv")
        assert len(result) == 2
        mock_parse.assert_called_once_with(b"csv data", "test.csv")


def test_parse_stock_file_caps_at_5000_rows():
    """Stock file parser caps output at 5000 rows."""
    with (
        patch("app.file_utils.parse_tabular_file") as mock_parse,
        patch("app.file_utils.normalize_stock_row") as mock_norm,
    ):
        mock_parse.return_value = [{"mpn": f"MPN{i}"} for i in range(6000)]
        mock_norm.side_effect = lambda r: r
        from app.jobs.inventory_jobs import _parse_stock_file

        result = _parse_stock_file(b"data", "big.csv")
        assert len(result) == 5000


def test_parse_stock_file_filters_invalid_rows():
    """Stock file parser filters out rows that normalize_stock_row returns None for."""
    with (
        patch("app.file_utils.parse_tabular_file") as mock_parse,
        patch("app.file_utils.normalize_stock_row") as mock_norm,
    ):
        mock_parse.return_value = [
            {"mpn": "VALID"},
            {"mpn": ""},  # invalid
            {"mpn": "ALSO_VALID"},
        ]
        mock_norm.side_effect = lambda r: r if r.get("mpn") else None
        from app.jobs.inventory_jobs import _parse_stock_file

        result = _parse_stock_file(b"data", "test.csv")
        assert len(result) == 2


# ── _scan_stock_list_attachments ──────────────────────────────────────


def test_scan_stock_list_attachments_no_emails(scheduler_db, test_user):
    """No stock emails found returns early."""
    test_user.access_token = "at_stock"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_for_stock_lists = AsyncMock(return_value=[])

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.inventory_jobs import _scan_stock_list_attachments

        asyncio.run(_scan_stock_list_attachments(test_user, scheduler_db, is_backfill=False))


def test_scan_stock_list_attachments_with_files(scheduler_db, test_user):
    """Stock emails with attachments trigger download and import."""
    test_user.access_token = "at_stock"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_for_stock_lists = AsyncMock(
        return_value=[
            {
                "vendor_name": "Arrow",
                "from_email": "sales@arrow.com",
                "stock_files": [
                    {
                        "message_id": "msg1",
                        "attachment_id": "att1",
                        "filename": "stock.csv",
                    }
                ],
            }
        ]
    )

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch("app.jobs.inventory_jobs._download_and_import_stock_list", new_callable=AsyncMock) as mock_dl,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.inventory_jobs import _scan_stock_list_attachments

        asyncio.run(_scan_stock_list_attachments(test_user, scheduler_db, is_backfill=True))

        mock_dl.assert_called_once()


def test_scan_stock_list_attachments_import_error(scheduler_db, test_user):
    """Exception during import is caught per-attachment."""
    test_user.access_token = "at_stock"
    scheduler_db.commit()

    mock_miner = MagicMock()
    mock_miner.scan_for_stock_lists = AsyncMock(
        return_value=[
            {
                "vendor_name": "Arrow",
                "from_email": "sales@arrow.com",
                "stock_files": [
                    {
                        "message_id": "msg1",
                        "attachment_id": "att1",
                        "filename": "stock.csv",
                    }
                ],
            }
        ]
    )

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        patch(
            "app.jobs.inventory_jobs._download_and_import_stock_list",
            new_callable=AsyncMock,
            side_effect=Exception("import failed"),
        ),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.inbox_backfill_days = 180

        from app.jobs.inventory_jobs import _scan_stock_list_attachments

        asyncio.run(_scan_stock_list_attachments(test_user, scheduler_db, is_backfill=False))


# ── _download_and_import_stock_list ───────────────────────────────────


def test_download_and_import_stock_list_attachment_download_fails(scheduler_db, test_user):
    """Attachment download failure returns early."""
    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(side_effect=Exception("download error"))

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_error_in_att_data(scheduler_db, test_user):
    """Attachment data with error key returns early."""
    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"error": {"code": "NotFound"}})

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_no_content_bytes(scheduler_db, test_user):
    """Attachment with no contentBytes returns early."""
    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"id": "att1"})

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_file_validation_fails(scheduler_db, test_user):
    """Invalid file type returns early."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"not a csv").decode()})

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(False, "application/octet-stream")),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.bin",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_no_rows(scheduler_db, test_user):
    """No valid rows in parsed file returns early."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"header\n").decode()})

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=[]),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_ai_parser_fallback(scheduler_db, test_user):
    """AI parser failure falls back to legacy _parse_stock_file."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"mpn,qty\nLM317T,100").decode()})

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch(
            "app.services.attachment_parser.parse_attachment",
            new_callable=AsyncMock,
            side_effect=Exception("AI parser down"),
        ),
        patch("app.jobs.inventory_jobs._parse_stock_file", return_value=[{"mpn": "LM317T", "qty": 100}]) as mock_legacy,
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )
        mock_legacy.assert_called_once()


def test_download_and_import_stock_list_creates_cards_and_mvh(scheduler_db, test_user):
    """Successful import creates MaterialCard and MaterialVendorHistory."""
    import base64

    from app.models import MaterialCard, MaterialVendorHistory

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [
        {"mpn": "LM317T", "qty": 100, "price": 0.50, "manufacturer": "TI", "description": "Reg"},
    ]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    card = scheduler_db.query(MaterialCard).filter_by(normalized_mpn="lm317t").first()
    assert card is not None
    assert card.display_mpn == "LM317T"
    assert card.manufacturer == "TI"

    mvh = scheduler_db.query(MaterialVendorHistory).filter_by(material_card_id=card.id, vendor_name="arrow").first()
    assert mvh is not None
    assert mvh.last_qty == 100


def test_download_and_import_stock_list_hyphenated_mpn_no_duplicate(scheduler_db, test_user):
    """MPN with hyphens should normalize to canonical key, not create duplicate cards."""
    import base64

    from app.models import MaterialCard

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    existing = MaterialCard(
        normalized_mpn="qatest001",
        display_mpn="QA-TEST-001",
        search_count=0,
    )
    scheduler_db.add(existing)
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "QA-TEST-001", "qty": 50, "manufacturer": "Test Corp"}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="testvendor"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="TestVendor",
                vendor_email="sales@test.com",
            )
        )

    all_cards = scheduler_db.query(MaterialCard).all()
    qa_cards = [c for c in all_cards if "qatest" in c.normalized_mpn]
    assert len(qa_cards) == 1
    assert qa_cards[0].normalized_mpn == "qatest001"


def test_download_and_import_stock_list_updates_existing_mvh(scheduler_db, test_user):
    """Importing into existing MaterialCard updates MVH."""
    import base64

    from app.models import MaterialCard, MaterialVendorHistory

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    card = MaterialCard(
        normalized_mpn="ne555",
        display_mpn="NE555",
        manufacturer="TI",
        description="Timer",
    )
    scheduler_db.add(card)
    scheduler_db.flush()
    mvh = MaterialVendorHistory(
        material_card_id=card.id,
        vendor_name="arrow",
        source_type="email_auto_import",
        last_qty=50,
        times_seen=1,
    )
    scheduler_db.add(mvh)
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "NE555", "qty": 200, "unit_price": 0.30, "manufacturer": "TI"}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    scheduler_db.refresh(mvh)
    assert mvh.times_seen == 2
    assert mvh.last_qty == 200
    assert mvh.last_price == 0.30


def test_download_and_import_stock_list_excess_list(scheduler_db, test_user, test_company):
    """Import from a company email is classified as excess_list."""
    import base64

    from app.models import MaterialVendorHistory

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "EXCESS1", "qty": 500, "manufacturer": "Murata"}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="acme"),
        patch(
            "app.services.activity_service.match_email_to_entity",
            return_value={"type": "company", "id": test_company.id, "name": "Acme Electronics"},
        ),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="excess.csv",
                vendor_name="Acme Electronics",
                vendor_email="purchasing@acme-electronics.com",
            )
        )

    mvh = scheduler_db.query(MaterialVendorHistory).filter_by(vendor_name="acme").first()
    assert mvh is not None
    assert mvh.source_type == "excess_list"


def test_download_and_import_stock_list_skips_short_mpn(scheduler_db, test_user):
    """MPNs shorter than 3 chars are skipped."""
    import base64

    from app.models import MaterialCard

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [
        {"mpn": "AB", "qty": 100},
        {"mpn": "", "qty": 200},
        {"mpn": "ABC", "qty": 300},
    ]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    abc_card = scheduler_db.query(MaterialCard).filter_by(normalized_mpn="abc").first()
    assert abc_card is not None


def test_download_and_import_stock_list_commit_fails(scheduler_db, test_user):
    """Commit failure during import is handled gracefully."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "FAIL1", "qty": 100}]

    original_commit = scheduler_db.commit
    call_count = [0]

    def _failing_commit():
        call_count[0] += 1
        if call_count[0] > 2:
            raise Exception("commit failed")
        return original_commit()

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
    ):
        scheduler_db.commit = _failing_commit
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )
        scheduler_db.commit = original_commit


def test_download_and_import_stock_list_no_vendor_email(scheduler_db, test_user):
    """Import works with empty vendor_email."""
    import base64

    from app.models import MaterialCard

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "NOEMAIL1", "qty": 100}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="unknown"),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Unknown",
                vendor_email="",
            )
        )

    card = scheduler_db.query(MaterialCard).filter_by(normalized_mpn="noemail1").first()
    assert card is not None


def test_download_and_import_stock_list_price_field_fallback(scheduler_db, test_user):
    """MVH uses price field when unit_price is absent."""
    import base64

    from app.models import MaterialCard, MaterialVendorHistory

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    card = MaterialCard(
        normalized_mpn="pricefb",
        display_mpn="PRICEFB",
        manufacturer="Test",
    )
    scheduler_db.add(card)
    scheduler_db.flush()
    mvh = MaterialVendorHistory(
        material_card_id=card.id,
        vendor_name="arrow",
        source_type="email_auto_import",
        times_seen=1,
    )
    scheduler_db.add(mvh)
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "PRICEFB", "qty": 100, "price": 1.25}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

    scheduler_db.refresh(mvh)
    assert mvh.last_price == 1.25


def test_download_and_import_stock_list_teams_alert(scheduler_db, test_user, test_requisition):
    """Teams alert is sent when imported MPNs match open requirements."""
    import base64

    test_user.access_token = "at_dl"
    test_requisition.status = "active"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "LM317T", "qty": 100}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock) as mock_alert,
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )

        mock_alert.assert_called_once()


def test_download_and_import_stock_list_teams_alert_exception(scheduler_db, test_user, test_requisition):
    """Teams alert exception is caught silently."""
    import base64

    test_user.access_token = "at_dl"
    test_requisition.status = "active"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "LM317T", "qty": 100}]

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch(
            "app.services.teams.send_stock_match_alert", new_callable=AsyncMock, side_effect=Exception("Teams error")
        ),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_null_att_data(scheduler_db, test_user):
    """None response from get_json returns early."""
    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value=None)

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )


def test_download_and_import_stock_list_final_commit_fails(scheduler_db, test_user):
    """Final db.commit() failure in _download_and_import is handled."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [{"mpn": "COMMITFAIL1", "qty": 100}]

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = []
    mock_db.flush = MagicMock()
    mock_db.add = MagicMock()
    mock_db.commit = MagicMock(side_effect=Exception("final commit failed"))
    mock_db.rollback = MagicMock()

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
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
                mock_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )
        mock_db.rollback.assert_called()


def test_download_and_import_stock_list_card_flush_conflict(scheduler_db, test_user):
    """MaterialCard flush conflict is handled (rollback + continue)."""
    import base64

    test_user.access_token = "at_dl"
    scheduler_db.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"contentBytes": base64.b64encode(b"data").decode()})

    rows = [
        {"mpn": "CONFLICT1", "qty": 100},
        {"mpn": "NOCONFLICT", "qty": 200},
    ]

    original_flush = scheduler_db.flush
    flush_count = [0]

    def _sometimes_failing_flush():
        flush_count[0] += 1
        if flush_count[0] == 1:
            raise Exception("unique constraint")
        return original_flush()

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(True, "text/csv")),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=rows),
        patch("app.vendor_utils.normalize_vendor_name", return_value="arrow"),
        patch("app.services.activity_service.match_email_to_entity", return_value=None),
        patch("app.services.teams.send_stock_match_alert", new_callable=AsyncMock),
    ):
        scheduler_db.flush = _sometimes_failing_flush
        from app.jobs.inventory_jobs import _download_and_import_stock_list

        asyncio.run(
            _download_and_import_stock_list(
                test_user,
                scheduler_db,
                message_id="msg1",
                attachment_id="att1",
                filename="stock.csv",
                vendor_name="Arrow",
                vendor_email="sales@arrow.com",
            )
        )
        scheduler_db.flush = original_flush
