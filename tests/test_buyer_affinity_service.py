"""Tests for buyer_affinity_service (Chunk C) — who-to-offer ranking, the buyer
scorecard rollup, the advisory team-overlap check, and the "usually-offered, not yet"
nudge strip.

The buyer-affinity layer is BUYER-side: it ranks the VendorCards we'd offer excess
TO (the inverse of the vendor coverage ranking), keyed on the canonical buyer
``vendor_card_id``. It must never touch the customer_excess supply Sighting mirror.

Covers:
  - rank_buyers_for     — tiered exact-MPN > commodity > engagement; DNC/unreachable
                          buyers filtered the same way the RFQ suggestion does.
  - recompute_buyer_score — rollup math (offers_received, wins, avg_bid_pct_of_ask,
                          response_rate, commodity_affinity) + upsert.
  - overlap_warning     — teammate-offered → advisory dict; same-owner → None; never
                          raises / blocks.
  - not_yet_offered_strip — historical commodity buyers absent from THIS list's
                          outreach.

Called by: pytest
Depends on: app.services.buyer_affinity_service, tests.conftest
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessOfferStatus, ExcessOutreachStatus, OfferLineMatchStatus
from app.models import Company, User, VendorCard
from app.models.excess import (
    BuyerScore,
    ExcessLineItem,
    ExcessList,
    ExcessOffer,
    ExcessOfferLine,
    ExcessOutreach,
)
from app.models.intelligence import MaterialCard
from app.models.vendors import VendorContact
from app.services import buyer_affinity_service as svc
from tests.conftest import engine

_ = engine

_CAP = "capacitors"
_CONN = "connectors"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def trader(db_session: Session) -> User:
    u = User(email="c-trader@trioscs.com", name="C Trader", role="trader", azure_id="c-trader-001")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def teammate(db_session: Session) -> User:
    u = User(email="c-mate@trioscs.com", name="C Mate", role="trader", azure_id="c-mate-001")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def seller_company(db_session: Session) -> Company:
    co = Company(name="Seller Co")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def _material_card(db: Session, mpn: str, category: str) -> MaterialCard:
    mc = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=category)
    db.add(mc)
    db.flush()
    return mc


def _reachable_card(db: Session, name: str, *, engagement: float | None = None) -> VendorCard:
    """A buyer card with a resolvable VendorContact email (so the RFQ reachability gate
    keeps it) and optional engagement score."""
    vc = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        emails=[f"sales@{name.lower().replace(' ', '')}.com"],
        engagement_score=engagement,
    )
    db.add(vc)
    db.flush()
    db.add(
        VendorContact(
            vendor_card_id=vc.id,
            email=f"sales@{name.lower().replace(' ', '')}.com",
            full_name="Buyer",
            source="test",
        )
    )
    db.flush()
    return vc


@pytest.fixture()
def excess_list(db_session: Session, seller_company: Company, trader: User) -> ExcessList:
    el = ExcessList(company_id=seller_company.id, owner_id=trader.id, title="C Excess")
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def cap_line(db_session: Session, excess_list: ExcessList) -> ExcessLineItem:
    mc = _material_card(db_session, "GRM188R", _CAP)
    li = ExcessLineItem(
        excess_list_id=excess_list.id,
        part_number="GRM188R",
        quantity=1000,
        material_card_id=mc.id,
        asking_price=Decimal("1.00"),
    )
    db_session.add(li)
    db_session.commit()
    db_session.refresh(li)
    return li


def _won_offer_for(
    db: Session,
    *,
    excess_list: ExcessList,
    buyer: VendorCard,
    owner: User,
    line: ExcessLineItem,
    unit_price: Decimal,
) -> ExcessOffer:
    """A WON ExcessOffer from ``buyer`` carrying one matched line — the bought-this-part
    history signal."""
    offer = ExcessOffer(
        excess_list_id=excess_list.id,
        submitted_by=owner.id,
        offerer_vendor_card_id=buyer.id,
        scope="per_line",
        status=ExcessOfferStatus.WON,
    )
    db.add(offer)
    db.flush()
    db.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw=line.part_number,
            quantity=line.quantity,
            unit_price=unit_price,
            match_status=OfferLineMatchStatus.MATCHED,
        )
    )
    db.flush()
    return offer


# ═══════════════════════════════════════════════════════════════════════
#  rank_buyers_for
# ═══════════════════════════════════════════════════════════════════════


class TestRankBuyersFor:
    def test_exact_mpn_outranks_commodity_outranks_engagement(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User
    ):
        # exact_buyer: won an offer on THIS material_card → top tier.
        exact_buyer = _reachable_card(db_session, "Exact Buyer", engagement=10.0)
        _won_offer_for(
            db_session,
            excess_list=excess_list,
            buyer=exact_buyer,
            owner=trader,
            line=cap_line,
            unit_price=Decimal("0.80"),
        )
        # commodity_buyer: tagged for capacitors but no buy on this exact card.
        commodity_buyer = _reachable_card(db_session, "Commodity Buyer", engagement=20.0)
        commodity_buyer.commodity_tags = [_CAP]
        # engagement_buyer: neither part nor commodity, just a high engagement score.
        engagement_buyer = _reachable_card(db_session, "Engagement Buyer", engagement=99.0)
        engagement_buyer.commodity_tags = [_CONN]
        db_session.commit()

        ranked = svc.rank_buyers_for(db_session, excess_list_id=excess_list.id)
        ids = [r.vendor_card_id for r in ranked]

        assert ids.index(exact_buyer.id) < ids.index(commodity_buyer.id)
        assert ids.index(commodity_buyer.id) < ids.index(engagement_buyer.id)
        # Rank reasons are buyer-side and tier-distinct.
        by_id = {r.vendor_card_id: r for r in ranked}
        assert by_id[exact_buyer.id].rank_reason == "bought_this_part"
        assert by_id[commodity_buyer.id].rank_reason == "buys_this_commodity"
        assert by_id[engagement_buyer.id].rank_reason == "engagement"

    def test_dnc_and_unreachable_buyers_filtered(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User
    ):
        from app.models.crm import CustomerSite, SiteContact

        # A reachable commodity buyer (kept).
        good = _reachable_card(db_session, "Good Buyer", engagement=50.0)
        good.commodity_tags = [_CAP]

        # Unreachable: card with NO VendorContact email at all.
        unreachable = VendorCard(normalized_name="no contact", display_name="No Contact", emails=[])
        unreachable.commodity_tags = [_CAP]
        db_session.add(unreachable)

        # DNC: reachable contact, but its email is flagged do_not_contact on a SiteContact
        # (the same join _dnc_emails_for_cards uses).
        dnc = _reachable_card(db_session, "DNC Buyer", engagement=50.0)
        dnc.commodity_tags = [_CAP]
        site = CustomerSite(company_id=excess_list.company_id, site_name="DNC Site")
        db_session.add(site)
        db_session.flush()
        db_session.add(
            SiteContact(
                customer_site_id=site.id, full_name="DNC Person", email="sales@dncbuyer.com", do_not_contact=True
            )
        )
        db_session.commit()

        ranked = svc.rank_buyers_for(db_session, excess_list_id=excess_list.id)
        ids = {r.vendor_card_id for r in ranked}
        assert good.id in ids
        assert unreachable.id not in ids
        assert dnc.id not in ids

    def test_blacklisted_buyer_filtered(self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem):
        bad = _reachable_card(db_session, "Blacklisted Buyer", engagement=80.0)
        bad.commodity_tags = [_CAP]
        bad.is_blacklisted = True
        db_session.commit()
        ranked = svc.rank_buyers_for(db_session, excess_list_id=excess_list.id)
        assert bad.id not in {r.vendor_card_id for r in ranked}

    def test_ranked_buyer_carries_scorecard_facts(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User
    ):
        buyer = _reachable_card(db_session, "Fact Buyer", engagement=40.0)
        _won_offer_for(
            db_session, excess_list=excess_list, buyer=buyer, owner=trader, line=cap_line, unit_price=Decimal("0.90")
        )
        db_session.commit()
        svc.recompute_buyer_score(db_session, buyer.id)

        ranked = svc.rank_buyers_for(db_session, excess_list_id=excess_list.id)
        row = next(r for r in ranked if r.vendor_card_id == buyer.id)
        assert row.display_name == "Fact Buyer"
        assert row.win_rate is not None
        assert row.last_bid == Decimal("0.90")

    def test_line_item_ids_scope(self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem):
        buyer = _reachable_card(db_session, "Scoped Buyer", engagement=30.0)
        buyer.commodity_tags = [_CAP]
        db_session.commit()
        ranked = svc.rank_buyers_for(db_session, line_item_ids=[cap_line.id])
        assert buyer.id in {r.vendor_card_id for r in ranked}

    def test_requires_a_target(self, db_session: Session):
        with pytest.raises(ValueError):
            svc.rank_buyers_for(db_session)


# ═══════════════════════════════════════════════════════════════════════
#  recompute_buyer_score
# ═══════════════════════════════════════════════════════════════════════


class TestRecomputeBuyerScore:
    def test_rollup_math_and_upsert(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User
    ):
        buyer = _reachable_card(db_session, "Roll Buyer")
        # Two offers, one WON: bids 0.80 and 0.90 against asking 1.00 → avg 85%.
        won = _won_offer_for(
            db_session, excess_list=excess_list, buyer=buyer, owner=trader, line=cap_line, unit_price=Decimal("0.80")
        )
        _ = won
        lost = ExcessOffer(
            excess_list_id=excess_list.id,
            submitted_by=trader.id,
            offerer_vendor_card_id=buyer.id,
            scope="per_line",
            status=ExcessOfferStatus.LOST,
        )
        db_session.add(lost)
        db_session.flush()
        db_session.add(
            ExcessOfferLine(
                offer_id=lost.id,
                excess_line_item_id=cap_line.id,
                mpn_raw=cap_line.part_number,
                quantity=10,
                unit_price=Decimal("0.90"),
                match_status=OfferLineMatchStatus.MATCHED,
            )
        )
        # Outreach: 2 sent, 1 responded → response_rate 0.50.
        now = datetime.now(timezone.utc)
        db_session.add(
            ExcessOutreach(
                excess_list_id=excess_list.id,
                target_vendor_card_id=buyer.id,
                submitted_by=trader.id,
                channel="email",
                status=ExcessOutreachStatus.SENT,
                sent_at=now - timedelta(days=2),
            )
        )
        db_session.add(
            ExcessOutreach(
                excess_list_id=excess_list.id,
                target_vendor_card_id=buyer.id,
                submitted_by=trader.id,
                channel="email",
                status=ExcessOutreachStatus.RESPONDED,
                sent_at=now - timedelta(days=1),
            )
        )
        db_session.commit()

        score = svc.recompute_buyer_score(db_session, buyer.id)

        assert score.offers_received == 2
        assert score.wins == 1
        # avg of 80% and 90% of ask.
        assert score.avg_bid_pct_of_ask == Decimal("85.00")
        assert score.response_rate == Decimal("0.50")
        assert score.last_offered_at is not None
        # commodity_affinity counts the capacitor buys.
        assert score.commodity_affinity.get(_CAP, 0) >= 1

        # Upsert: a second call updates the SAME row, never duplicates.
        again = svc.recompute_buyer_score(db_session, buyer.id)
        assert again.id == score.id
        assert db_session.query(BuyerScore).filter_by(vendor_card_id=buyer.id).count() == 1

    def test_no_history_zero_rollup(self, db_session: Session):
        buyer = _reachable_card(db_session, "Empty Buyer")
        db_session.commit()
        score = svc.recompute_buyer_score(db_session, buyer.id)
        assert score.offers_received == 0
        assert score.wins == 0
        assert score.response_rate is None or score.response_rate == Decimal("0.00")


# ═══════════════════════════════════════════════════════════════════════
#  overlap_warning
# ═══════════════════════════════════════════════════════════════════════


class TestOverlapWarning:
    def test_teammate_offered_returns_advisory(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User, teammate: User
    ):
        buyer = _reachable_card(db_session, "Overlap Buyer")
        db_session.add(
            ExcessOutreach(
                excess_list_id=excess_list.id,
                excess_line_item_id=cap_line.id,
                target_vendor_card_id=buyer.id,
                submitted_by=teammate.id,  # a DIFFERENT user
                channel="email",
                status=ExcessOutreachStatus.SENT,
                sent_at=datetime.now(timezone.utc) - timedelta(days=3),
            )
        )
        db_session.commit()

        warn = svc.overlap_warning(
            db_session, excess_list_id=excess_list.id, target_vendor_card_id=buyer.id, owner_id=trader.id
        )
        assert warn is not None
        assert warn["by_user_id"] == teammate.id
        assert warn["by_user_name"] == teammate.name
        assert cap_line.id in warn["line_item_ids"]

    def test_same_owner_no_warning(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User
    ):
        buyer = _reachable_card(db_session, "Self Buyer")
        db_session.add(
            ExcessOutreach(
                excess_list_id=excess_list.id,
                target_vendor_card_id=buyer.id,
                submitted_by=trader.id,  # same owner → not overlap
                channel="phone",
                status=ExcessOutreachStatus.SENT,
                sent_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()
        warn = svc.overlap_warning(
            db_session, excess_list_id=excess_list.id, target_vendor_card_id=buyer.id, owner_id=trader.id
        )
        assert warn is None

    def test_stale_touch_outside_window_no_warning(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User, teammate: User
    ):
        buyer = _reachable_card(db_session, "Stale Buyer")
        db_session.add(
            ExcessOutreach(
                excess_list_id=excess_list.id,
                target_vendor_card_id=buyer.id,
                submitted_by=teammate.id,
                channel="email",
                status=ExcessOutreachStatus.SENT,
                sent_at=datetime.now(timezone.utc) - timedelta(days=90),
            )
        )
        db_session.commit()
        warn = svc.overlap_warning(
            db_session,
            excess_list_id=excess_list.id,
            target_vendor_card_id=buyer.id,
            owner_id=trader.id,
            within_days=14,
        )
        assert warn is None

    def test_no_teammate_touch_no_warning_and_never_raises(
        self, db_session: Session, excess_list: ExcessList, trader: User
    ):
        buyer = _reachable_card(db_session, "Quiet Buyer")
        db_session.commit()
        # No outreach at all — must return None, never raise / block.
        warn = svc.overlap_warning(
            db_session, excess_list_id=excess_list.id, target_vendor_card_id=buyer.id, owner_id=trader.id
        )
        assert warn is None


# ═══════════════════════════════════════════════════════════════════════
#  not_yet_offered_strip
# ═══════════════════════════════════════════════════════════════════════


class TestNotYetOfferedStrip:
    def test_returns_historical_buyer_absent_from_this_list(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User
    ):
        # A buyer who historically bought capacitors (won offer) but has NO outreach
        # row on THIS list → should be nudged.
        historical = _reachable_card(db_session, "Historical Buyer", engagement=60.0)
        prior_list = ExcessList(company_id=excess_list.company_id, owner_id=trader.id, title="Prior")
        db_session.add(prior_list)
        db_session.flush()
        prior_mc = _material_card(db_session, "GRM21B", _CAP)
        prior_line = ExcessLineItem(
            excess_list_id=prior_list.id,
            part_number="GRM21B",
            quantity=10,
            material_card_id=prior_mc.id,
            asking_price=Decimal("1.00"),
        )
        db_session.add(prior_line)
        db_session.flush()
        _won_offer_for(
            db_session,
            excess_list=prior_list,
            buyer=historical,
            owner=trader,
            line=prior_line,
            unit_price=Decimal("0.95"),
        )

        # A buyer already offered THIS list's lines → must NOT appear in the nudge.
        already = _reachable_card(db_session, "Already Buyer", engagement=60.0)
        already.commodity_tags = [_CAP]
        db_session.add(
            ExcessOutreach(
                excess_list_id=excess_list.id,
                target_vendor_card_id=already.id,
                submitted_by=trader.id,
                channel="email",
                status=ExcessOutreachStatus.SENT,
                sent_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()

        strip = svc.not_yet_offered_strip(db_session, excess_list_id=excess_list.id)
        ids = {r.vendor_card_id for r in strip}
        assert historical.id in ids
        assert already.id not in ids


# ═══════════════════════════════════════════════════════════════════════
#  Item-0 bounds (Chunk D carry-over)
# ═══════════════════════════════════════════════════════════════════════


class TestItem0Bounds:
    def test_overlap_warning_tolerates_null_timestamps(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User, teammate: User
    ):
        """A teammate touch with BOTH sent_at and created_at NULL is skipped, not
        raised.

        The advisory overlap check must never blow up the offer panel on a degenerate
        row (Item-0 defensive guard).
        """
        buyer = _reachable_card(db_session, "Null Stamp Buyer")
        row = ExcessOutreach(
            excess_list_id=excess_list.id,
            target_vendor_card_id=buyer.id,
            submitted_by=teammate.id,
            channel="phone",
            status=ExcessOutreachStatus.SENT,
        )
        db_session.add(row)
        db_session.flush()
        # Force both timestamps NULL (server_default would otherwise stamp created_at).
        row.created_at = None
        row.sent_at = None
        db_session.flush()

        # Must return None (no usable recent touch) rather than raise.
        result = svc.overlap_warning(
            db_session, excess_list_id=excess_list.id, target_vendor_card_id=buyer.id, owner_id=trader.id
        )
        assert result is None

    def test_last_bid_populated_via_batch(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User
    ):
        """rank_buyers_for still fills last_bid (the batched lookup replaces the
        N+1)."""
        buyer = _reachable_card(db_session, "Bid Buyer", engagement=10.0)
        _won_offer_for(
            db_session,
            excess_list=excess_list,
            buyer=buyer,
            owner=trader,
            line=cap_line,
            unit_price=Decimal("0.77"),
        )
        db_session.commit()

        ranked = svc.rank_buyers_for(db_session, excess_list_id=excess_list.id)
        by_id = {r.vendor_card_id: r for r in ranked}
        assert by_id[buyer.id].last_bid == Decimal("0.77")

    def test_limit_bounds_returned_rows(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, trader: User
    ):
        """The limit caps the returned set (query-level candidate bounding kept
        identical)."""
        for i in range(5):
            b = _reachable_card(db_session, f"Cap Buyer {i}", engagement=float(i))
            b.commodity_tags = [_CAP]
        db_session.commit()
        ranked = svc.rank_buyers_for(db_session, excess_list_id=excess_list.id, limit=2)
        assert len(ranked) == 2
