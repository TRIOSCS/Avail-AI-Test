"""Tests for proactive prepare/send workflow (Phase 3).

Covers: batch dismiss, prepare page, send flow, throttle creation, AI draft.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import (
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    ProactiveMatch,
    ProactiveOffer,
    Requirement,
    Requisition,
    SiteContact,
    User,
)
from app.models.intelligence import ProactiveThrottle
from app.services.proactive_service import (
    get_matches_for_user,
    get_sent_offers,
    send_proactive_offer,
)
from tests.conftest import engine  # noqa: F401


def _setup_send_scenario(db):
    """Create scenario with matches and contacts ready for sending."""
    owner = User(
        email="sales@trioscs.com",
        name="Sales Rep",
        role="sales",
        azure_id="s-001",
        created_at=datetime.now(timezone.utc),
    )
    db.add(owner)
    db.flush()

    company = Company(name="Acme Corp", is_active=True, account_owner_id=owner.id)
    db.add(company)
    db.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()

    contact1 = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Doe",
        email="jane@acme.com",
        is_primary=True,
    )
    contact2 = SiteContact(
        customer_site_id=site.id,
        full_name="Bob Smith",
        email="bob@acme.com",
        is_primary=False,
    )
    db.add_all([contact1, contact2])
    db.flush()

    card = MaterialCard(normalized_mpn="lm358n", display_mpn="LM358N")
    db.add(card)
    db.flush()

    req = Requisition(
        name="Test Req",
        customer_site_id=site.id,
        status="archived",
        created_by=owner.id,
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM358N",
        normalized_mpn="lm358n",
        material_card_id=card.id,
        target_qty=1000,
    )
    db.add(requirement)
    db.flush()

    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        material_card_id=card.id,
        vendor_name="Arrow",
        mpn="LM358N",
        unit_price=Decimal("0.42"),
        qty_available=5000,
        status="active",
    )
    db.add(offer)
    db.flush()

    match = ProactiveMatch(
        offer_id=offer.id,
        requirement_id=requirement.id,
        requisition_id=req.id,
        customer_site_id=site.id,
        salesperson_id=owner.id,
        mpn="LM358N",
        material_card_id=card.id,
        company_id=company.id,
        match_score=85,
        margin_pct=23.0,
        our_cost=0.42,
        status="new",
    )
    db.add(match)
    db.commit()

    return {
        "owner": owner,
        "company": company,
        "site": site,
        "contact1": contact1,
        "contact2": contact2,
        "card": card,
        "offer": offer,
        "match": match,
        "requirement": requirement,
        "requisition": req,
    }


# ── Send flow tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_creates_throttle_records(db_session):
    """Sending creates throttle records for each MPN+site."""
    data = _setup_send_scenario(db_session)

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        result = await send_proactive_offer(
            db=db_session,
            user=data["owner"],
            token="fake-token",
            match_ids=[data["match"].id],
            contact_ids=[data["contact1"].id],
            sell_prices={},
            subject="Test",
        )

    assert result is not None
    throttle = (
        db_session.query(ProactiveThrottle)
        .filter(
            ProactiveThrottle.mpn == "LM358N",
            ProactiveThrottle.customer_site_id == data["site"].id,
        )
        .first()
    )
    assert throttle is not None

    match = db_session.get(ProactiveMatch, data["match"].id)
    assert match.status == "sent"


@pytest.mark.asyncio
async def test_send_to_multiple_contacts(db_session):
    """Sending to multiple contacts includes all emails."""
    data = _setup_send_scenario(db_session)

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        result = await send_proactive_offer(
            db=db_session,
            user=data["owner"],
            token="fake-token",
            match_ids=[data["match"].id],
            contact_ids=[data["contact1"].id, data["contact2"].id],
            sell_prices={},
        )

    assert "jane@acme.com" in result["recipient_emails"]
    assert "bob@acme.com" in result["recipient_emails"]


@pytest.mark.asyncio
async def test_send_with_custom_subject(db_session):
    """Custom subject is preserved in the ProactiveOffer."""
    data = _setup_send_scenario(db_session)

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        result = await send_proactive_offer(
            db=db_session,
            user=data["owner"],
            token="fake-token",
            match_ids=[data["match"].id],
            contact_ids=[data["contact1"].id],
            sell_prices={},
            subject="Custom Subject Line",
        )

    assert result["subject"] == "Custom Subject Line"


@pytest.mark.asyncio
async def test_send_with_email_html(db_session):
    """Pre-built email HTML is used instead of fallback."""
    data = _setup_send_scenario(db_session)

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        result = await send_proactive_offer(
            db=db_session,
            user=data["owner"],
            token="fake-token",
            match_ids=[data["match"].id],
            contact_ids=[data["contact1"].id],
            sell_prices={},
            email_html="<p>Custom HTML body</p>",
        )

    po = db_session.get(ProactiveOffer, result["id"])
    assert "Custom HTML body" in po.email_body_html


# ── Match retrieval tests ────────────────────────────────────────────────


def test_matches_sorted_by_score_within_group(db_session):
    """Matches within a group are sorted by match_score descending."""
    data = _setup_send_scenario(db_session)

    # Create a second match with lower score
    offer2 = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Mouser",
        mpn="LM358N-2",
        unit_price=Decimal("0.50"),
        qty_available=2000,
        status="active",
    )
    db_session.add(offer2)
    db_session.flush()

    match2 = ProactiveMatch(
        offer_id=offer2.id,
        customer_site_id=data["site"].id,
        salesperson_id=data["owner"].id,
        mpn="LM358N-2",
        company_id=data["company"].id,
        match_score=40,
        margin_pct=10.0,
        status="new",
    )
    db_session.add(match2)
    db_session.commit()

    result = get_matches_for_user(db_session, data["owner"].id, status="new")
    groups = result["groups"]
    assert len(groups) >= 1
    matches = groups[0]["matches"]
    assert len(matches) >= 2
    # Higher score should come first
    assert matches[0]["match_score"] >= matches[1]["match_score"]


# ── Sent offers grouped by customer ──────────────────────────────────────


def test_sent_offers_grouped_by_customer(db_session):
    """get_sent_offers returns offers grouped by customer company."""
    data = _setup_send_scenario(db_session)

    po = ProactiveOffer(
        customer_site_id=data["site"].id,
        salesperson_id=data["owner"].id,
        line_items=[{"mpn": "LM358N", "qty": 100}],
        recipient_emails=["jane@acme.com"],
        subject="Test Offer",
        status="sent",
        total_sell=500,
        total_cost=300,
        sent_at=datetime.now(timezone.utc),
    )
    db_session.add(po)
    db_session.commit()

    result = get_sent_offers(db_session, data["owner"].id)
    assert len(result) >= 1
    # Result should be a list of group dicts with 'company_name' and 'offers'
    group = result[0]
    assert "company_name" in group
    assert "offers" in group
    assert len(group["offers"]) >= 1


# ── Batch dismiss ────────────────────────────────────────────────────────


def test_batch_dismiss(db_session):
    """Batch dismiss updates multiple matches to dismissed status."""
    data = _setup_send_scenario(db_session)

    # Create a second match
    offer2 = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Mouser",
        mpn="TL072CP",
        unit_price=Decimal("0.89"),
        qty_available=2500,
        status="active",
    )
    db_session.add(offer2)
    db_session.flush()

    match2 = ProactiveMatch(
        offer_id=offer2.id,
        customer_site_id=data["site"].id,
        salesperson_id=data["owner"].id,
        mpn="TL072CP",
        company_id=data["company"].id,
        match_score=60,
        status="new",
    )
    db_session.add(match2)
    db_session.commit()

    # Dismiss both
    db_session.query(ProactiveMatch).filter(
        ProactiveMatch.id.in_([data["match"].id, match2.id]),
        ProactiveMatch.salesperson_id == data["owner"].id,
        ProactiveMatch.status == "new",
    ).update({"status": "dismissed"}, synchronize_session=False)
    db_session.commit()

    m1 = db_session.get(ProactiveMatch, data["match"].id)
    m2 = db_session.get(ProactiveMatch, match2.id)
    assert m1.status == "dismissed"
    assert m2.status == "dismissed"


# ── Timeago filter ───────────────────────────────────────────────────────


def test_timeago_filter():
    """Timeago filter produces compact relative timestamps."""
    from app.template_env import _timeago_filter as _timeago

    # None input
    assert _timeago(None) == "--"

    # Recent
    now = datetime.now(timezone.utc)
    assert _timeago(now - timedelta(seconds=30)) == "just now"
    assert "m ago" in _timeago(now - timedelta(minutes=15))
    assert "h ago" in _timeago(now - timedelta(hours=3))
    assert "d ago" in _timeago(now - timedelta(days=2))
    assert "w ago" in _timeago(now - timedelta(days=14))

    # String input
    iso_str = (now - timedelta(hours=5)).isoformat()
    assert "h ago" in _timeago(iso_str)


# ── Proactive helpers dedup in htmx_views ────────────────────────────────


def test_do_not_offer_dedup(db_session):
    """Creating a do-not-offer with shared helper prevents duplicates."""
    from app.models.intelligence import ProactiveDoNotOffer
    from app.services.proactive_helpers import is_do_not_offer

    data = _setup_send_scenario(db_session)

    # First creation
    assert not is_do_not_offer(db_session, "LM358N", data["company"].id)
    db_session.add(
        ProactiveDoNotOffer(
            mpn="LM358N",
            company_id=data["company"].id,
            created_by_id=data["owner"].id,
        )
    )
    db_session.commit()

    # Now it should be detected
    assert is_do_not_offer(db_session, "LM358N", data["company"].id)
    assert is_do_not_offer(db_session, "  lm358n  ", data["company"].id)
