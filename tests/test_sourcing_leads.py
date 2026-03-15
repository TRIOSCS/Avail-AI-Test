"""Tests for canonical sourcing lead flow (lead/evidence/status/feedback)."""

from datetime import datetime, timezone

from app.models import LeadEvidence, LeadFeedbackEvent, Sighting, SourcingLead, VendorCard
from app.services.sourcing_leads import (
    sync_leads_for_sightings,
    update_lead_status,
    _compute_vendor_safety,
    _signal_type_for_source,
    _match_type_for_parts,
    _reliability_band,
    _source_category,
)


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


def test_evidence_fields_match_handoff_spec(db_session, test_requisition):
    """Evidence signal_type, source_reliability_band, confidence_impact, match_type
    should follow the handoff schema values, not hardcoded defaults."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey")

    sync_leads_for_sightings(db_session, requirement, [s1])
    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    # match_type should be 'exact' (not 'near')
    assert lead.match_type in ("exact", "normalized", "fuzzy", "cross_ref")

    ev = db_session.query(LeadEvidence).filter(LeadEvidence.lead_id == lead.id).first()
    assert ev is not None

    # signal_type should be 'stock_listing' for digikey (an API source)
    assert ev.signal_type == "stock_listing"

    # source_reliability_band should be high/medium/low (not based on confidence band)
    assert ev.source_reliability_band in ("high", "medium", "low")

    # confidence_impact should be a small number (scoring contribution), not the full lead score
    assert ev.confidence_impact < 25, f"confidence_impact should be incremental, got {ev.confidence_impact}"

    # verification_state starts as raw
    assert ev.verification_state == "raw"


def test_signal_type_varies_by_source():
    """signal_type should differ based on connector source_type."""
    assert _signal_type_for_source("digikey") == "stock_listing"
    assert _signal_type_for_source("brokerbin") == "stock_listing"
    assert _signal_type_for_source("vendor_affinity") == "vendor_affinity"
    assert _signal_type_for_source("salesforce") == "vendor_history"
    assert _signal_type_for_source("material_history") == "historical_activity"
    assert _signal_type_for_source("ai_live_web") == "web_discovery"
    assert _signal_type_for_source("email_mining") == "email_signal"


def test_match_type_uses_spec_enum():
    """match_type should use exact/normalized/fuzzy/cross_ref per lead.schema.yaml."""
    assert _match_type_for_parts("LM317T", "LM317T") == "exact"
    assert _match_type_for_parts("LM-317T", "LM317T") == "exact"  # normalized MPN strips dashes
    assert _match_type_for_parts("LM317T", "LM317TAMP") == "normalized"  # substring
    assert _match_type_for_parts("LM317T", "XYZ999") == "fuzzy"  # no relationship


def test_reliability_band_values():
    """source_reliability_band should be high/medium/low."""
    assert _reliability_band(80) == "high"
    assert _reliability_band(60) == "medium"
    assert _reliability_band(30) == "low"


def test_source_category_mapping():
    """Connector names should map to handoff source categories."""
    assert _source_category("digikey") == "api"
    assert _source_category("mouser") == "api"
    assert _source_category("brokerbin") == "marketplace"
    assert _source_category("oemsecrets") == "marketplace"
    assert _source_category("salesforce") == "salesforce_history"
    assert _source_category("material_history") == "avail_history"
    assert _source_category("ai_live_web") == "web_ai"


def test_corroboration_requires_distinct_categories(db_session, test_requisition):
    """Two sightings from the same source category should NOT produce corroboration.
    Two sightings from different categories SHOULD."""
    requirement = test_requisition.requirements[0]

    # digikey + mouser = both 'api' category → NOT corroborated
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Same Cat Vendor")
    s2 = _make_sighting(db_session, requirement.id, source_type="mouser", vendor="Same Cat Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1, s2])

    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None
    assert lead.evidence_count == 2
    assert lead.corroborated is False, "Same-category sources should not produce corroboration"


def test_corroboration_with_distinct_categories(db_session, test_requisition):
    """digikey (api) + brokerbin (marketplace) = different categories → corroborated."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Cross Cat Vendor")
    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin", vendor="Cross Cat Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1, s2])

    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None
    assert lead.evidence_count == 2
    assert lead.corroborated is True, "Different-category sources should produce corroboration"


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


