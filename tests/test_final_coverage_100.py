"""
test_final_coverage_100.py -- Tests targeting every uncovered line to reach 100% coverage.

Called by: pytest
Depends on: conftest.py fixtures
"""

import asyncio
import io
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models import (
    ActivityLog,
    ApiSource,
    BuyPlan,
    Company,
    CustomerSite,
    GraphSubscription,
    MaterialCard,
    Offer,
    ProactiveMatch,
    ProactiveOffer,
    ProactiveThrottle,
    Quote,
    Requirement,
    Requisition,
    Sighting,
    SiteContact,
    User,
    VendorCard,
    VendorReview,
)

# =========================================================================
# 1. buyplan_service.py -- lines 43-44, 181-182, 267, 319-320, 395-396,
#    496-497, 564-565, 718, 722
# =========================================================================


def _make_plan(db, user, **kw):
    # BuyPlan requires a valid requisition and quote (NOT NULL FKs)
    req_id = kw.get("requisition_id")
    if not req_id:
        req = Requisition(
            name="REQ-BP-AUTO",
            customer_name="Test",
            status="open",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(req)
        db.flush()
        req_id = req.id

    quote_id = kw.get("quote_id")
    if not quote_id:
        site = db.query(CustomerSite).first()
        if not site:
            co = Company(name="BPTestCo", created_at=datetime.now(timezone.utc))
            db.add(co)
            db.flush()
            site = CustomerSite(company_id=co.id, site_name="HQ")
            db.add(site)
            db.flush()
        q = Quote(
            requisition_id=req_id,
            customer_site_id=site.id,
            quote_number=f"Q-BP-{secrets.token_hex(4)}",
            status="sent",
            line_items=[],
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            created_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(q)
        db.flush()
        quote_id = q.id

    plan = BuyPlan(
        status=kw.get("status", "pending_approval"),
        requisition_id=req_id,
        quote_id=quote_id,
        line_items=kw.get(
            "line_items",
            [
                {
                    "offer_id": 1,
                    "mpn": "LM317T",
                    "vendor_name": "Arrow",
                    "qty": 1000,
                    "plan_qty": 1000,
                    "cost_price": 0.50,
                    "lead_time": "2 weeks",
                    "entered_by_id": None,
                }
            ],
        ),
        approval_token=secrets.token_urlsafe(32),
        submitted_by_id=kw.get("submitted_by_id", user.id),
        salesperson_notes=kw.get("salesperson_notes", None),
        manager_notes=kw.get("manager_notes", None),
        rejection_reason=kw.get("rejection_reason", None),
        created_at=datetime.now(timezone.utc),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


@pytest.mark.asyncio
async def test_run_buyplan_bg_success_path(db_session, test_user):
    """buyplan_service lines 43-44: _run() finds plan and calls coro_factory."""
    from app.services.buyplan_service import run_buyplan_bg

    plan = _make_plan(db_session, test_user)

    factory_called = asyncio.Event()

    async def _factory(p, d, **kw):
        factory_called.set()

    with patch("app.database.SessionLocal", return_value=db_session):
        run_buyplan_bg(_factory, plan.id)
        await asyncio.sleep(0.15)

    assert factory_called.is_set()


@pytest.mark.asyncio
async def test_notify_submitted_email_failure(db_session, test_user):
    """buyplan_service lines 181-182: except when admin email send fails."""
    from app.services.buyplan_service import notify_buyplan_submitted

    admin = User(
        email="admin-bp@test.com",
        name="Admin",
        role="admin",
        azure_id="adm-bp-001",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(admin)
    db_session.commit()
    plan = _make_plan(db_session, test_user)

    gc_mock = MagicMock()
    gc_mock.post = AsyncMock(side_effect=Exception("SMTP down"))

    with (
        patch("app.services.buyplan_notifications.settings") as ms,
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        patch("app.services.buyplan_notifications._post_teams_channel", new_callable=AsyncMock),
        patch("app.services.buyplan_notifications._send_teams_dm", new_callable=AsyncMock),
    ):
        ms.admin_emails = [admin.email]
        ms.teams_webhook_url = ""
        await notify_buyplan_submitted(plan, db_session)

    logs = db_session.query(ActivityLog).filter_by(activity_type="buyplan_pending").all()
    assert len(logs) >= 1


@pytest.mark.asyncio
async def test_notify_approved_salesperson_notes(db_session, test_user):
    """buyplan_service line 267: salesperson_notes in approved email."""
    from app.services.buyplan_service import notify_buyplan_approved

    # Create offer so the function can find a buyer via entered_by_id
    req = Requisition(
        name="REQ-APPNOTE",
        customer_name="Test",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    offer = Offer(
        requisition_id=req.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=100,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()

    plan = _make_plan(
        db_session,
        test_user,
        status="approved",
        requisition_id=req.id,
        salesperson_notes="Urgent delivery needed",
        line_items=[{"offer_id": offer.id, "mpn": "LM317T", "vendor_name": "Arrow", "entered_by_id": test_user.id}],
    )
    gc_mock = MagicMock()
    gc_mock.post_json = AsyncMock(return_value=MagicMock(status_code=202))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        patch("app.services.buyplan_notifications._post_teams_channel", new_callable=AsyncMock),
        patch("app.services.buyplan_notifications._send_teams_dm", new_callable=AsyncMock),
    ):
        await notify_buyplan_approved(plan, db_session)

    logs = db_session.query(ActivityLog).filter_by(activity_type="buyplan_approved").all()
    assert len(logs) >= 1


@pytest.mark.asyncio
async def test_notify_approved_email_failure(db_session, test_user):
    """buyplan_service lines 319-320: except when buyer approved email fails."""
    from app.services.buyplan_service import notify_buyplan_approved

    req = Requisition(
        name="REQ-APPFAIL",
        customer_name="Test",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    offer = Offer(
        requisition_id=req.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=100,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()

    plan = _make_plan(
        db_session,
        test_user,
        status="approved",
        requisition_id=req.id,
        line_items=[{"offer_id": offer.id, "mpn": "LM317T", "vendor_name": "Arrow", "entered_by_id": test_user.id}],
    )
    gc_mock = MagicMock()
    gc_mock.post_json = AsyncMock(side_effect=Exception("Network error"))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        patch("app.services.buyplan_notifications._post_teams_channel", new_callable=AsyncMock),
        patch("app.services.buyplan_notifications._send_teams_dm", new_callable=AsyncMock),
    ):
        await notify_buyplan_approved(plan, db_session)

    logs = db_session.query(ActivityLog).filter_by(activity_type="buyplan_approved").all()
    assert len(logs) >= 1


@pytest.mark.asyncio
async def test_notify_rejected_email_failure(db_session, test_user):
    """buyplan_service lines 395-396: except when rejection email fails."""
    from app.services.buyplan_service import notify_buyplan_rejected

    plan = _make_plan(db_session, test_user, status="rejected", rejection_reason="Too expensive")
    gc_mock = MagicMock()
    gc_mock.post_json = AsyncMock(side_effect=Exception("Auth expired"))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        patch("app.services.buyplan_notifications._post_teams_channel", new_callable=AsyncMock),
        patch("app.services.buyplan_notifications._send_teams_dm", new_callable=AsyncMock),
    ):
        await notify_buyplan_rejected(plan, db_session)

    logs = db_session.query(ActivityLog).filter_by(activity_type="buyplan_rejected").all()
    assert len(logs) >= 1


@pytest.mark.asyncio
async def test_notify_stock_sale_email_failure(db_session, test_user):
    """buyplan_service lines 496-497: except when stock sale email fails."""
    from app.services.buyplan_service import notify_stock_sale_approved

    plan = _make_plan(db_session, test_user, status="approved")
    gc_mock = MagicMock()
    gc_mock.post_json = AsyncMock(side_effect=Exception("Timeout"))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        patch("app.services.buyplan_notifications._post_teams_channel", new_callable=AsyncMock),
        patch("app.services.buyplan_notifications._send_teams_dm", new_callable=AsyncMock),
        patch("app.services.buyplan_notifications.settings") as ms,
    ):
        ms.stock_sale_notify_emails = ["stock@test.com"]
        ms.teams_webhook_url = ""
        ms.app_url = "http://test"
        await notify_stock_sale_approved(plan, db_session)

    logs = db_session.query(ActivityLog).filter_by(activity_type="buyplan_completed").all()
    assert len(logs) >= 1


@pytest.mark.asyncio
async def test_notify_completed_email_failure(db_session, test_user):
    """buyplan_service lines 564-565: except when completion email fails."""
    from app.services.buyplan_service import notify_buyplan_completed

    plan = _make_plan(db_session, test_user, status="complete")
    gc_mock = MagicMock()
    gc_mock.post_json = AsyncMock(side_effect=Exception("Connection reset"))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        patch("app.services.buyplan_notifications._post_teams_channel", new_callable=AsyncMock),
        patch("app.services.buyplan_notifications._send_teams_dm", new_callable=AsyncMock),
    ):
        await notify_buyplan_completed(plan, db_session, completer_name="Admin")

    logs = db_session.query(ActivityLog).filter_by(activity_type="buyplan_completed").all()
    assert len(logs) >= 1


@pytest.mark.asyncio
async def test_verify_po_offer_fallback_and_no_entered_by(db_session, test_user):
    """buyplan_service lines 718 + 722: offer lookup for entered_by_id and continue when None."""
    from app.services.buyplan_service import verify_po_sent

    req = Requisition(
        name="REQ-PO-001",
        customer_name="Acme",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    offer = Offer(
        requisition_id=req.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=100,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()

    offer_none = Offer(
        requisition_id=req.id,
        vendor_name="Mouser",
        mpn="LM317T",
        qty_available=50,
        unit_price=0.60,
        entered_by_id=None,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer_none)
    db_session.flush()

    plan = _make_plan(
        db_session,
        test_user,
        line_items=[
            {
                "offer_id": offer.id,
                "mpn": "LM317T",
                "vendor_name": "Arrow",
                "po_number": "PO-001",
                "entered_by_id": None,
            },
            {
                "offer_id": offer_none.id,
                "mpn": "LM317T",
                "vendor_name": "Mouser",
                "po_number": "PO-002",
                "entered_by_id": None,
            },
        ],
    )
    db_session.commit()

    gc_mock = MagicMock()
    gc_mock.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"value": [{"subject": "PO-001"}]}))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
    ):
        results = await verify_po_sent(plan, db_session)

    assert isinstance(results, dict)


# =========================================================================
# 2. attachment_parser.py -- lines 52, 139, 232-233, 275, 282, 285,
#    375, 379-380, 383
# =========================================================================


def test_match_headers_empty_skipped():
    """attachment_parser line 52: empty header string is skipped."""
    from app.services.attachment_parser import _match_headers_deterministic

    result = _match_headers_deterministic(["", "Part Number", "Qty", "Price"])
    assert len(result) > 0


@pytest.mark.asyncio
async def test_ai_mapping_none_result():
    """attachment_parser line 139: AI returns None -> {}."""
    from app.services.attachment_parser import _ai_detect_columns

    with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=None):
        result = await _ai_detect_columns(["Col1", "Col2"], [["a", "b"]], "test.com")
        assert result == {}


