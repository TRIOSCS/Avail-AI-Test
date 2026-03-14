"""Tests for canonical sourcing lead flow (lead/evidence/status/feedback)."""

from datetime import datetime, timezone

from app.models import LeadEvidence, LeadFeedbackEvent, Sighting, SourcingLead, VendorCard
from app.services.sourcing_leads import sync_leads_for_sightings, update_lead_status, _compute_vendor_safety


def _make_sighting(db_session, requirement_id: int, *, source_type: str, vendor: str = "Arrow Electronics") -> Sighting:
    s = Sighting(
        requirement_id=requirement_id,
        vendor_name=vendor,
        vendor_name_normalized=vendor.lower(),
        mpn_matched="LM317T",
        source_type=source_type,
        qty_available=1200,
        unit_price=0.41,
        confidence=0.8,
        score=82.0,
        raw_data={"vendor_url": f"https://example.com/{source_type}/LM317T", "description": "Live stock listing"},
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def test_sync_sightings_creates_one_lead_with_multiple_evidence(db_session, test_requisition):
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey")
    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin")

    synced = sync_leads_for_sightings(db_session, requirement, [s1, s2])
    assert synced == 2

    leads = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).all()
    assert len(leads) == 1
    lead = leads[0]
    assert lead.evidence_count == 2
    assert lead.corroborated is True

    ev_count = db_session.query(LeadEvidence).filter(LeadEvidence.lead_id == lead.id).count()
    assert ev_count == 2


def test_update_lead_status_writes_feedback_event(db_session, test_requisition):
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Future Electronics")
    sync_leads_for_sightings(db_session, requirement, [s1])
    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    old_conf = lead.confidence_score
    updated = update_lead_status(db_session, lead.id, "has_stock", note="Vendor confirmed 1,200 pcs", actor_user_id=None)
    assert updated is not None
    assert updated.buyer_status == "has_stock"
    assert updated.confidence_score >= old_conf

    events = db_session.query(LeadFeedbackEvent).filter(LeadFeedbackEvent.lead_id == lead.id).all()
    assert len(events) == 1
    assert events[0].status == "has_stock"


