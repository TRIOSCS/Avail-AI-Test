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
    PartEquivalence,
    ProactiveMatch,
    Requirement,
    Requisition,
    User,
)
from app.models.purchase_history import CustomerPartHistory
from app.services.part_equivalence import norm_key
from app.services.proactive_matching import find_matches_for_offer
from app.services.proactive_service import get_matches_for_user
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

    Since the 2026-08-06 rework CPH no longer seeds matches — the hotlist pass owns this
    customer's only demand record (the HOTLIST requisition), and the single active-
    match-per-(part, company) slot holds across both passes.
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
    # The hotlist pass owns the slot — purchase history is context, not a seed.
    assert rows[0].match_source == "hotlist"


def test_ai_variant_hotlist_match_displays_customer_spelling_with_amber_flag(db_session):
    """An AI-verdict variant offer against a hotlisted ask stores the CUSTOMER spelling,
    so the Matches-tab rollup pools the variant supply under it and sets has_ai_variants
    — the amber 'AI match — verify' chip's data source."""
    db = db_session
    d = _setup(db, mpn="GSOT36C")  # hotlist ask under the customer spelling
    ka, kb = sorted([norm_key("GSOT36C"), norm_key("GSOT36C-E3-08")])
    db.add(
        PartEquivalence(
            key_a=ka,
            key_b=kb,
            example_a="GSOT36C",
            example_b="GSOT36C-E3-08",
            verdict="same",
            source="ai",
            reason="pkg suffix",
        )
    )
    variant_offer = Offer(
        vendor_name="Sierra",
        mpn="GSOT36C-E3-08",
        qty_available=177_630,
        unit_price=Decimal("0.05"),
        status="active",
    )
    db.add(variant_offer)
    db.commit()

    matches = find_matches_for_offer(variant_offer.id, db)
    db.commit()
    hot = [m for m in matches if m.company_id == d["company"].id]
    assert len(hot) == 1
    assert hot[0].mpn == "GSOT36C"  # customer's own spelling wins the display

    result = get_matches_for_user(db, d["owner"].id)
    rows = [m for g in result["groups"] for m in g["matches"]]
    row = next(r for r in rows if r["mpn"] == "GSOT36C")
    assert row["has_ai_variants"] is True
    assert "GSOT36C-E3-08" in row["variants"]