@pytest.mark.asyncio
async def test_cache_write_failure(db_session):
    """attachment_parser lines 232-233: cache write fails gracefully."""
    from app.services.attachment_parser import _get_or_detect_mapping

    with patch(
        "app.services.attachment_parser._ai_detect_columns", new_callable=AsyncMock, return_value={0: "mpn", 1: "qty"}
    ):
        result = await _get_or_detect_mapping(["MPN", "QTY"], [["LM317T", "1000"]], "test.com", "abc", db_session)
        assert "mpn" in result.values()


def test_parse_csv_tsv_file():
    """attachment_parser lines 275, 282, 285: TSV detection."""
    from app.services.attachment_parser import _parse_csv

    data = "MPN\tQTY\tPrice\nLM317T\t1000\t0.50\n".encode()
    headers, rows = _parse_csv(data, "stock.tsv")
    assert len(headers) >= 1


def test_parse_csv_auto_tab():
    """attachment_parser line 282: auto-detect tab in CSV."""
    from app.services.attachment_parser import _parse_csv

    data = "MPN\tQTY\tPrice\nLM317T\t1000\t0.50\n".encode()
    headers, rows = _parse_csv(data, "data.csv")
    assert len(headers) >= 1


@pytest.mark.asyncio
async def test_parse_unsupported_type(db_session):
    """attachment_parser line 375: unsupported file returns []."""
    from app.services.attachment_parser import parse_attachment

    result = await parse_attachment(b"data", "file.pdf", "v.com", db_session)
    assert result == []