def test_duplicate_auto_merged_for_shared_vendor_card(db_session, test_requisition):
    """Two leads with same vendor_card_id (strong signal) get auto-merged into one."""
    from app.vendor_utils import normalize_vendor_name

    requirement = test_requisition.requirements[0]

    # Create vendor card whose normalized_name matches what _find_vendor_card looks up
    vendor_a_norm = normalize_vendor_name("Dup Shared Vendor")
    card = VendorCard(
        normalized_name=vendor_a_norm,
        display_name="Dup Shared Vendor",
        vendor_score=50.0,
    )
    db_session.add(card)
    db_session.commit()

    # First lead uses the vendor name matching the card
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Dup Shared Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1])

    # Second lead uses a different vendor name (creates a separate lead)
    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin", vendor="Dup Other Vendor")
    sync_leads_for_sightings(db_session, requirement, [s2])

    leads = db_session.query(SourcingLead).filter(
        SourcingLead.requirement_id == requirement.id
    ).all()
    assert len(leads) == 2, "Different vendor names should create separate leads"

    # Manually assign second lead to same vendor card to simulate domain-based match
    for lead in leads:
        lead.vendor_card_id = card.id
    db_session.commit()

    # Re-sync first vendor to trigger duplicate check with card loaded
    s3 = _make_sighting(db_session, requirement.id, source_type="nexar", vendor="Dup Shared Vendor")
    sync_leads_for_sightings(db_session, requirement, [s3])

    db_session.expire_all()
    leads = db_session.query(SourcingLead).filter(
        SourcingLead.requirement_id == requirement.id
    ).all()

    # Strong signal (shared vendor_card_id) triggers auto-merge → 1 lead remains
    assert len(leads) == 1, f"Auto-merge should reduce to 1 lead, got {len(leads)}"
    assert leads[0].evidence_count >= 2, "Merged lead should have combined evidence"


def test_no_duplicate_flag_for_distinct_vendors(db_session, test_requisition):
    """Leads with different vendor cards should NOT be flagged as duplicate_candidate."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Vendor Alpha")
    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin", vendor="Vendor Beta")
    sync_leads_for_sightings(db_session, requirement, [s1, s2])

    leads = db_session.query(SourcingLead).filter(
        SourcingLead.requirement_id == requirement.id
    ).all()
    assert len(leads) == 2

    for lead in leads:
        flags = lead.risk_flags or []
        assert "duplicate_candidate" not in flags, "Distinct vendors should not be flagged as duplicates"


def test_has_stock_sets_evidence_buyer_confirmed(db_session, test_requisition):
    """has_stock status transitions evidence verification_state from raw to buyer_confirmed."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Verify Test Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1])

    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    ev = db_session.query(LeadEvidence).filter(LeadEvidence.lead_id == lead.id).first()
    assert ev is not None
    assert ev.verification_state == "raw"

    update_lead_status(db_session, lead.id, "has_stock", note="Confirmed stock")
    db_session.refresh(ev)
    assert ev.verification_state == "buyer_confirmed"


