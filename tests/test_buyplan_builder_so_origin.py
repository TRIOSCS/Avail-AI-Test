"""test_buyplan_builder_so_origin.py — SO-origin buy plan builder tests.

Covers create_sales_order_from_offers: originate a DRAFT buy plan directly from
chosen RFQ offers (quote_id=None) and block a duplicate open Sales Order for the
same requisition. Exercises the shared _assemble_buy_plan core extracted from
build_buy_plan.

Called by: pytest
Depends on: conftest db_session fixture, app/services/buyplan_builder.py
"""

from datetime import datetime, timezone

import pytest

from app.constants import BuyPlanStatus
from app.models import Company, CustomerSite, Offer, Requirement, Requisition, User, VendorCard


@pytest.fixture
def so_origin_fixture(db_session):
    """Build a requisition + one requirement with one scored active offer (SO path).

    Returns ``(requisition, selections, sell_prices, buyer)`` where ``selections`` maps
    ``requirement_id -> chosen offer_id`` and ``sell_prices`` maps
    ``requirement_id -> sell price``, mirroring the seed shape used by the other
    ``tests/test_buyplan_builder_*`` factories.
    """
    user = User(
        email="so-origin@trioscs.com",
        name="SO Origin Buyer",
        role="buyer",
        azure_id="az-so-origin",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.flush()

    company = Company(name="SO Origin Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        country="US",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="REQ-SO-ORIGIN",
        status="open",
        created_by=user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="SO-MPN-1",
        target_qty=100,
        target_price=1.0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    vendor = VendorCard(
        normalized_name="so vendor",
        display_name="SO Vendor",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vendor)
    db_session.flush()

    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_card_id=vendor.id,
        vendor_name="SO Vendor",
        mpn="SO-MPN-1",
        qty_available=100,
        unit_price=0.50,
        status="active",
        entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()

    selections = {requirement.id: offer.id}
    sell_prices = {requirement.id: 1.25}
    return req, selections, sell_prices, user


def test_create_sales_order_from_offers_makes_draft_without_quote(db_session, so_origin_fixture):
    from app.services.buyplan_builder import create_sales_order_from_offers

    req, selections, sell_prices, user = so_origin_fixture
    plan = create_sales_order_from_offers(req.id, selections, sell_prices, db_session, user)
    assert plan.id is not None
    assert plan.quote_id is None
    assert plan.requisition_id == req.id
    assert plan.status == BuyPlanStatus.DRAFT.value
    assert len(plan.lines) == len(selections)


def test_duplicate_so_for_requisition_is_blocked(db_session, so_origin_fixture):
    from app.services.buyplan_builder import create_sales_order_from_offers

    req, selections, sell_prices, user = so_origin_fixture
    create_sales_order_from_offers(req.id, selections, sell_prices, db_session, user)
    with pytest.raises(ValueError, match="already an open"):
        create_sales_order_from_offers(req.id, selections, sell_prices, db_session, user)