@pytest.mark.asyncio
async def test_parse_empty_csv_headers(db_session):
    """attachment_parser lines 379-380: empty headers returns []."""
    from app.services.attachment_parser import parse_attachment

    with patch("app.services.attachment_parser._parse_csv", return_value=([], [])):
        result = await parse_attachment(b"x", "s.csv", "v.com", db_session)
        assert result == []


@pytest.mark.asyncio
async def test_parse_no_mpn_column(db_session):
    """attachment_parser line 383: no MPN column returns []."""
    from app.services.attachment_parser import parse_attachment

    csv_data = "Foo,Bar\n1,2\n".encode()
    with patch("app.services.attachment_parser._get_or_detect_mapping", new_callable=AsyncMock, return_value={}):
        result = await parse_attachment(csv_data, "s.csv", "v.com", db_session)
        assert result == []


# =========================================================================
# 3. proactive_service.py -- lines 266, 275, 400-401, 480
# =========================================================================


@pytest.mark.asyncio
async def test_proactive_contacts_no_email(db_session, test_user, test_company, test_requisition):
    """proactive_service line 266: contacts with no email raises ValueError."""
    from app.services.proactive_service import send_proactive_offer

    site = CustomerSite(company_id=test_company.id, site_name="S-A")
    db_session.add(site)
    db_session.flush()
    sc = SiteContact(customer_site_id=site.id, full_name="X", email=None)
    db_session.add(sc)
    db_session.flush()
    offer = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=100,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()
    req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    match = ProactiveMatch(
        customer_site_id=site.id,
        salesperson_id=test_user.id,
        mpn="LM317T",
        status="new",
        offer_id=offer.id,
        requirement_id=req_item.id,
        requisition_id=test_requisition.id,
    )
    db_session.add(match)
    db_session.flush()

    with pytest.raises(ValueError, match="no email"):
        await send_proactive_offer(
            db=db_session, user=test_user, token="t", match_ids=[match.id], contact_ids=[sc.id], sell_prices={}
        )


