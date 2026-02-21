"""
test_proactive_service.py — Tests for app/services/proactive_service.py

Covers: scan_new_offers_for_matches, send_proactive_offer,
        convert_proactive_to_win, get_scorecard, get_sent_offers.

Business rules tested:
- Scanning new offers against archived requirements produces ProactiveMatch rows
- Short MPNs (<3 chars), self-matches, throttled, and duplicate matches are skipped
- Sending proactive offers builds emails, saves ProactiveOffer, and handles Graph API errors
- Converting a proactive offer creates Requisition + Quote + BuyPlan
- Scorecard aggregates metrics; admin view includes per-user breakdown
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    BuyPlan,
    Company,
    CustomerSite,
    Offer,
    ProactiveMatch,
    ProactiveOffer,
    ProactiveThrottle,
    Quote,
    Requirement,
    Requisition,
    SiteContact,
    User,
)
from app.services.proactive_service import (
    convert_proactive_to_win,
    get_scorecard,
    get_sent_offers,
    scan_new_offers_for_matches,
    send_proactive_offer,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _reset_last_scan():
    """Reset the module-level _last_proactive_scan to datetime.min so all
    offers appear as 'new' to the scanner."""
    import app.services.proactive_service as mod

    mod._last_proactive_scan = datetime.min.replace(tzinfo=timezone.utc)


def _register_btrim(db_session: Session):
    """Register a SQLite 'btrim' function so sqlfunc.btrim works in tests.
    PostgreSQL has btrim natively; SQLite does not."""
    raw_conn = db_session.get_bind().raw_connection()
    raw_conn.create_function("btrim", 1, lambda s: s.strip() if s else s)
    raw_conn.close()


def _make_archived_requisition(
    db: Session,
    user: User,
    site: CustomerSite,
    mpn: str = "LM317T",
    target_qty: int = 500,
    days_ago: int = 60,
) -> tuple[Requisition, Requirement]:
    """Create an archived requisition with one requirement at a customer site."""
    req = Requisition(
        name=f"Archived-{mpn}",
        customer_name="Acme Electronics",
        status="archived",
        created_by=user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=target_qty,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(item)
    db.flush()
    return req, item


def _make_offer(
    db: Session,
    requisition: Requisition,
    user: User,
    mpn: str = "LM317T",
    qty: int = 1000,
    price: float = 0.50,
) -> Offer:
    """Create an offer on a requisition (entered just now)."""
    o = Offer(
        requisition_id=requisition.id,
        vendor_name="SupplierCo",
        mpn=mpn,
        qty_available=qty,
        unit_price=price,
        entered_by_id=user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add(o)
    db.flush()
    return o


def _make_site_contact(db: Session, site: CustomerSite) -> SiteContact:
    """Create a SiteContact at the given customer site."""
    sc = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Contact",
        email="jane@acme-electronics.com",
    )
    db.add(sc)
    db.flush()
    return sc


# ── scan_new_offers_for_matches ────────────────────────────────────────


class TestScanNewOffersForMatches:
    """Tests for the background matching scan."""

    def test_scan_no_offers(self, db_session):
        """No offers in DB -> returns {scanned: 0, matches_created: 0}."""
        _reset_last_scan()
        result = scan_new_offers_for_matches(db_session)
        assert result["scanned"] == 0
        assert result["matches_created"] == 0

    def test_scan_creates_match(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """Offer with MPN matching an archived requirement creates ProactiveMatch."""
        _reset_last_scan()
        _register_btrim(db_session)

        # A second user is the salesperson who owns the archived req
        sales = User(
            email="sales@trioscs.com",
            name="Sales Rep",
            role="sales",
            azure_id="sales-az-001",
        )
        db_session.add(sales)
        db_session.flush()

        archived_req, _ = _make_archived_requisition(
            db_session, sales, test_customer_site, mpn="LM317T"
        )

        # Offer entered by test_user on a DIFFERENT requisition
        source_req = Requisition(
            name="Source-Req",
            customer_name="Other Co",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(source_req)
        db_session.flush()

        _make_offer(db_session, source_req, test_user, mpn="LM317T")
        db_session.commit()

        result = scan_new_offers_for_matches(db_session)
        assert result["scanned"] == 1
        assert result["matches_created"] == 1

        match = db_session.query(ProactiveMatch).first()
        assert match is not None
        assert match.mpn == "LM317T"
        assert match.salesperson_id == sales.id
        assert match.customer_site_id == test_customer_site.id

    def test_scan_skips_short_mpn(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """Offer with MPN shorter than 3 chars is skipped."""
        _reset_last_scan()
        _register_btrim(db_session)

        sales = User(
            email="sales2@trioscs.com",
            name="Sales2",
            role="sales",
            azure_id="sales-az-002",
        )
        db_session.add(sales)
        db_session.flush()

        _make_archived_requisition(db_session, sales, test_customer_site, mpn="AB")

        source_req = Requisition(
            name="Src-Short",
            customer_name="X",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(source_req)
        db_session.flush()
        _make_offer(db_session, source_req, test_user, mpn="AB")
        db_session.commit()

        result = scan_new_offers_for_matches(db_session)
        assert result["scanned"] == 1
        assert result["matches_created"] == 0

    def test_scan_respects_throttle(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """Throttle entry for same MPN+site prevents new match."""
        _reset_last_scan()
        _register_btrim(db_session)

        sales = User(
            email="sales3@trioscs.com",
            name="Sales3",
            role="sales",
            azure_id="sales-az-003",
        )
        db_session.add(sales)
        db_session.flush()

        _make_archived_requisition(
            db_session, sales, test_customer_site, mpn="THROTTLED1"
        )

        # Add a recent throttle entry
        db_session.add(
            ProactiveThrottle(
                mpn="THROTTLED1",
                customer_site_id=test_customer_site.id,
                last_offered_at=datetime.now(timezone.utc),
            )
        )

        source_req = Requisition(
            name="Src-Thr",
            customer_name="Y",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(source_req)
        db_session.flush()
        _make_offer(db_session, source_req, test_user, mpn="THROTTLED1")
        db_session.commit()

        result = scan_new_offers_for_matches(db_session)
        assert result["matches_created"] == 0

    def test_scan_deduplicates(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """Same offer+requirement already matched -> no duplicate ProactiveMatch."""
        _reset_last_scan()
        _register_btrim(db_session)

        sales = User(
            email="sales4@trioscs.com",
            name="Sales4",
            role="sales",
            azure_id="sales-az-004",
        )
        db_session.add(sales)
        db_session.flush()

        archived_req, req_item = _make_archived_requisition(
            db_session, sales, test_customer_site, mpn="DEDUP123"
        )

        source_req = Requisition(
            name="Src-Dup",
            customer_name="Z",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(source_req)
        db_session.flush()

        offer = _make_offer(db_session, source_req, test_user, mpn="DEDUP123")

        # Pre-create a match for this offer+requirement
        db_session.add(
            ProactiveMatch(
                offer_id=offer.id,
                requirement_id=req_item.id,
                requisition_id=archived_req.id,
                customer_site_id=test_customer_site.id,
                salesperson_id=sales.id,
                mpn="DEDUP123",
            )
        )
        db_session.commit()

        result = scan_new_offers_for_matches(db_session)
        assert result["matches_created"] == 0
        # Still only the one pre-existing match
        count = db_session.query(ProactiveMatch).count()
        assert count == 1

    def test_scan_skips_self_match(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """Offer on the same requisition as the archived requirement is skipped."""
        _reset_last_scan()
        _register_btrim(db_session)

        # The archived req and the offer are on the SAME requisition
        archived_req, _ = _make_archived_requisition(
            db_session, test_user, test_customer_site, mpn="SELFMATCH"
        )

        # Add offer to the same requisition
        _make_offer(db_session, archived_req, test_user, mpn="SELFMATCH")
        db_session.commit()

        result = scan_new_offers_for_matches(db_session)
        assert result["matches_created"] == 0

    def test_scan_multiple_matches(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """One offer matches multiple requirements -> multiple ProactiveMatch rows."""
        _reset_last_scan()
        _register_btrim(db_session)

        sales = User(
            email="sales5@trioscs.com",
            name="Sales5",
            role="sales",
            azure_id="sales-az-005",
        )
        db_session.add(sales)
        db_session.flush()

        # Two different archived requisitions for the same MPN at the same site
        site2 = CustomerSite(
            company_id=test_company.id,
            site_name="Acme Branch",
            contact_name="Bob",
            contact_email="bob@acme.com",
        )
        db_session.add(site2)
        db_session.flush()

        _make_archived_requisition(
            db_session, sales, test_customer_site, mpn="MULTI001"
        )
        _make_archived_requisition(db_session, sales, site2, mpn="MULTI001")

        source_req = Requisition(
            name="Src-Multi",
            customer_name="W",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(source_req)
        db_session.flush()
        _make_offer(db_session, source_req, test_user, mpn="MULTI001")
        db_session.commit()

        result = scan_new_offers_for_matches(db_session)
        assert result["matches_created"] == 2

        matches = db_session.query(ProactiveMatch).all()
        assert len(matches) == 2
        site_ids = {m.customer_site_id for m in matches}
        assert site_ids == {test_customer_site.id, site2.id}


# ── send_proactive_offer ───────────────────────────────────────────────


class TestSendProactiveOffer:
    """Tests for sending proactive offer emails."""

    def test_send_no_matches(self, db_session, test_user):
        """Empty match_ids raises ValueError."""
        with pytest.raises(ValueError, match="No valid matches"):
            asyncio.get_event_loop().run_until_complete(
                send_proactive_offer(
                    db=db_session,
                    user=test_user,
                    token="fake-token",
                    match_ids=[],
                    contact_ids=[1],
                    sell_prices={},
                )
            )

    def test_send_no_contacts(
        self, db_session, test_user, test_company, test_customer_site, test_offer
    ):
        """Valid matches but empty contact_ids raises ValueError."""
        # Create a match for the test_offer
        archived_req, req_item = _make_archived_requisition(
            db_session, test_user, test_customer_site, mpn="LM317T"
        )
        match = ProactiveMatch(
            offer_id=test_offer.id,
            requirement_id=req_item.id,
            requisition_id=archived_req.id,
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            mpn="LM317T",
        )
        db_session.add(match)
        db_session.commit()

        with pytest.raises(ValueError, match="No valid contacts"):
            asyncio.get_event_loop().run_until_complete(
                send_proactive_offer(
                    db=db_session,
                    user=test_user,
                    token="fake-token",
                    match_ids=[match.id],
                    contact_ids=[],
                    sell_prices={},
                )
            )

    @pytest.mark.asyncio
    async def test_send_success(
        self, db_session, test_user, test_company, test_customer_site, test_offer
    ):
        """Mocked GraphClient.post_json -> creates ProactiveOffer and returns data."""
        archived_req, req_item = _make_archived_requisition(
            db_session, test_user, test_customer_site, mpn="LM317T"
        )
        match = ProactiveMatch(
            offer_id=test_offer.id,
            requirement_id=req_item.id,
            requisition_id=archived_req.id,
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            mpn="LM317T",
        )
        db_session.add(match)
        contact = _make_site_contact(db_session, test_customer_site)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value=None)

        with patch(
            "app.utils.graph_client.GraphClient", return_value=mock_gc
        ):
            result = await send_proactive_offer(
                db=db_session,
                user=test_user,
                token="fake-token",
                match_ids=[match.id],
                contact_ids=[contact.id],
                sell_prices={str(match.id): 0.75},
            )

        assert result["id"] is not None
        assert result["status"] == "sent"
        assert "jane@acme-electronics.com" in result["recipient_emails"]
        assert len(result["line_items"]) == 1

        # Verify match status updated
        db_session.refresh(match)
        assert match.status == "sent"

        # Verify throttle created
        throttle = db_session.query(ProactiveThrottle).first()
        assert throttle is not None
        assert throttle.mpn == "LM317T"

    @pytest.mark.asyncio
    async def test_send_calculates_totals(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """Verify total_sell and total_cost are computed from line items."""
        # Create an offer with known price and qty
        source_req = Requisition(
            name="Totals-Req",
            customer_name="Test",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(source_req)
        db_session.flush()

        offer = Offer(
            requisition_id=source_req.id,
            vendor_name="Vendor",
            mpn="TOTAL100",
            qty_available=100,
            unit_price=2.00,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        archived_req, req_item = _make_archived_requisition(
            db_session, test_user, test_customer_site, mpn="TOTAL100"
        )
        match = ProactiveMatch(
            offer_id=offer.id,
            requirement_id=req_item.id,
            requisition_id=archived_req.id,
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            mpn="TOTAL100",
        )
        db_session.add(match)
        contact = _make_site_contact(db_session, test_customer_site)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value=None)

        with patch(
            "app.utils.graph_client.GraphClient", return_value=mock_gc
        ):
            result = await send_proactive_offer(
                db=db_session,
                user=test_user,
                token="fake-token",
                match_ids=[match.id],
                contact_ids=[contact.id],
                sell_prices={str(match.id): 3.00},  # sell price = 3.00
            )

        # total_sell = 3.00 * 100 = 300.00
        # total_cost = 2.00 * 100 = 200.00
        assert result["total_sell"] == 300.00
        assert result["total_cost"] == 200.00

    @pytest.mark.asyncio
    async def test_send_graph_error_still_saves(
        self, db_session, test_user, test_company, test_customer_site, test_offer
    ):
        """GraphClient raises exception -> ProactiveOffer is still saved."""
        archived_req, req_item = _make_archived_requisition(
            db_session, test_user, test_customer_site, mpn="LM317T"
        )
        match = ProactiveMatch(
            offer_id=test_offer.id,
            requirement_id=req_item.id,
            requisition_id=archived_req.id,
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            mpn="LM317T",
        )
        db_session.add(match)
        contact = _make_site_contact(db_session, test_customer_site)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Graph API down"))

        with patch(
            "app.utils.graph_client.GraphClient", return_value=mock_gc
        ):
            result = await send_proactive_offer(
                db=db_session,
                user=test_user,
                token="fake-token",
                match_ids=[match.id],
                contact_ids=[contact.id],
                sell_prices={},
            )

        # Offer should still be persisted
        assert result["id"] is not None
        po = db_session.get(ProactiveOffer, result["id"])
        assert po is not None
        # Match status should still be updated
        db_session.refresh(match)
        assert match.status == "sent"


# ── convert_proactive_to_win ───────────────────────────────────────────


class TestConvertProactiveToWin:
    """Tests for converting a proactive offer to a won requisition."""

    def test_convert_not_found(self, db_session, test_user):
        """Invalid proactive_offer_id raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            convert_proactive_to_win(db_session, 99999, test_user)

    def test_convert_not_yours(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """Offer belongs to different user raises ValueError."""
        other_user = User(
            email="other@trioscs.com",
            name="Other",
            role="buyer",
            azure_id="other-az-001",
        )
        db_session.add(other_user)
        db_session.flush()

        po = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=other_user.id,
            line_items=[],
            recipient_emails=["x@x.com"],
            subject="Test",
            status="sent",
            total_sell=100,
            total_cost=50,
        )
        db_session.add(po)
        db_session.commit()

        with pytest.raises(ValueError, match="Not your"):
            convert_proactive_to_win(db_session, po.id, test_user)

    def test_convert_already_converted(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """Offer already converted raises ValueError."""
        po = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            line_items=[],
            recipient_emails=["x@x.com"],
            subject="Test",
            status="converted",
            total_sell=100,
            total_cost=50,
        )
        db_session.add(po)
        db_session.commit()

        with pytest.raises(ValueError, match="Already converted"):
            convert_proactive_to_win(db_session, po.id, test_user)

    @patch("app.services.crm_service.next_quote_number", return_value="Q-2026-0100")
    def test_convert_success(
        self,
        mock_qn,
        db_session,
        test_user,
        test_company,
        test_customer_site,
        test_offer,
    ):
        """Conversion creates Requisition (status=won), Requirements, Offers, Quote, BuyPlan."""
        # Build a ProactiveOffer with a real offer reference
        po = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            line_items=[
                {
                    "match_id": None,
                    "offer_id": test_offer.id,
                    "mpn": "LM317T",
                    "vendor_name": "Arrow Electronics",
                    "manufacturer": "TI",
                    "qty": 1000,
                    "unit_price": 0.50,
                    "sell_price": 0.75,
                    "condition": "New",
                    "lead_time": "2 weeks",
                }
            ],
            recipient_emails=["jane@acme.com"],
            subject="Proactive",
            status="sent",
            total_sell=750,
            total_cost=500,
        )
        db_session.add(po)
        db_session.commit()

        result = convert_proactive_to_win(db_session, po.id, test_user)

        assert "requisition_id" in result
        assert "quote_id" in result
        assert "buy_plan_id" in result

        # Verify requisition
        req = db_session.get(Requisition, result["requisition_id"])
        assert req is not None
        assert req.status == "won"
        assert "Proactive" in req.name

        # Verify requirement created
        reqs = (
            db_session.query(Requirement)
            .filter(Requirement.requisition_id == req.id)
            .all()
        )
        assert len(reqs) == 1
        assert reqs[0].primary_mpn == "LM317T"

        # Verify new offer created on the new requisition
        new_offers = (
            db_session.query(Offer)
            .filter(Offer.requisition_id == req.id)
            .all()
        )
        assert len(new_offers) == 1
        assert new_offers[0].source == "proactive"

        # Verify quote
        quote = db_session.get(Quote, result["quote_id"])
        assert quote is not None
        assert quote.quote_number == "Q-2026-0100"
        assert quote.status == "won"

        # Verify buy plan
        bp = db_session.get(BuyPlan, result["buy_plan_id"])
        assert bp is not None
        assert bp.status == "pending_approval"
        assert bp.submitted_by_id == test_user.id

        # ProactiveOffer updated
        db_session.refresh(po)
        assert po.status == "converted"
        assert po.converted_requisition_id == req.id

    @patch("app.services.crm_service.next_quote_number", return_value="Q-2026-0101")
    def test_convert_updates_match_status(
        self,
        mock_qn,
        db_session,
        test_user,
        test_company,
        test_customer_site,
        test_offer,
    ):
        """ProactiveMatch.status is updated to 'converted' after conversion."""
        # Create a match first
        archived_req, req_item = _make_archived_requisition(
            db_session, test_user, test_customer_site, mpn="LM317T"
        )
        match = ProactiveMatch(
            offer_id=test_offer.id,
            requirement_id=req_item.id,
            requisition_id=archived_req.id,
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            mpn="LM317T",
            status="sent",
        )
        db_session.add(match)
        db_session.flush()

        po = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            line_items=[
                {
                    "match_id": match.id,
                    "offer_id": test_offer.id,
                    "mpn": "LM317T",
                    "vendor_name": "Arrow",
                    "manufacturer": "TI",
                    "qty": 1000,
                    "unit_price": 0.50,
                    "sell_price": 0.75,
                    "condition": "New",
                    "lead_time": "2w",
                }
            ],
            recipient_emails=["jane@acme.com"],
            subject="Proactive",
            status="sent",
            total_sell=750,
            total_cost=500,
        )
        db_session.add(po)
        db_session.commit()

        convert_proactive_to_win(db_session, po.id, test_user)

        db_session.refresh(match)
        assert match.status == "converted"