def test_bad_lead_sets_evidence_rejected(db_session, test_requisition):
    """bad_lead status transitions evidence verification_state from raw to rejected."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="brokerbin", vendor="Reject Test Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1])

    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    update_lead_status(db_session, lead.id, "bad_lead", note="Fake listing")
    ev = db_session.query(LeadEvidence).filter(LeadEvidence.lead_id == lead.id).first()
    assert ev is not None
    assert ev.verification_state == "rejected"


def test_positive_safety_signals_with_enriched_card(db_session):
    """Enriched vendor card produces positive: prefixed safety flags."""
    card = VendorCard(
        normalized_name="positive-test-vendor",
        display_name="Positive Test Vendor",
        website="https://positivevendor.com",
        domain="positivevendor.com",
        hq_city="Austin",
        hq_country="US",
        legal_name="Positive Vendor LLC",
        emails=["sales@positivevendor.com"],
        phones=["+1-555-9999"],
        is_new_vendor=False,
        sighting_count=10,
        relationship_months=12,
        total_wins=5,
        vendor_score=70.0,
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    score, flags, summary = _compute_vendor_safety(card, contactability=80.0)
    positive = [f for f in flags if f.startswith("positive:")]
    caution = [f for f in flags if not f.startswith("positive:")]

    assert len(positive) >= 3, f"Expected at least 3 positive signals, got {positive}"
    assert "positive:verified_business_footprint" in positive
    assert "positive:established_relationship" in positive
    assert "positive:proven_success_history" in positive
    assert "positive:contact_channels_present" in positive
    assert len(caution) == 0, f"Well-enriched card should have no caution flags, got {caution}"


def test_no_positive_signals_for_bare_card(db_session):
    """Bare vendor card produces only caution flags, no positive signals."""
    card = VendorCard(
        normalized_name="bare-pos-test",
        display_name="Bare Pos Test",
        is_new_vendor=True,
        sighting_count=0,
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    score, flags, summary = _compute_vendor_safety(card, contactability=30.0)
    positive = [f for f in flags if f.startswith("positive:")]
    caution = [f for f in flags if not f.startswith("positive:")]

    assert len(positive) == 0, f"Bare card should have no positive signals, got {positive}"
    assert len(caution) >= 2, f"Bare card should have caution flags, got {caution}"


def test_cross_ref_match_type_with_substitutes():
    """cross_ref match_type returned when matched part is in substitutes list."""
    assert _match_type_for_parts("LM317T", "LM317AHVT", substitutes=["LM317AHVT"]) == "cross_ref"
    assert _match_type_for_parts("LM317T", "LM317AHVT", substitutes=[{"mpn": "LM317AHVT"}]) == "cross_ref"
    # Not in substitutes → fuzzy
    assert _match_type_for_parts("LM317T", "XYZ999", substitutes=["ABC123"]) == "fuzzy"
    # No substitutes → fuzzy
    assert _match_type_for_parts("LM317T", "XYZ999") == "fuzzy"


def test_inferred_verification_state_on_corroboration(db_session, test_requisition):
    """Corroborated evidence (2+ source categories) promotes verification_state to inferred."""
    requirement = test_requisition.requirements[0]
    # digikey (api) + brokerbin (marketplace) = corroborated
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Infer Test Vendor")
    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin", vendor="Infer Test Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1, s2])

    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None
    assert lead.corroborated is True

    evidence = db_session.query(LeadEvidence).filter(LeadEvidence.lead_id == lead.id).all()
    for ev in evidence:
        assert ev.verification_state == "inferred", f"Corroborated evidence should be inferred, got {ev.verification_state}"


def test_auto_merge_on_strong_duplicate_signal(db_session, test_requisition):
    """Two leads with same vendor_card_id (2 signals) get auto-merged."""
    from app.vendor_utils import normalize_vendor_name

    requirement = test_requisition.requirements[0]

    # Create two vendor cards with same domain (will count as 1 signal each for vendor_card + domain = 2)
    card = VendorCard(
        normalized_name=normalize_vendor_name("Merge Test Vendor"),
        display_name="Merge Test Vendor",
        domain="mergetest.com",
        vendor_score=50.0,
    )
    db_session.add(card)
    db_session.commit()

    # First sighting creates lead 1
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Merge Test Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1])

    # Second sighting with different vendor name creates lead 2
    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin", vendor="Merge Other Name")
    sync_leads_for_sightings(db_session, requirement, [s2])

    leads = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).all()
    assert len(leads) == 2, "Should have 2 leads before merge"

    # Assign both leads to the same vendor card to trigger strong signal
    for lead in leads:
        lead.vendor_card_id = card.id
    db_session.commit()

    # Re-sync to trigger duplicate check with shared vendor card
    s3 = _make_sighting(db_session, requirement.id, source_type="nexar", vendor="Merge Test Vendor")
    sync_leads_for_sightings(db_session, requirement, [s3])

    db_session.expire_all()
    leads = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).all()
    assert len(leads) == 1, f"Auto-merge should reduce to 1 lead, got {len(leads)}"
    # Survivor should have all evidence combined
    assert leads[0].evidence_count >= 2


def test_no_auto_merge_when_buyer_acted(db_session, test_requisition):
    """Auto-merge does not happen if the duplicate lead has been acted on by buyer."""
    from app.vendor_utils import normalize_vendor_name

    requirement = test_requisition.requirements[0]

    card = VendorCard(
        normalized_name=normalize_vendor_name("No Merge Vendor"),
        display_name="No Merge Vendor",
        domain="nomerge.com",
        vendor_score=50.0,
    )
    db_session.add(card)
    db_session.commit()

    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="No Merge Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1])

    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin", vendor="No Merge Alt")
    sync_leads_for_sightings(db_session, requirement, [s2])

    leads = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).all()
    assert len(leads) == 2

    # Mark second lead as contacted by buyer
    for lead in leads:
        lead.vendor_card_id = card.id
    leads[1].buyer_status = "contacted"
    db_session.commit()

    # Re-sync — should flag, not merge, because buyer acted on one lead
    s3 = _make_sighting(db_session, requirement.id, source_type="nexar", vendor="No Merge Vendor")
    sync_leads_for_sightings(db_session, requirement, [s3])

    db_session.expire_all()
    leads = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).all()
    assert len(leads) == 2, "Should NOT auto-merge when buyer has acted on a lead"
    flagged = [l for l in leads if "duplicate_candidate" in (l.risk_flags or [])]
    assert len(flagged) >= 1, "Should flag as duplicate_candidate instead of merging"