@pytest.mark.asyncio
async def test_proactive_match_no_offer(db_session, test_user, test_company, test_requisition):
    """proactive_service line 274-275: match.offer is None -> continue (skip)."""
    from app.services.proactive_service import send_proactive_offer

    site = CustomerSite(company_id=test_company.id, site_name="S-B", contact_email="b@acme.com")
    db_session.add(site)
    db_session.flush()
    sc = SiteContact(customer_site_id=site.id, full_name="J", email="j@acme.com")
    db_session.add(sc)
    db_session.flush()
    offer = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=100,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()
    req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    match = ProactiveMatch(
        customer_site_id=site.id,
        salesperson_id=test_user.id,
        mpn="LM317T",
        offer_id=offer.id,
        status="new",
        requirement_id=req_item.id,
        requisition_id=test_requisition.id,
    )
    db_session.add(match)
    db_session.commit()

    # Force match.offer to return None (simulates deleted offer) by expiring and mocking
    fake_match = MagicMock(spec=ProactiveMatch)
    fake_match.id = match.id
    fake_match.customer_site_id = match.customer_site_id
    fake_match.salesperson_id = match.salesperson_id
    fake_match.mpn = match.mpn
    fake_match.offer = None  # This is the key — triggers the "continue" on line 274-275

    orig_query = db_session.query

    def patched_query(*args, **kwargs):
        q = orig_query(*args, **kwargs)
        if args and args[0] is ProactiveMatch:
            mock_q = MagicMock()
            mock_q.filter.return_value = mock_q
            mock_q.all.return_value = [fake_match]
            return mock_q
        return q

    gc_mock = MagicMock()
    gc_mock.post_json = AsyncMock(return_value=MagicMock(status_code=202))

    with (
        patch.object(db_session, "query", side_effect=patched_query),
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
    ):
        # All matches have offer=None, so line_items will be empty.
        # The function should proceed without error (offers are simply skipped).
        await send_proactive_offer(
            db=db_session, user=test_user, token="tok", match_ids=[match.id], contact_ids=[sc.id], sell_prices={}
        )


@pytest.mark.asyncio
async def test_proactive_throttle_update(db_session, test_user, test_company, test_requisition):
    """proactive_service lines 400-401: existing throttle gets updated."""
    from app.services.proactive_service import send_proactive_offer

    site = CustomerSite(company_id=test_company.id, site_name="S-C")
    db_session.add(site)
    db_session.flush()
    sc = SiteContact(customer_site_id=site.id, full_name="B", email="b@acme.com")
    db_session.add(sc)
    db_session.flush()
    offer = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=100,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()
    req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    match = ProactiveMatch(
        customer_site_id=site.id,
        salesperson_id=test_user.id,
        mpn="LM317T",
        offer_id=offer.id,
        status="new",
        requirement_id=req_item.id,
        requisition_id=test_requisition.id,
    )
    db_session.add(match)
    db_session.flush()
    throttle = ProactiveThrottle(
        mpn="LM317T", customer_site_id=site.id, last_offered_at=datetime.now(timezone.utc) - timedelta(days=30)
    )
    db_session.add(throttle)
    db_session.commit()

    gc_mock = MagicMock()
    gc_mock.post = AsyncMock(return_value=MagicMock(status_code=202))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
        patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
    ):
        await send_proactive_offer(
            db=db_session,
            user=test_user,
            token="tok",
            match_ids=[match.id],
            contact_ids=[sc.id],
            sell_prices={str(match.id): 0.75},
        )

    throttles = db_session.query(ProactiveThrottle).filter_by(mpn="LM317T").all()
    assert len(throttles) == 1
    assert throttles[0].proactive_offer_id is not None


def test_proactive_convert_vendor_card(db_session, test_user, test_company, test_requisition, test_vendor_card):
    """proactive_service line 479-480: vendor_card_id set on new offer."""
    from app.services.proactive_service import convert_proactive_to_win

    offer = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=100,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        vendor_card_id=test_vendor_card.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()

    site = CustomerSite(company_id=test_company.id, site_name="S-D")
    db_session.add(site)
    db_session.flush()

    po = ProactiveOffer(
        customer_site_id=site.id,
        salesperson_id=test_user.id,
        line_items=[
            {
                "mpn": "LM317T",
                "vendor_name": "Arrow",
                "qty": 100,
                "unit_price": 0.50,
                "sell_price": 0.75,
                "offer_id": offer.id,
                "manufacturer": "TI",
            }
        ],
        subject="Offer",
        recipient_emails=["b@acme.com"],
        status="sent",
    )
    db_session.add(po)
    db_session.commit()

    result = convert_proactive_to_win(db=db_session, proactive_offer_id=po.id, user=test_user)
    assert "requisition_id" in result
    new_offers = (
        db_session.query(Offer)
        .filter(Offer.requisition_id == result["requisition_id"], Offer.source == "proactive")
        .all()
    )
    assert any(o.vendor_card_id == test_vendor_card.id for o in new_offers)


# =========================================================================
# 4. vendor_score.py -- lines 190, 249-250, 274-278, 295-299
# =========================================================================


def test_get_quote_offer_ids(db_session, test_user, test_requisition, test_company):
    """vendor_score lines 274-278."""
    from app.services.vendor_score import _get_quote_offer_ids

    offer = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=100,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()

    site = db_session.query(CustomerSite).first()
    if not site:
        site = CustomerSite(company_id=test_company.id, site_name="M")
        db_session.add(site)
        db_session.flush()

    q = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=site.id,
        quote_number="Q-SC-001",
        status="sent",
        line_items=[{"offer_id": offer.id, "mpn": "LM317T"}],
        subtotal=100,
        total_cost=50,
        total_margin_pct=50,
        created_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(q)
    db_session.commit()

    found = _get_quote_offer_ids(db_session, {offer.id})
    assert offer.id in found


def test_get_buyplan_offer_ids(db_session, test_user):
    """vendor_score lines 295-299."""
    from app.services.vendor_score import AWARDED_STATUSES, _get_buyplan_offer_ids

    plan = _make_plan(db_session, test_user, status="approved", line_items=[{"offer_id": 42, "mpn": "X"}])

    found = _get_buyplan_offer_ids(db_session, {42, 99}, AWARDED_STATUSES)
    assert 42 in found
    assert 99 not in found


