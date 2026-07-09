"""Hotlist reqs seed Proactive matches even with no purchase history.

A HOTLIST requisition is an explicit salesperson request to monitor a part for a
customer. When a matching offer lands, the Proactive matcher must surface a match
EVEN WITH NO CustomerPartHistory (CPH) — the CPH path returns [] in that case.

Called by: pytest.
Depends on: app.services.proactive_matching, models, conftest db_session fixture.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.constants import ProactiveMatchStatus, RequisitionStatus
from app.models import (
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    ProactiveMatch,
    Requirement,
    Requisition,
    User,
)
from app.models.purchase_history import CustomerPartHistory
from app.services.proactive_matching import find_matches_for_offer
from tests.conftest import engine  # noqa: F401


def _setup(db, *, mpn="ABC123"):
    """Hotlist scenario: owner + company + active site + HOTLIST req + requirement + offer.

    Mirrors the NOT-NULL columns the real models require (User.azure_id/role,
    Company.is_active, CustomerSite.site_name, MaterialCard). The card/offer/requirement
    all share the freshly-created card's id, so each scenario is independent.
    """
    owner = User(
        email=f"owner-{mpn}@trioscs.com",
        name="Account Owner",
        role="sales",
        azure_id=f"owner-{mpn}",
        created_at=datetime.now(UTC),
    )
    db.add(owner)
    db.flush()

    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, search_count=1)
    db.add(card)
    db.flush()

    co = Company(name="Acme", is_active=True, account_owner_id=owner.id)
    db.add(co)
    db.flush()

    site = CustomerSite(company_id=co.id, site_name="Acme HQ", is_active=True)
    db.add(site)
    db.flush()

    req = Requisition(
        name="watch",
        status=RequisitionStatus.HOTLIST.value,
        customer_site_id=site.id,
        company_id=co.id,
        created_by=owner.id,
    )
    db.add(req)
    db.flush()

    db.add(Requirement(requisition_id=req.id, material_card_id=card.id, primary_mpn=mpn))
    offer = Offer(
        material_card_id=card.id,
        vendor_name="Arrow",
        mpn=mpn,
        unit_price=Decimal("10"),
        status="active",
    )
    db.add(offer)
    db.commit()
    return {"owner": owner, "company": co, "site": site, "req": req, "offer": offer, "card": card}


def test_hotlist_seeds_match_without_cph(db_session):
    """An offer matching a hotlisted part surfaces a match with no purchase history."""
    db = db_session
    d = _setup(db, mpn="ABC123")
    co, req, offer = d["company"], d["req"], d["offer"]

    # No CPH exists for this card.
    assert db.query(CustomerPartHistory).filter_by(material_card_id=d["card"].id).count() == 0

    matches = find_matches_for_offer(offer.id, db)
    assert any(m.requisition_id == req.id and m.company_id == co.id for m in matches)

    db.commit()
    rows = db.query(ProactiveMatch).filter_by(requisition_id=req.id).all()
    assert len(rows) == 1
    m = rows[0]
    assert m.material_card_id == d["card"].id
    assert m.customer_site_id == d["site"].id
    assert m.salesperson_id == d["owner"].id
    assert m.requirement_id is None  # hotlist matches carry no requirement
    assert m.customer_purchase_count == 0
    assert m.match_score == 60  # baseline


def test_hotlist_match_surfaces_status_new(db_session):
    """A hotlist-seeded match defaults to status NEW so it surfaces in the tab."""
    db = db_session
    d = _setup(db, mpn="DEF456")
    find_matches_for_offer(d["offer"].id, db)
    db.commit()
    m = db.query(ProactiveMatch).filter_by(requisition_id=d["req"].id).first()
    assert m is not None
    assert m.status == ProactiveMatchStatus.NEW


def test_hotlist_and_cph_dedup_one_match(db_session):
    """A company with BOTH a hotlist req AND CPH history for the part gets ONE match.

    The CPH pass produces the match first; the hotlist pass must skip the company
    (uncommitted CPH adds are invisible to its DB query, so dedup rides on the
    skip_company_ids set passed in from find_matches_for_offer).
    """
    db = db_session
    d = _setup(db, mpn="GHI789")
    co, card, offer = d["company"], d["card"], d["offer"]

    # Same company also has purchase history for this part.
    db.add(
        CustomerPartHistory(
            company_id=co.id,
            material_card_id=card.id,
            mpn="GHI789",
            source="avail_offer",
            purchase_count=2,
            last_purchased_at=datetime.now(UTC) - timedelta(days=30),
            avg_unit_price=Decimal("20.00"),
            last_unit_price=Decimal("21.00"),
            total_quantity=100,
        )
    )
    db.commit()

    matches = find_matches_for_offer(offer.id, db)
    db.commit()

    # Exactly one active match for this (card, company) across both passes.
    rows = (
        db.query(ProactiveMatch)
        .filter(
            ProactiveMatch.material_card_id == card.id,
            ProactiveMatch.company_id == co.id,
        )
        .all()
    )
    assert len(rows) == 1
    assert len([m for m in matches if m.company_id == co.id]) == 1
    # CPH wins the single slot (purchase history → its requirement-aware path).
    assert rows[0].customer_purchase_count == 2