def test_lead_endpoints_return_status_and_feedback(client, db_session, test_requisition):
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Avnet")
    sync_leads_for_sightings(db_session, requirement, [s1])
    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    patch_resp = client.patch(f"/api/leads/{lead.id}/status", json={"status": "contacted"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["status"] == "contacted"

    fb_resp = client.post(
        f"/api/leads/{lead.id}/feedback",
        json={"note": "Called sales line, awaiting email reply", "contact_method": "phone"},
    )
    assert fb_resp.status_code == 200

    leads_resp = client.get(f"/api/requisitions/{test_requisition.id}/leads")
    assert leads_resp.status_code == 200
    data = leads_resp.json()
    assert data
    assert data[0]["buyer_status"] == "contacted"

    sightings_resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
    assert sightings_resp.status_code == 200
    payload = sightings_resp.json().get(str(requirement.id), {})
    rows = payload.get("sightings", [])
    assert any(row.get("lead_id") == lead.id for row in rows)


def test_lead_detail_endpoint_returns_evidence_and_feedback(client, db_session, test_requisition):
    """GET /api/leads/{id} returns full lead detail with evidence and feedback."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Detail Test Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1])
    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    # Add a status change to create a feedback event
    client.patch(f"/api/leads/{lead.id}/status", json={"status": "contacted", "note": "Called vendor"})

    resp = client.get(f"/api/leads/{lead.id}")
    assert resp.status_code == 200
    data = resp.json()

    # Core fields
    assert data["vendor_name"] == "Detail Test Vendor"
    assert data["confidence_band"] in ("high", "medium", "low")
    assert data["buyer_status"] == "contacted"

    # Safety fields
    assert "vendor_safety_band" in data
    assert "vendor_safety_flags" in data

    # Contact fields
    assert "contact_email" in data

    # Evidence
    assert len(data["evidence"]) >= 1
    ev = data["evidence"][0]
    assert ev["source_type"] == "digikey"

    # Feedback history
    assert len(data["feedback_events"]) >= 1
    assert data["feedback_events"][0]["status"] == "contacted"


def test_resync_preserves_buyer_status(db_session, test_requisition):
    """Re-syncing sightings must not overwrite a manually set buyer status."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey")
    sync_leads_for_sightings(db_session, requirement, [s1])

    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None
    assert lead.buyer_status == "new"

    update_lead_status(db_session, lead.id, "contacted", note="Called vendor")
    db_session.refresh(lead)
    assert lead.buyer_status == "contacted"

    # Re-sync with a new sighting from a different source — status must stay "contacted"
    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin")
    sync_leads_for_sightings(db_session, requirement, [s2])
    db_session.refresh(lead)
    assert lead.buyer_status == "contacted", "Re-sync should not overwrite manual buyer status"


def test_safety_flags_with_no_vendor_card():
    """No vendor card produces unknown band with appropriate flags."""
    score, flags, summary = _compute_vendor_safety(None, contactability=60.0)
    assert "no_internal_vendor_profile" in flags
    assert "marketplace_trust_unknown" in flags
    assert "unknown" in summary.lower()


def test_safety_flags_with_bare_vendor_card(db_session):
    """Minimal vendor card with no enrichment gets multiple caution flags."""
    card = VendorCard(
        normalized_name="bare-test-vendor",
        display_name="Bare Test Vendor",
        is_new_vendor=True,
        sighting_count=0,
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    score, flags, summary = _compute_vendor_safety(card, contactability=30.0)
    assert "no_business_footprint" in flags
    assert "unverifiable_address" in flags
    assert "new_domain" in flags
    assert score < 50  # should be high_risk or close


def test_safety_flags_with_enriched_vendor_card(db_session):
    """Well-enriched vendor card with good history gets higher safety score."""
    card = VendorCard(
        normalized_name="good-vendor-test",
        display_name="Good Vendor",
        website="https://goodvendor.com",
        domain="goodvendor.com",
        hq_city="Dallas",
        hq_country="US",
        legal_name="Good Vendor LLC",
        emails=["sales@goodvendor.com"],
        phones=["+1-555-1234"],
        is_new_vendor=False,
        sighting_count=10,
        relationship_months=12,
        total_wins=5,
        vendor_score=72.0,
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    score, flags, summary = _compute_vendor_safety(card, contactability=80.0)
    assert score >= 75  # should be low_risk
    assert "no_business_footprint" not in flags
    assert "no_internal_vendor_profile" not in flags
    assert "lower risk" in summary.lower()


def test_has_stock_propagates_to_vendor_card(db_session, test_requisition):
    """Marking lead 'has_stock' increments vendor_card.total_wins."""
    requirement = test_requisition.requirements[0]
    card = VendorCard(
        normalized_name="feedback test vendor",
        display_name="Feedback Test Vendor",
        vendor_score=60.0,
        total_wins=0,
    )
    db_session.add(card)
    db_session.commit()

    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Feedback Test Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1])
    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    # Manually link the vendor card (sync normally does this via _find_vendor_card)
    lead.vendor_card_id = card.id
    db_session.commit()

    update_lead_status(db_session, lead.id, "has_stock", note="Confirmed stock")
    db_session.refresh(card)
    assert card.total_wins == 1
    assert card.vendor_score == 62.0


def test_bad_lead_reduces_vendor_score(db_session, test_requisition):
    """Marking lead 'bad_lead' decreases vendor_card.vendor_score."""
    requirement = test_requisition.requirements[0]
    card = VendorCard(
        normalized_name="bad vendor test",
        display_name="Bad Vendor Test",
        vendor_score=50.0,
    )
    db_session.add(card)
    db_session.commit()

    s1 = _make_sighting(db_session, requirement.id, source_type="brokerbin", vendor="Bad Vendor Test")
    sync_leads_for_sightings(db_session, requirement, [s1])
    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    lead.vendor_card_id = card.id
    db_session.commit()

    update_lead_status(db_session, lead.id, "bad_lead", note="Fake listing")
    db_session.refresh(card)
    assert card.vendor_score == 47.0


def test_vendor_dedup_strips_suffixes(db_session, test_requisition):
    """Sightings from 'Arrow Electronics Inc.' and 'Arrow Electronics' create one lead."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="brokerbin", vendor="Arrow Electronics Inc.")
    s2 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Arrow Electronics")

    synced = sync_leads_for_sightings(db_session, requirement, [s1, s2])
    assert synced == 2

    leads = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).all()
    assert len(leads) == 1, f"Expected 1 lead but got {len(leads)} — suffix stripping failed"
    assert leads[0].evidence_count == 2
    assert leads[0].corroborated is True