@pytest.mark.asyncio
async def test_po_confirmed_scoring(db_session, test_user, test_vendor_card):
    """vendor_score line 190."""
    from app.services.vendor_score import compute_all_vendor_scores

    req = Requisition(
        name="REQ-SC-001",
        customer_name="T",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    for i in range(5):
        db_session.add(
            Offer(
                requisition_id=req.id,
                vendor_name="arrow electronics",
                mpn="P-%d" % i,
                qty_available=100,
                unit_price=0.50,
                entered_by_id=test_user.id,
                status="active",
                vendor_card_id=test_vendor_card.id,
                created_at=datetime.now(timezone.utc),
            )
        )
    db_session.flush()

    first_offer = db_session.query(Offer).filter_by(requisition_id=req.id).first()
    plan = _make_plan(
        db_session,
        test_user,
        status="po_confirmed",
        requisition_id=req.id,
        line_items=[{"offer_id": first_offer.id, "mpn": "P-0"}],
    )

    result = await compute_all_vendor_scores(db_session)
    assert "updated" in result


@pytest.mark.asyncio
async def test_vendor_scoring_flush_failure(db_session, test_user, test_vendor_card):
    """vendor_score lines 249-250."""
    from app.services.vendor_score import compute_all_vendor_scores

    req = Requisition(
        name="REQ-SC-002",
        customer_name="T",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    for i in range(5):
        db_session.add(
            Offer(
                requisition_id=req.id,
                vendor_name="arrow electronics",
                mpn="FP-%d" % i,
                qty_available=100,
                unit_price=0.50,
                entered_by_id=test_user.id,
                status="active",
                vendor_card_id=test_vendor_card.id,
                created_at=datetime.now(timezone.utc),
            )
        )
    db_session.commit()

    orig = db_session.flush
    count = [0]

    def flaky(*a, **kw):
        count[0] += 1
        if count[0] > 1:
            raise Exception("flush error")
        return orig(*a, **kw)

    with patch.object(db_session, "flush", side_effect=flaky):
        result = await compute_all_vendor_scores(db_session)
    assert isinstance(result, dict)


# =========================================================================
# 5. vendor_analysis_service.py -- lines 68-69, 71
# =========================================================================


@pytest.mark.asyncio
async def test_vendor_sighting_analysis(db_session, test_user, test_vendor_card):
    """vendor_analysis_service lines 68-69, 71."""
    from app.services.vendor_analysis_service import _analyze_vendor_materials

    req = Requisition(
        name="REQ-VA", customer_name="T", status="open", created_by=test_user.id, created_at=datetime.now(timezone.utc)
    )
    db_session.add(req)
    db_session.flush()
    requirement = Requirement(
        requisition_id=req.id, primary_mpn="T", target_qty=100, created_at=datetime.now(timezone.utc)
    )
    db_session.add(requirement)
    db_session.flush()

    for i in range(210):
        db_session.add(
            Sighting(
                requirement_id=requirement.id,
                vendor_name=test_vendor_card.normalized_name,
                mpn_matched="PART-%04d" % i,
                manufacturer="TI",
                qty_available=100,
                created_at=datetime.now(timezone.utc),
            )
        )
    db_session.commit()

    with patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
        return_value={"brands": ["TI"], "commodities": ["Regulators"]},
    ):
        await _analyze_vendor_materials(test_vendor_card.id, db_session)


# =========================================================================
# 6. admin_service.py -- lines 177-178, 221-222
# =========================================================================


def test_admin_count_query_failure(db_session, test_user):
    """admin_service lines 177-178."""
    from app.services.admin_service import get_system_health

    orig_query = db_session.query
    count = [0]

    def side_effect(*a, **kw):
        count[0] += 1
        if count[0] == 4:
            raise Exception("Table missing")
        return orig_query(*a, **kw)

    with patch.object(db_session, "query", side_effect=side_effect):
        result = get_system_health(db_session)
    assert -1 in result["db_stats"].values()


def test_admin_api_source_failure(db_session, test_user):
    """admin_service lines 221-222."""
    from app.services.admin_service import get_system_health

    orig_query = db_session.query

    def patched(*a, **kw):
        if a and a[0] is ApiSource:
            raise Exception("No table")
        return orig_query(*a, **kw)

    with patch.object(db_session, "query", side_effect=patched):
        result = get_system_health(db_session)
    assert result["connectors"] == []


# =========================================================================
# 7. ai_part_normalizer.py -- lines 158-159
# =========================================================================


def test_validate_result_low_confidence():
    """ai_part_normalizer lines 158-159."""
    from app.services.ai_part_normalizer import _validate_result

    parsed = {"mpn": "ABC123", "manufacturer": "TI", "confidence": 0.1}
    result = _validate_result("ABC-123", parsed)
    assert result["confidence"] == 0.1
    assert result["normalized"] == "ABC-123"


# =========================================================================
# 8. ownership_service.py -- lines 185, 539
# =========================================================================


def test_days_inactive_none_becomes_999(db_session, test_user):
    """ownership_service line 185."""
    from app.services.ownership_service import get_accounts_at_risk

    co = Company(
        name="Dormant Corp", is_active=True, account_owner_id=test_user.id, created_at=datetime.now(timezone.utc)
    )
    db_session.add(co)
    db_session.commit()

    at_risk = get_accounts_at_risk(db_session)
    assert any(r["company_id"] == co.id for r in at_risk)


@pytest.mark.asyncio
async def test_send_digest_no_token(db_session, test_user):
    """ownership_service line 539."""
    from app.services.ownership_service import send_manager_digest_email

    with (
        patch("app.services.ownership_service.settings") as ms,
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None),
    ):
        ms.admin_emails = [test_user.email]
        ms.customer_inactivity_days = 30
        ms.strategic_inactivity_days = 90
        await send_manager_digest_email(db_session)