# ── Scorecard & Sent Offers ────────────────────────────────────────────


class TestScorecardAndSentOffers:
    """Tests for get_scorecard and get_sent_offers."""

    def test_scorecard_empty(self, db_session):
        """No proactive offers -> all zeroes."""
        result = get_scorecard(db_session)
        assert result["total_sent"] == 0
        assert result["total_converted"] == 0
        assert result["converted_revenue"] == 0
        assert result["gross_profit"] == 0
        assert result["conversion_rate"] == 0

    def test_scorecard_with_data(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """ProactiveOffers with mixed statuses are correctly aggregated."""
        # Sent offer
        po1 = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            line_items=[],
            recipient_emails=["a@a.com"],
            subject="Offer 1",
            status="sent",
            total_sell=1000,
            total_cost=600,
            sent_at=datetime.now(timezone.utc),
        )
        # Converted offer
        po2 = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            line_items=[],
            recipient_emails=["b@b.com"],
            subject="Offer 2",
            status="converted",
            total_sell=2000,
            total_cost=1200,
            sent_at=datetime.now(timezone.utc),
        )
        db_session.add_all([po1, po2])
        db_session.commit()

        result = get_scorecard(db_session)
        assert result["total_sent"] == 2
        assert result["total_converted"] == 1
        assert result["converted_revenue"] == 2000.0
        assert result["gross_profit"] == 800.0
        assert result["anticipated_revenue"] == 1000.0
        assert result["conversion_rate"] == 50.0

    def test_scorecard_admin_breakdown(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """No salesperson_id filter -> includes per-user breakdown."""
        po = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            line_items=[],
            recipient_emails=["c@c.com"],
            subject="Admin View",
            status="sent",
            total_sell=500,
            total_cost=300,
            sent_at=datetime.now(timezone.utc),
        )
        db_session.add(po)
        db_session.commit()

        result = get_scorecard(db_session, salesperson_id=None)
        assert "breakdown" in result
        assert len(result["breakdown"]) == 1
        assert result["breakdown"][0]["salesperson_id"] == test_user.id
        assert result["breakdown"][0]["sent"] == 1

    def test_scorecard_filtered(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """With salesperson_id filter -> only that user's data, no breakdown."""
        other_user = User(
            email="other2@trioscs.com",
            name="Other2",
            role="sales",
            azure_id="other-az-002",
        )
        db_session.add(other_user)
        db_session.flush()

        # test_user's offer
        po1 = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            line_items=[],
            recipient_emails=["d@d.com"],
            subject="Mine",
            status="sent",
            total_sell=1000,
            total_cost=600,
            sent_at=datetime.now(timezone.utc),
        )
        # other_user's offer
        po2 = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=other_user.id,
            line_items=[],
            recipient_emails=["e@e.com"],
            subject="Theirs",
            status="converted",
            total_sell=5000,
            total_cost=3000,
            sent_at=datetime.now(timezone.utc),
        )
        db_session.add_all([po1, po2])
        db_session.commit()

        result = get_scorecard(db_session, salesperson_id=test_user.id)
        assert result["total_sent"] == 1
        assert result["total_converted"] == 0
        assert result["converted_revenue"] == 0
        # No breakdown when filtered by salesperson
        assert "breakdown" not in result

    def test_get_sent_offers(
        self, db_session, test_user, test_company, test_customer_site
    ):
        """Returns list of sent offers for the user."""
        po = ProactiveOffer(
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            line_items=[{"mpn": "ABC123", "qty": 50}],
            recipient_emails=["f@f.com"],
            subject="Sent Offer",
            status="sent",
            total_sell=250,
            total_cost=150,
            sent_at=datetime.now(timezone.utc),
        )
        db_session.add(po)
        db_session.commit()

        offers = get_sent_offers(db_session, test_user.id)
        assert len(offers) == 1
        assert offers[0]["subject"] == "Sent Offer"
        assert offers[0]["total_sell"] == 250.0
        assert offers[0]["status"] == "sent"
        assert len(offers[0]["line_items"]) == 1
