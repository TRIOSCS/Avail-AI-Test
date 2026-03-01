"""Tests for the proactive matching engine (Phase 2.2)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

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
from app.models.intelligence import ProactiveDoNotOffer, ProactiveThrottle
from app.models.purchase_history import CustomerPartHistory
from app.services.proactive_matching import (
    _score_frequency,
    _score_margin,
    _score_recency,
    compute_match_score,
    dismiss_match,
    expire_old_matches,
    find_matches_for_offer,
    find_matches_for_sighting,
    mark_match_sent,
    run_proactive_scan,
)
from tests.conftest import engine  # noqa: F401


def _setup_scenario(db):
    """Create a common test scenario: company + site + owner + card + CPH + requisition."""
    owner = User(
        email="sales@trioscs.com",
        name="Sales Rep",
        role="sales",
        azure_id="sales-001",
        created_at=datetime.now(timezone.utc),
    )
    db.add(owner)
    db.flush()

    company = Company(
        name="Sensata Technologies",
        is_active=True,
        account_owner_id=owner.id,
    )
    db.add(company)
    db.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="Sensata HQ",
        is_active=True,
    )
    db.add(site)
    db.flush()

    card = MaterialCard(normalized_mpn="stm32f407", display_mpn="STM32F407", search_count=5)
    db.add(card)
    db.flush()

    # Create a requisition + requirement so the match has valid FKs
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
        primary_mpn="STM32F407",
        normalized_mpn="stm32f407",
        material_card_id=card.id,
    )
    db.add(requirement)
    db.flush()

    # Purchase history
    cph = CustomerPartHistory(
        company_id=company.id,
        material_card_id=card.id,
        mpn="STM32F407",
        source="avail_offer",
        purchase_count=3,
        last_purchased_at=datetime.now(timezone.utc) - timedelta(days=60),
        avg_unit_price=Decimal("12.50"),
        last_unit_price=Decimal("13.00"),
        total_quantity=500,
    )
    db.add(cph)
    db.commit()

    return {
        "owner": owner,
        "company": company,
        "site": site,
        "card": card,
        "requisition": req,
        "requirement": requirement,
        "cph": cph,
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


# ── Additional scoring branch tests ──────────────────────────────────────


def test_score_recency_365_days():
    """Purchase 200-365 days ago returns 80."""
    assert _score_recency(datetime.now(timezone.utc) - timedelta(days=250)) == 80


def test_score_recency_730_days():
    """Purchase 366-730 days ago returns 60."""
    assert _score_recency(datetime.now(timezone.utc) - timedelta(days=500)) == 60


def test_score_frequency_two_purchases():
    """Exactly 2 purchases returns 60."""
    assert _score_frequency(2) == 60


def test_score_margin_10_to_20_pct():
    """Margin between 10% and 20% returns score=60."""
    # customer_avg_price=12.5, our_cost=10 → margin = 20% exactly → score 80
    # customer_avg_price=11.5, our_cost=10 → margin = 13.04% → score 60
    score, margin = _score_margin(11.5, 10.0)
    assert score == 60
    assert margin is not None
    assert 10 <= margin < 20


def test_score_margin_0_to_10_pct():
    """Margin between 0% and 10% returns score=40."""
    # customer_avg_price=10.5, our_cost=10 → margin = 4.76% → score 40
    score, margin = _score_margin(10.5, 10.0)
    assert score == 40
    assert margin is not None
    assert 0 < margin < 10


# ── offer not found or no material_card_id (line 100) ────────────────────


def test_find_matches_offer_not_found(db_session):
    """Non-existent offer_id returns [] early (line 100)."""
    _setup_scenario(db_session)
    matches = find_matches_for_offer(99999, db_session)
    assert matches == []


def test_find_matches_offer_no_material_card(db_session):
    """Offer with no material_card_id returns [] early (line 100)."""
    data = _setup_scenario(db_session)

    offer = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=None,
        vendor_name="Arrow",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    matches = find_matches_for_offer(offer.id, db_session)
    assert matches == []


# ── sighting with no material_card_id (line 114) ─────────────────────────


def test_find_matches_for_sighting_no_card(db_session):
    """Sighting with no material_card_id returns [] early."""
    data = _setup_scenario(db_session)

    sighting = Sighting(
        requirement_id=data["requirement"].id,
        material_card_id=None,
        vendor_name="DigiKey",
        mpn_matched="STM32F407",
        unit_price=Decimal("9.00"),
    )
    db_session.add(sighting)
    db_session.commit()

    matches = find_matches_for_sighting(sighting.id, db_session)
    assert matches == []


# ── no active site for company (line 162) ─────────────────────────────────


def test_find_matches_no_active_site(db_session):
    """Company with no active site is skipped."""
    data = _setup_scenario(db_session)

    # Deactivate the site
    data["site"].is_active = False
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


# ── do-not-offer suppression (line 174) ──────────────────────────────────


def test_find_matches_do_not_offer_suppression(db_session):
    """Company with do-not-offer record for the MPN is skipped."""
    data = _setup_scenario(db_session)

    # Add a do-not-offer suppression
    dno = ProactiveDoNotOffer(
        mpn="STM32F407",
        company_id=data["company"].id,
        created_by_id=data["owner"].id,
        reason="Customer said no",
    )
    db_session.add(dno)
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


# ── throttle check (line 187) ────────────────────────────────────────────


def test_find_matches_throttled(db_session):
    """Recently offered MPN to the same site is throttled (skipped)."""
    data = _setup_scenario(db_session)

    # Add a recent throttle record
    throttle = ProactiveThrottle(
        mpn="STM32F407",
        customer_site_id=data["site"].id,
        last_offered_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(throttle)
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


# ── margin below min_margin (line 200) ───────────────────────────────────


def test_find_matches_below_min_margin(db_session):
    """Match with margin below min_margin_pct is skipped."""
    data = _setup_scenario(db_session)

    # Set CPH avg_unit_price very close to our cost so margin is tiny
    data["cph"].avg_unit_price = Decimal("8.10")
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

    # margin = (8.10 - 8.00) / 8.10 * 100 = 1.23% < 5% (mock min_margin)
    with patch("app.services.proactive_matching.settings") as mock_settings:
        mock_settings.proactive_throttle_days = 30
        mock_settings.proactive_min_margin_pct = 5
        mock_settings.proactive_match_expiry_days = 30
        matches = find_matches_for_offer(offer.id, db_session)

    assert len(matches) == 0


# ── no requisition history (line 216) ────────────────────────────────────


def test_find_matches_no_requisition_history(db_session):
    """Company with CPH but no requisition for the part is skipped."""
    owner = User(
        email="sales2@trioscs.com",
        name="Sales2",
        role="sales",
        azure_id="sales-002",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(owner)
    db_session.flush()

    company = Company(
        name="NoReq Corp",
        is_active=True,
        account_owner_id=owner.id,
    )
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="NoReq HQ",
        is_active=True,
    )
    db_session.add(site)
    db_session.flush()

    card = MaterialCard(normalized_mpn="atmega328p", display_mpn="ATMEGA328P", search_count=1)
    db_session.add(card)
    db_session.flush()

    # CPH exists, but NO requisition/requirement for this card+site
    cph = CustomerPartHistory(
        company_id=company.id,
        material_card_id=card.id,
        mpn="ATMEGA328P",
        source="avail_offer",
        purchase_count=3,
        last_purchased_at=datetime.now(timezone.utc) - timedelta(days=30),
        avg_unit_price=Decimal("5.00"),
        last_unit_price=Decimal("5.50"),
        total_quantity=100,
    )
    db_session.add(cph)
    db_session.flush()

    # Need a requisition for the Offer FK, but on a DIFFERENT site/card
    other_site = CustomerSite(
        company_id=company.id,
        site_name="Other Site",
        is_active=True,
    )
    db_session.add(other_site)
    db_session.flush()

    req = Requisition(
        name="Other Req",
        customer_site_id=other_site.id,
        status="archived",
        created_by=owner.id,
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="ATMEGA328P",
        normalized_mpn="atmega328p",
        material_card_id=card.id,
    )
    db_session.add(requirement)
    db_session.flush()

    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        material_card_id=card.id,
        vendor_name="Arrow",
        mpn="ATMEGA328P",
        unit_price=Decimal("3.00"),
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    # The match logic queries Requisition.customer_site_id == site.id
    # (the first site). But the requisition is on other_site. So no match
    # for the CPH company because the requirement join won't match.
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 0


# ── sighting-triggered, no fallback offer (line 245) ─────────────────────


def test_find_matches_sighting_no_fallback_offer(db_session):
    """Sighting-triggered match without any existing offer for the part is skipped."""
    owner = User(
        email="sales3@trioscs.com",
        name="Sales3",
        role="sales",
        azure_id="sales-003",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(owner)
    db_session.flush()

    company = Company(
        name="NoOffer Corp",
        is_active=True,
        account_owner_id=owner.id,
    )
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="NoOffer HQ",
        is_active=True,
    )
    db_session.add(site)
    db_session.flush()

    card = MaterialCard(normalized_mpn="pic16f877a", display_mpn="PIC16F877A", search_count=1)
    db_session.add(card)
    db_session.flush()

    req = Requisition(
        name="PIC Req",
        customer_site_id=site.id,
        status="archived",
        created_by=owner.id,
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="PIC16F877A",
        normalized_mpn="pic16f877a",
        material_card_id=card.id,
    )
    db_session.add(requirement)
    db_session.flush()

    cph = CustomerPartHistory(
        company_id=company.id,
        material_card_id=card.id,
        mpn="PIC16F877A",
        source="avail_offer",
        purchase_count=4,
        last_purchased_at=datetime.now(timezone.utc) - timedelta(days=30),
        avg_unit_price=Decimal("10.00"),
        last_unit_price=Decimal("11.00"),
        total_quantity=200,
    )
    db_session.add(cph)
    db_session.flush()

    # NO offer exists for this card — sighting won't find fallback
    sighting = Sighting(
        requirement_id=requirement.id,
        material_card_id=card.id,
        vendor_name="Mouser",
        mpn_matched="PIC16F877A",
        unit_price=Decimal("7.00"),
    )
    db_session.add(sighting)
    db_session.commit()

    matches = find_matches_for_sighting(sighting.id, db_session)
    assert len(matches) == 0


# ── run_proactive_scan dedup + sightings (lines 322, 328-332) ────────────


def test_run_proactive_scan_dedup_and_sightings(db_session):
    """Batch scan deduplicates same material_card_id across offers and scans sightings."""
    import app.services.proactive_matching as pm

    data = _setup_scenario(db_session)

    # Two offers with the same material_card_id — second should be deduped
    offer1 = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Arrow",
        mpn="STM32F407",
        unit_price=Decimal("8.00"),
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    offer2 = Offer(
        requisition_id=data["requisition"].id,
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="Mouser",
        mpn="STM32F407",
        unit_price=Decimal("9.00"),
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([offer1, offer2])
    db_session.flush()

    # A sighting with the same card — should also be deduped
    sighting = Sighting(
        requirement_id=data["requirement"].id,
        material_card_id=data["card"].id,
        vendor_name="DigiKey",
        mpn_matched="STM32F407",
        unit_price=Decimal("9.50"),
        is_unavailable=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.flush()

    # Also add a sighting with a DIFFERENT card to test the sighting scan path
    card2 = MaterialCard(normalized_mpn="lm358n", display_mpn="LM358N", search_count=1)
    db_session.add(card2)
    db_session.flush()

    # Need CPH, requisition, requirement, and an existing offer for this second card
    req2 = Requisition(
        name="Req2",
        customer_site_id=data["site"].id,
        status="archived",
        created_by=data["owner"].id,
    )
    db_session.add(req2)
    db_session.flush()

    requirement2 = Requirement(
        requisition_id=req2.id,
        primary_mpn="LM358N",
        normalized_mpn="lm358n",
        material_card_id=card2.id,
    )
    db_session.add(requirement2)
    db_session.flush()

    cph2 = CustomerPartHistory(
        company_id=data["company"].id,
        material_card_id=card2.id,
        mpn="LM358N",
        source="avail_offer",
        purchase_count=5,
        last_purchased_at=datetime.now(timezone.utc) - timedelta(days=10),
        avg_unit_price=Decimal("2.00"),
        last_unit_price=Decimal("2.50"),
        total_quantity=1000,
    )
    db_session.add(cph2)
    db_session.flush()

    # Offer for card2 so sighting-triggered match can use it as fallback
    offer3 = Offer(
        requisition_id=req2.id,
        requirement_id=requirement2.id,
        material_card_id=card2.id,
        vendor_name="Arrow",
        mpn="LM358N",
        unit_price=Decimal("1.00"),
        status="active",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(offer3)
    db_session.flush()

    sighting2 = Sighting(
        requirement_id=requirement2.id,
        material_card_id=card2.id,
        vendor_name="Mouser",
        mpn_matched="LM358N",
        unit_price=Decimal("1.20"),
        is_unavailable=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting2)
    db_session.commit()

    pm._last_scan_at = datetime.now(timezone.utc) - timedelta(hours=1)

    result = run_proactive_scan(db_session)

    # offer1 scanned (card1), offer2 deduped, sighting1 deduped (card1 already in set),
    # sighting2 scanned (card2 is new)
    assert result["scanned_offers"] == 2  # Both offers fetched from DB
    assert result["scanned_sightings"] >= 1
    assert result["matches_created"] >= 1


# ── run_proactive_scan commit failure (lines 337-340) ────────────────────


def test_run_proactive_scan_commit_failure(db_session):
    """Commit failure in batch scan triggers rollback and returns matches_created=0."""
    import app.services.proactive_matching as pm

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

    pm._last_scan_at = datetime.now(timezone.utc) - timedelta(hours=1)

    with patch.object(db_session, "commit", side_effect=Exception("DB error")):
        result = run_proactive_scan(db_session)

    assert result["matches_created"] == 0
    assert result["scanned_offers"] >= 1


# ── dismiss_match error paths (lines 364, 366) ──────────────────────────


def test_dismiss_match_not_found(db_session):
    """Dismissing a non-existent match raises ValueError."""
    _setup_scenario(db_session)
    with pytest.raises(ValueError, match="Match not found"):
        dismiss_match(99999, 1, "reason", db_session)


def test_dismiss_match_wrong_user(db_session):
    """Dismissing someone else's match raises ValueError."""
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

    # Try to dismiss with a different user_id
    other_user = User(
        email="other@trioscs.com",
        name="Other",
        role="sales",
        azure_id="other-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_user)
    db_session.commit()

    with pytest.raises(ValueError, match="Not your match"):
        dismiss_match(matches[0].id, other_user.id, "reason", db_session)


# ── mark_match_sent error paths (lines 376, 378) ────────────────────────


def test_mark_match_sent_not_found(db_session):
    """Marking a non-existent match as sent raises ValueError."""
    _setup_scenario(db_session)
    with pytest.raises(ValueError, match="Match not found"):
        mark_match_sent(99999, 1, db_session)


def test_mark_match_sent_wrong_user(db_session):
    """Marking someone else's match as sent raises ValueError."""
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

    other_user = User(
        email="other2@trioscs.com",
        name="Other2",
        role="sales",
        azure_id="other-002",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_user)
    db_session.commit()

    with pytest.raises(ValueError, match="Not your match"):
        mark_match_sent(matches[0].id, other_user.id, db_session)