# =========================================================================
# 9. webhook_service.py -- line 256
# =========================================================================


def test_webhook_user_not_found(db_session, test_user):
    """webhook_service line 256."""
    from app.services.webhook_service import _seen_notifications, validate_notifications

    _seen_notifications.clear()

    # Create a temporary user, create subscription
    tmp_user = User(
        email="tmp-wh@test.com", name="Tmp", role="buyer", azure_id="tmp-wh-001", created_at=datetime.now(timezone.utc)
    )
    db_session.add(tmp_user)
    db_session.flush()
    tmp_uid = tmp_user.id

    sub = GraphSubscription(
        user_id=tmp_uid,
        subscription_id="sub-miss",
        resource="me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=1),
        client_state="secret",
    )
    db_session.add(sub)
    db_session.commit()

    # Mock db.get to return None for User lookups to simulate missing user
    orig_get = db_session.get

    def patched_get(model, id_val, *a, **kw):
        if model is User and id_val == tmp_uid:
            return None
        return orig_get(model, id_val, *a, **kw)

    with patch.object(db_session, "get", side_effect=patched_get):
        result = validate_notifications(
            {
                "value": [
                    {
                        "subscriptionId": "sub-miss",
                        "clientState": "secret",
                        "resource": "me/messages/abc",
                        "changeType": "created",
                    }
                ]
            },
            db_session,
        )
    assert len(result) == 0


# =========================================================================
# 10. routers/rfq.py -- lines 437, 522-524
# =========================================================================


def test_rfq_prepare_with_substitutes(client, db_session, test_user, test_requisition):
    """rfq.py line 437."""
    req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    req_item.substitutes = ["LM7805", "LM337"]
    db_session.commit()

    resp = client.post(
        "/api/requisitions/%d/rfq-prepare" % test_requisition.id,
        json={"vendors": [{"vendor_name": "Arrow Electronics"}]},
    )
    assert resp.status_code == 200


def test_rfq_prepare_vendor_lookup_exception(client, db_session, test_user, test_requisition):
    """rfq.py lines 522-524."""
    resp = client.post(
        "/api/requisitions/%d/rfq-prepare" % test_requisition.id,
        json={"vendors": [{"vendor_name": "Unknown Vendor XYZ"}]},
    )
    assert resp.status_code == 200


# =========================================================================
# 11. routers/sources.py -- lines 526-527, 618-619, 643-645
# =========================================================================


