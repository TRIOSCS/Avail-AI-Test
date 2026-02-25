"""Tests for the proactive matching engine (Phase 2.2)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import (
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    ProactiveMatch,
    Requirement,
    Requisition,
    Sighting,
    User,
)
from app.models.purchase_history import CustomerPartHistory
from app.services.proactive_matching import (
    compute_match_score,
    dismiss_match,
    expire_old_matches,
    find_matches_for_offer,
    find_matches_for_sighting,
    mark_match_sent,
    run_proactive_scan,
)


def _setup_scenario(db):
    """Create a common test scenario: company + site + owner + card + CPH + requisition."""
    owner = User(
        email="sales@trioscs.com", name="Sales Rep", role="sales",
        azure_id="sales-001", created_at=datetime.now(timezone.utc),
    )
    db.add(owner)
    db.flush()

    company = Company(
        name="Sensata Technologies", is_active=True, account_owner_id=owner.id,
    )
    db.add(company)
    db.flush()

    site = CustomerSite(
        company_id=company.id, site_name="Sensata HQ", is_active=True,
    )
    db.add(site)
    db.flush()

    card = MaterialCard(normalized_mpn="stm32f407", display_mpn="STM32F407", search_count=5)
    db.add(card)
    db.flush()

    # Create a requisition + requirement so the match has valid FKs
    req = Requisition(
        name="Test Req", customer_site_id=site.id, status="archived", created_by=owner.id,
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id, primary_mpn="STM32F407",
        normalized_mpn="stm32f407", material_card_id=card.id,
    )
    db.add(requirement)
    db.flush()

    # Purchase history
    cph = CustomerPartHistory(
        company_id=company.id, material_card_id=card.id, mpn="STM32F407",
        source="avail_offer", purchase_count=3,
        last_purchased_at=datetime.now(timezone.utc) - timedelta(days=60),
        avg_unit_price=Decimal("12.50"), last_unit_price=Decimal("13.00"),
        total_quantity=500,
    )
    db.add(cph)
    db.commit()

    return {
        "owner": owner, "company": company, "site": site, "card": card,
        "requisition": req, "requirement": requirement, "cph": cph,
    }


# ── Scoring tests ────────────────────────────────────────────────────────


def test_score_recent_frequent_high_margin():
    """Recent purchase + high frequency + good margin = high score."""
    score, margin = compute_match_score(
        last_purchased_at=datetime.now(timezone.utc) - timedelta(days=30),
        purchase_count=5,
        customer_avg_price=20.0,
        our_cost=10.0,
    )
    assert score >= 90
    assert margin == 50.0


def test_score_old_single_no_margin():
    """Old purchase + single buy + no margin info = low score."""
    score, margin = compute_match_score(
        last_purchased_at=datetime.now(timezone.utc) - timedelta(days=800),
        purchase_count=1,
        customer_avg_price=None,
        our_cost=None,
    )
    assert score <= 50
    assert margin is None


def test_score_no_purchase_date():
    """No purchase date + good frequency + good margin = medium score."""
    score, margin = compute_match_score(
        last_purchased_at=None,
        purchase_count=4,
        customer_avg_price=15.0,
        our_cost=10.0,
    )
    assert 40 < score < 80
    assert margin is not None


def test_score_negative_margin():
    """Negative margin scenario — score should be low."""
    score, margin = compute_match_score(
        last_purchased_at=datetime.now(timezone.utc) - timedelta(days=30),
        purchase_count=3,
        customer_avg_price=5.0,
        our_cost=10.0,
    )
    # Margin is negative
    assert margin < 0
    assert score < 70


# ── find_matches_for_offer ───────────────────────────────────────────────


def test_find_matches_for_offer(db_session):
    """Offer with CPH data creates a scored ProactiveMatch."""
    data = _setup_scenario(db_session)

    # Create an offer for the same part
    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Arrow Electronics",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        qty_available=200,
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    matches = find_matches_for_offer(offer.id, db_session)
    db_session.commit()

    assert len(matches) == 1
    m = matches[0]
    assert m.company_id == data["company"].id
    assert m.material_card_id == data["card"].id
    assert m.match_score > 0
    assert m.customer_purchase_count == 3
    assert m.our_cost == 8.0
    assert m.margin_pct is not None
    assert m.salesperson_id == data["owner"].id


def test_find_matches_no_cph(db_session):
    """Offer for a part with no CPH data returns no matches."""
    data = _setup_scenario(db_session)

    # Different card with no CPH
    card2 = MaterialCard(normalized_mpn="lm358n", display_mpn="LM358N", search_count=0)
    db_session.add(card2)
    db_session.flush()

    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=card2.id,
        vendor_name="Mouser",
        mpn="LM358N",
        unit_price=Decimal("0.50"),
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 0


def test_find_matches_no_owner(db_session):
    """Company without account_owner_id is skipped."""
    data = _setup_scenario(db_session)
    data["company"].account_owner_id = None
    db_session.commit()

    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Arrow",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 0


def test_find_matches_dedup(db_session):
    """Duplicate matches for same card+company are not created."""
    data = _setup_scenario(db_session)

    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Arrow",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    # First call creates match
    matches1 = find_matches_for_offer(offer.id, db_session)
    db_session.commit()
    assert len(matches1) == 1

    # Second call should dedup
    matches2 = find_matches_for_offer(offer.id, db_session)
    db_session.commit()
    assert len(matches2) == 0


# ── find_matches_for_sighting ────────────────────────────────────────────


def test_find_matches_for_sighting(db_session):
    """Sighting with CPH data creates a scored match (needs existing offer for FK)."""
    data = _setup_scenario(db_session)

    # Sighting-triggered matches need a fallback offer_id (NOT NULL FK)
    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Arrow",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        status="active",
    )
    db_session.add(offer)
    db_session.flush()

    sighting = Sighting(
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="DigiKey",
        mpn_matched="STM32F407",
        unit_price=Decimal("9.00"),
        qty_available=100,
    )
    db_session.add(sighting)
    db_session.commit()

    matches = find_matches_for_sighting(sighting.id, db_session)
    db_session.commit()

    assert len(matches) == 1
    assert matches[0].our_cost == 9.0


# ── run_proactive_scan ───────────────────────────────────────────────────


def test_run_proactive_scan(db_session):
    """Batch scan picks up new offers and creates matches."""
    data = _setup_scenario(db_session)

    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Arrow",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    # Reset scan timestamp so it picks up the offer
    from app.services.proactive_matching import _last_scan_at
    import app.services.proactive_matching as pm
    pm._last_scan_at = datetime.now(timezone.utc) - timedelta(hours=1)

    result = run_proactive_scan(db_session)
    assert result["scanned_offers"] >= 1
    assert result["matches_created"] >= 1


# ── dismiss + mark_match_sent ────────────────────────────────────────────


def test_dismiss_match(db_session):
    """Dismissing a match sets status and reason."""
    data = _setup_scenario(db_session)

    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Arrow",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    matches = find_matches_for_offer(offer.id, db_session)
    db_session.commit()
    assert len(matches) == 1

    dismiss_match(matches[0].id, data["owner"].id, "Not buying this anymore", db_session)

    m = db_session.get(ProactiveMatch, matches[0].id)
    assert m.status == "dismissed"
    assert m.dismiss_reason == "Not buying this anymore"


def test_mark_match_sent(db_session):
    """Marking a match as sent updates status."""
    data = _setup_scenario(db_session)

    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Arrow",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    matches = find_matches_for_offer(offer.id, db_session)
    db_session.commit()

    mark_match_sent(matches[0].id, data["owner"].id, db_session)

    m = db_session.get(ProactiveMatch, matches[0].id)
    assert m.status == "sent"


# ── expire_old_matches ───────────────────────────────────────────────────


def test_expire_old_matches(db_session):
    """Old 'new' matches get expired."""
    data = _setup_scenario(db_session)

    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Arrow",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    matches = find_matches_for_offer(offer.id, db_session)
    db_session.commit()
    assert len(matches) == 1

    # Backdate the match
    matches[0].created_at = datetime.now(timezone.utc) - timedelta(days=60)
    db_session.commit()

    expired = expire_old_matches(db_session)
    assert expired == 1

    m = db_session.get(ProactiveMatch, matches[0].id)
    assert m.status == "expired"