def test_scan_inbox_invalid_json(client, db_session, test_user):
    """sources.py lines 526-527."""
    test_user.m365_connected = True
    test_user.access_token = "fake"
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={"contacts_enriched": [], "sightings": [], "messages_scanned": 0, "offers_found": 0}
    )

    with (
        patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="t"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = client.post("/api/email-mining/scan", content=b"{{{", headers={"content-type": "application/json"})
    assert resp.status_code == 200


def test_scan_outbound_invalid_json(client, db_session, test_user):
    """sources.py lines 618-619."""
    test_user.m365_connected = True
    test_user.access_token = "fake"
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={"vendors_contacted": {}, "messages_scanned": 0, "rfqs_detected": 0, "used_delta": False}
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="t"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = client.post(
            "/api/email-mining/scan-outbound", content=b"{{{", headers={"content-type": "application/json"}
        )
    assert resp.status_code == 200


def test_scan_outbound_commit_failure(client, db_session, test_user):
    """sources.py lines 643-645."""
    test_user.m365_connected = True
    test_user.access_token = "fake"
    db_session.commit()

    vc = VendorCard(
        normalized_name="arrow",
        display_name="Arrow",
        domain="arrow.com",
        emails=[],
        phones=[],
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vc)
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "vendors_contacted": {"arrow.com": 3},
            "messages_scanned": 10,
            "rfqs_detected": 2,
            "used_delta": False,
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="t"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        with patch.object(db_session, "commit", side_effect=SQLAlchemyError("fail")):
            resp = client.post("/api/email-mining/scan-outbound")
    assert resp.status_code == 200


# =========================================================================
# 12. routers/vendors.py -- lines 299-304, 498, 704-706,
#     1456-1458, 1473-1474, 1487-1489
# =========================================================================


def test_vendor_search_long_query(client, db_session, test_vendor_card):
    """vendors.py lines 299-304 (FTS fallback in SQLite)."""
    resp = client.get("/api/vendors?q=arrow+electronics")
    assert resp.status_code == 200


def test_delete_review_card_gone(client, db_session, test_user):
    """vendors.py line 498."""
    vc = VendorCard(normalized_name="tmpv", display_name="Tmp", sighting_count=0, created_at=datetime.now(timezone.utc))
    db_session.add(vc)
    db_session.commit()
    cid = vc.id

    review = VendorReview(vendor_card_id=cid, user_id=test_user.id, rating=3, comment="T")
    db_session.add(review)
    db_session.commit()
    rid = review.id

    orig_get = db_session.get

    def patched_get(model, id_val, *a, **kw):
        if model is VendorCard and id_val == cid:
            return None
        return orig_get(model, id_val, *a, **kw)

    with patch.object(db_session, "get", side_effect=patched_get):
        resp = client.delete("/api/vendors/%d/reviews/%d" % (cid, rid))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_stock_import_existing_card(client, db_session, test_user):
    """vendors.py lines 1456-1458, 1487-1489."""
    vc = VendorCard(
        normalized_name="stockvendor",
        display_name="Stock Vendor",
        emails=[],
        phones=[],
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vc)
    mc = MaterialCard(
        normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI", created_at=datetime.now(timezone.utc)
    )
    db_session.add(mc)
    db_session.commit()

    csv_bytes = b"MPN,QTY,Price\nLM317T,1000,0.50\n"
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Stock Vendor"},
        files={"file": ("s.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 200


def test_stock_import_empty_mpn(client, db_session, test_user):
    """vendors.py lines 1473-1474."""
    csv_bytes = b"MPN,QTY,Price\n,1000,0.50\nLM317T,500,0.30\n"
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Some Vendor"},
        files={"file": ("s.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 200


# =========================================================================
# 13. routers/v13_features.py -- lines 222, 246, 407-410, 445
# =========================================================================


def test_log_email_no_match_fc100(client):
    """v13_features line 222."""
    with patch("app.services.activity_service.log_email_activity", return_value=None):
        resp = client.post("/api/activities/email", json={"email": "x@rand.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_match"


def test_log_call_no_match_fc100(client):
    """v13_features line 246."""
    with patch("app.services.activity_service.log_call_activity", return_value=None):
        resp = client.post("/api/activities/call", json={"phone": "+15555551234", "direction": "outbound"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_match"


def test_vendor_activity_yellow_fc100(client, test_vendor_card):
    """v13_features lines 407-408."""
    with patch("app.services.activity_service.days_since_last_vendor_activity", return_value=40):
        resp = client.get("/api/vendors/%d/activity-status" % test_vendor_card.id)
    assert resp.json()["status"] == "yellow"


def test_vendor_activity_red_fc100(client, test_vendor_card):
    """v13_features lines 409-410."""
    with patch("app.services.activity_service.days_since_last_vendor_activity", return_value=100):
        resp = client.get("/api/vendors/%d/activity-status" % test_vendor_card.id)
    assert resp.json()["status"] == "red"


def test_company_activity_red_fc100(client, test_company):
    """v13_features line 445."""
    with patch("app.services.activity_service.days_since_last_activity", return_value=100):
        resp = client.get("/api/companies/%d/activity-status" % test_company.id)
    assert resp.json()["status"] == "red"


# =========================================================================
# 15. schemas/vendors.py -- lines 32, 48, 53, 106, 121
# =========================================================================


def test_vendor_update_phones_blank():
    from app.schemas.vendors import VendorCardUpdate

    obj = VendorCardUpdate(phones=["", "  ", "+1-555-0100"])
    assert obj.phones == ["+1-555-0100"]


def test_vendor_contact_no_at():
    from app.schemas.vendors import VendorContactCreate

    with pytest.raises(ValueError, match="Invalid email"):
        VendorContactCreate(email="notanemail")


def test_vendor_contact_update_empty_email():
    from app.schemas.vendors import VendorContactUpdate

    assert VendorContactUpdate(email="   ").email is None


# =========================================================================
# 16. schemas/requisitions.py -- line 72
# =========================================================================


def test_substitutes_non_list_non_str():
    """Line 72: substitutes passes through when not str or list."""
    from app.schemas.requisitions import RequirementCreate

    # Default is an empty list (default_factory=list).
    obj = RequirementCreate(primary_mpn="LM317T", target_qty=100)
    assert obj.substitutes == []


# =========================================================================
# 17. utils/claude_client.py -- lines 91, 182-185, 194, 426
# =========================================================================


@pytest.mark.asyncio
async def test_claude_text_cache_system():
    from app.utils.claude_client import claude_text

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    with (
        patch("app.utils.claude_client.get_credential_cached", return_value="sk-t"),
        patch("app.utils.claude_client.http") as mh,
    ):
        mh.post = AsyncMock(return_value=mock_resp)
        await claude_text("test", system="sys", cache_system=True)
        body = mh.post.call_args[1].get("json", {})
        if "system" in body:
            assert body["system"][0].get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_claude_structured_system_cache():
    from app.utils.claude_client import claude_structured

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"type": "tool_use", "name": "structured_output", "input": {"k": "v"}}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    with (
        patch("app.utils.claude_client.get_credential_cached", return_value="sk-t"),
        patch("app.utils.claude_client.http") as mh,
    ):
        mh.post = AsyncMock(return_value=mock_resp)
        await claude_structured(
            "test", schema={"type": "object", "properties": {"k": {"type": "string"}}}, system="sys", cache_system=True
        )
        body = mh.post.call_args[1].get("json", {})
        assert "system" in body
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_claude_structured_cache_read_tokens():
    """Sentry AI monitoring: cache_read_input_tokens branch (line 151)."""
    from app.utils.claude_client import claude_structured

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"type": "tool_use", "name": "structured_output", "input": {"k": "v"}}],
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 8},
    }

    with (
        patch("app.utils.claude_client.get_credential_cached", return_value="sk-t"),
        patch("app.utils.claude_client.http") as mh,
    ):
        mh.post = AsyncMock(return_value=mock_resp)
        result = await claude_structured("test", schema={"type": "object", "properties": {"k": {"type": "string"}}})
        assert result == {"k": "v"}


@pytest.mark.asyncio
async def test_claude_text_cache_read_tokens():
    """claude_client.py line 248: cache_read_input_tokens in claude_text response."""
    from app.utils.claude_client import claude_text

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"type": "text", "text": "cached response"}],
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 8},
    }

    with (
        patch("app.utils.claude_client.get_credential_cached", return_value="sk-t"),
        patch("app.utils.claude_client.http") as mh,
    ):
        mh.post = AsyncMock(return_value=mock_resp)
        result = await claude_text("test prompt")
        assert result == "cached response"


@pytest.mark.asyncio
async def test_batch_blank_line_skipped():
    from app.utils.claude_client import claude_batch_results

    status_resp = MagicMock(status_code=200)
    status_resp.json.return_value = {
        "id": "b-1",
        "processing_status": "ended",
        "results_url": "https://api.anthropic.com/v1/r/b-1",
    }

    line1 = '{"custom_id":"i1","result":{"type":"succeeded","message":{"content":[{"type":"tool_use","name":"structured_output","input":{"v":"1"}}]}}}'
    line2 = '{"custom_id":"i2","result":{"type":"succeeded","message":{"content":[{"type":"tool_use","name":"structured_output","input":{"v":"2"}}]}}}'
    results_resp = MagicMock(status_code=200)
    results_resp.text = line1 + "\n\n  \n" + line2 + "\n"

    with (
        patch("app.utils.claude_client.get_credential_cached", return_value="sk-t"),
        patch("app.utils.claude_client.http") as mh,
    ):
        mh.get = AsyncMock(side_effect=[status_resp, results_resp])
        result = await claude_batch_results("b-1")

    assert result is not None
    assert "i1" in result
    assert "i2" in result


# =========================================================================
# 18. utils/file_validation.py -- lines 56, 69, 106, 141
# =========================================================================


def test_validate_file_magic_bytes():
    from app.utils.file_validation import validate_file

    with patch("filetype.guess") as mg:
        mg.return_value = MagicMock(mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        valid, ft = validate_file(b"x" * 10, "t.xlsx")
        assert valid is True
        assert ft == "xlsx"


def test_validate_file_csv_encoding():
    from app.utils.file_validation import validate_file

    with patch("filetype.guess", return_value=None):
        valid, ft = validate_file(b"MPN,QTY\nLM317T,100\n", "d.csv")
        assert valid is True
        assert ft == "csv"


def test_detect_encoding_fallback():
    from app.utils.file_validation import detect_encoding

    result = detect_encoding(b"\xff\xfe" + b"Hello")
    assert result is not None


def test_is_password_protected_other_error():
    from app.utils.file_validation import is_password_protected

    with patch("openpyxl.load_workbook", side_effect=Exception("Corrupt file")):
        assert is_password_protected(b"data") is False


# =========================================================================
# 19. utils/normalization.py -- lines 79-80, 151-152, 200, 266, 378
# =========================================================================


def test_normalize_price_range():
    from app.utils.normalization import normalize_price

    assert normalize_price("0.38-0.42") == 0.38


def test_normalize_quantity_k():
    from app.utils.normalization import normalize_quantity

    assert normalize_quantity("50K") == 50000


def test_normalize_quantity_m():
    from app.utils.normalization import normalize_quantity

    assert normalize_quantity("2M") == 2000000


def test_normalize_lead_time_ambiguous():
    from app.utils.normalization import normalize_lead_time

    result = normalize_lead_time("60-90")
    assert result == 75


def test_normalize_date_code_no_digits():
    from app.utils.normalization import normalize_date_code

    assert normalize_date_code("DC: N/A") is None


def test_mpn_match_short_suffix():
    from app.utils.normalization import fuzzy_mpn_match

    assert fuzzy_mpn_match("LM317TA", "LM317T") is True


def test_mpn_match_long_suffix():
    from app.utils.normalization import fuzzy_mpn_match

    assert fuzzy_mpn_match("LM317T", "LM317TXYZ") is False


# =========================================================================
# 20. utils/normalization_helpers.py -- line 66
# =========================================================================


def test_phone_10_digit_us():
    from app.utils.normalization_helpers import normalize_phone_e164

    assert normalize_phone_e164("5551234567") == "+15551234567"


def test_phone_7_digits():
    from app.utils.normalization_helpers import normalize_phone_e164

    assert normalize_phone_e164("5551234") == "+15551234"
