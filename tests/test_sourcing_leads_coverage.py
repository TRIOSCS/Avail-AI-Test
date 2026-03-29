"""Tests for app/services/sourcing_leads.py — comprehensive coverage.

Covers utility functions, upsert_lead_from_sighting, sync_leads_for_sightings,
append_evidence, get_requisition_leads, update_lead_status, attach_lead_metadata.

Called by: pytest
Depends on: conftest fixtures, sourcing_leads
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import User, VendorCard
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.sourcing_lead import SourcingLead


@pytest.fixture()
def req_pair(db_session: Session, test_user: User) -> tuple:
    req = Requisition(
        name="SL-TEST-REQ",
        customer_name="Test Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


@pytest.fixture()
def basic_sighting(db_session: Session, req_pair: tuple) -> Sighting:
    _, item = req_pair
    s = Sighting(
        requirement_id=item.id,
        normalized_mpn="lm317t",
        mpn_matched="LM317T",
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow",
        vendor_email="sales@arrow.com",
        vendor_phone="+1-555-0100",
        source_type="digikey",
        qty_available=1000,
        unit_price=0.50,
        currency="USD",
        score=80.0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def vendor_card(db_session: Session) -> VendorCard:
    vc = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        emails=["sales@arrow.com"],
        phones=["+1-555-0100"],
        website="https://arrow.com",
        domain="arrow.com",
        vendor_score=75.0,
        is_blacklisted=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


class TestUtilityFunctions:
    def test_normalize_mpn_basic(self):
        from app.services.sourcing_leads import normalize_mpn
        assert normalize_mpn("LM-317T") == "LM317T"
        assert normalize_mpn("lm317t") == "LM317T"
        assert normalize_mpn("LM 317T") == "LM317T"

    def test_normalize_mpn_empty(self):
        from app.services.sourcing_leads import normalize_mpn
        assert normalize_mpn("") == ""
        assert normalize_mpn(None) == ""

    def test_clamp_within_bounds(self):
        from app.services.sourcing_leads import _clamp
        assert _clamp(50.0) == 50.0
        assert _clamp(-10.0) == 0.0
        assert _clamp(110.0) == 100.0

    def test_confidence_band(self):
        from app.services.sourcing_leads import _confidence_band
        assert _confidence_band(80.0) == "high"
        assert _confidence_band(60.0) == "medium"
        assert _confidence_band(30.0) == "low"

    def test_safety_band(self):
        from app.services.sourcing_leads import _safety_band
        assert _safety_band(80.0) == "low_risk"
        assert _safety_band(60.0) == "medium_risk"
        assert _safety_band(30.0) == "high_risk"
        assert _safety_band(30.0, has_vendor_data=False) == "unknown"

    def test_source_reliability_known_sources(self):
        from app.services.sourcing_leads import _source_reliability
        assert _source_reliability("digikey", None) >= 80
        assert _source_reliability("brokerbin", None) >= 60
        assert _source_reliability("ai", None) <= 50

    def test_source_reliability_with_tier(self):
        from app.services.sourcing_leads import _source_reliability
        r_t1 = _source_reliability("api", "T1")
        r_t7 = _source_reliability("api", "T7")
        assert r_t1 > r_t7

    def test_freshness_score_recent(self):
        from app.services.sourcing_leads import _freshness_score
        assert _freshness_score(datetime.now(timezone.utc)) == 95.0

    def test_freshness_score_old(self):
        from app.services.sourcing_leads import _freshness_score
        old = datetime.now(timezone.utc) - timedelta(days=60)
        assert _freshness_score(old) == 25.0

    def test_freshness_score_none(self):
        from app.services.sourcing_leads import _freshness_score
        assert _freshness_score(None) == 45.0

    def test_match_type_exact(self):
        from app.services.sourcing_leads import _match_type_for_parts
        assert _match_type_for_parts("LM317T", "LM317T") == "exact"

    def test_match_type_normalized(self):
        from app.services.sourcing_leads import _match_type_for_parts
        # "ABCDE" contains "ABC" so it's a normalized prefix match
        assert _match_type_for_parts("ABC", "ABCDE") == "normalized"

    def test_match_type_cross_ref(self):
        from app.services.sourcing_leads import _match_type_for_parts
        subs = [{"mpn": "LM7805"}]
        assert _match_type_for_parts("LM317T", "LM7805", substitutes=subs) == "cross_ref"

    def test_match_type_fuzzy(self):
        from app.services.sourcing_leads import _match_type_for_parts
        assert _match_type_for_parts("LM317T", "XYZ999") == "fuzzy"

    def test_suggested_next_action_low_safety(self):
        from app.services.sourcing_leads import _suggested_next_action
        result = _suggested_next_action(80.0, 40.0, 80.0)
        assert "Verify" in result

    def test_suggested_next_action_low_contactability(self):
        from app.services.sourcing_leads import _suggested_next_action
        result = _suggested_next_action(80.0, 80.0, 20.0)
        assert "contact" in result.lower()

    def test_suggested_next_action_high_confidence(self):
        from app.services.sourcing_leads import _suggested_next_action
        result = _suggested_next_action(80.0, 80.0, 80.0)
        assert "Contact now" in result

    def test_source_category_mapping(self):
        from app.services.sourcing_leads import _source_category
        assert _source_category("digikey") == "api"
        assert _source_category("brokerbin") == "marketplace"
        assert _source_category("ai") == "web_ai"
        assert _source_category("email_mining") == "marketplace"
        assert _source_category("unknown_type") == "marketplace"

    def test_signal_type_for_source(self):
        from app.services.sourcing_leads import _signal_type_for_source
        assert _signal_type_for_source("digikey") == "stock_listing"
        assert _signal_type_for_source("vendor_affinity") == "vendor_affinity"
        assert _signal_type_for_source("email_mining") == "email_signal"
        assert _signal_type_for_source("salesforce") == "vendor_history"

    def test_reliability_band(self):
        from app.services.sourcing_leads import _reliability_band
        assert _reliability_band(80) == "high"
        assert _reliability_band(60) == "medium"
        assert _reliability_band(30) == "low"

    def test_build_lead_risk_flags_all_flags(self):
        from app.services.sourcing_leads import _build_lead_risk_flags
        flags = _build_lead_risk_flags(30.0, 40.0, 30.0, 20.0)
        assert "lower_reliability_source" in flags
        assert "stale_signal" in flags
        assert "limited_contactability" in flags
        assert "low_stock_confidence" in flags

    def test_build_lead_risk_flags_no_flags(self):
        from app.services.sourcing_leads import _build_lead_risk_flags
        flags = _build_lead_risk_flags(80.0, 80.0, 80.0, 80.0)
        assert flags == []

    def test_add_risk_flag_adds_new(self):
        from unittest.mock import MagicMock

        from app.services.sourcing_leads import _add_risk_flag

        lead = MagicMock(spec=SourcingLead)
        lead.risk_flags = []
        _add_risk_flag(lead, "new_flag")
        assert "new_flag" in lead.risk_flags

    def test_add_risk_flag_no_duplicate(self):
        from unittest.mock import MagicMock

        from app.services.sourcing_leads import _add_risk_flag

        lead = MagicMock(spec=SourcingLead)
        lead.risk_flags = ["existing_flag"]
        _add_risk_flag(lead, "existing_flag")
        assert lead.risk_flags.count("existing_flag") == 1

    def test_normalize_phone(self):
        from app.services.sourcing_leads import _normalize_phone
        assert _normalize_phone("+1-555-0100") == "15550100"
        assert _normalize_phone(None) == ""
        assert _normalize_phone("") == ""

    def test_as_utc_none_returns_none(self):
        from app.services.sourcing_leads import _as_utc
        assert _as_utc(None) is None

    def test_as_utc_naive_gets_tzinfo(self):
        from app.services.sourcing_leads import _as_utc
        naive = datetime(2024, 1, 1, 12, 0, 0)
        result = _as_utc(naive)
        assert result.tzinfo is not None

    def test_as_utc_aware_converted(self):
        from app.services.sourcing_leads import _as_utc
        aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _as_utc(aware)
        assert result.tzinfo is not None


class TestContactabilityScore:
    def test_with_email_and_phone(self, db_session: Session, basic_sighting: Sighting):
        from app.services.sourcing_leads import _contactability_score
        score = _contactability_score(basic_sighting, None)
        assert score >= 45  # email alone gives 45

    def test_with_vendor_card_emails(self, db_session: Session, basic_sighting: Sighting, vendor_card: VendorCard):
        from app.services.sourcing_leads import _contactability_score
        score = _contactability_score(basic_sighting, vendor_card)
        assert score >= 70

    def test_no_contact_info(self, db_session: Session, req_pair: tuple):
        from app.services.sourcing_leads import _contactability_score
        _, item = req_pair
        s = Sighting(
            requirement_id=item.id,
            normalized_mpn="testmpn",
            vendor_name="No Contact",
            source_type="api",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        score = _contactability_score(s, None)
        assert score == 0.0


class TestComputeVendorSafety:
    def test_no_vendor_card(self):
        from app.services.sourcing_leads import _compute_vendor_safety
        score, flags, summary = _compute_vendor_safety(None, 50.0)
        assert "no_internal_vendor_profile" in flags
        assert "unknown" in summary.lower() or score < 50

    def test_blacklisted_vendor(self, db_session: Session):
        from app.services.sourcing_leads import _compute_vendor_safety
        vc = VendorCard(
            normalized_name="blacklisted",
            display_name="Bad Vendor",
            is_blacklisted=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vc)
        db_session.commit()
        score, flags, summary = _compute_vendor_safety(vc, 50.0)
        assert "internal_do_not_contact_history" in flags
        assert score < 50

    def test_vendor_with_good_profile(self, db_session: Session, vendor_card: VendorCard):
        from app.services.sourcing_leads import _compute_vendor_safety
        score, flags, summary = _compute_vendor_safety(vendor_card, 70.0)
        assert score > 40

    def test_low_contactability_penalizes(self):
        from app.services.sourcing_leads import _compute_vendor_safety
        score_high, _, _ = _compute_vendor_safety(None, 80.0)
        score_low, flags_low, _ = _compute_vendor_safety(None, 20.0)
        assert "limited_verified_contact_channels" in flags_low


class TestUpsertLeadFromSighting:
    def test_creates_new_lead(self, db_session: Session, req_pair: tuple, basic_sighting: Sighting):
        from app.services.sourcing_leads import upsert_lead_from_sighting
        _, item = req_pair
        lead = upsert_lead_from_sighting(db_session, item, basic_sighting)
        db_session.flush()
        assert lead.requirement_id == item.id
        assert lead.vendor_name == "Arrow Electronics"
        assert lead.confidence_score is not None

    def test_upserts_existing_lead(self, db_session: Session, req_pair: tuple, basic_sighting: Sighting):
        from app.services.sourcing_leads import upsert_lead_from_sighting
        _, item = req_pair
        lead1 = upsert_lead_from_sighting(db_session, item, basic_sighting)
        db_session.flush()
        lead2 = upsert_lead_from_sighting(db_session, item, basic_sighting)
        db_session.flush()
        assert lead1.id == lead2.id

    def test_lead_with_vendor_card(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting, vendor_card: VendorCard
    ):
        from app.services.sourcing_leads import upsert_lead_from_sighting
        _, item = req_pair
        lead = upsert_lead_from_sighting(db_session, item, basic_sighting)
        db_session.flush()
        assert lead.vendor_card_id == vendor_card.id

    def test_lead_without_vendor_name(self, db_session: Session, req_pair: tuple):
        from unittest.mock import MagicMock

        from app.services.sourcing_leads import upsert_lead_from_sighting
        _, item = req_pair
        # Use a mock sighting to avoid DB NOT NULL constraint on vendor_name
        s = MagicMock(spec=Sighting)
        s.id = 9999
        s.vendor_name = None
        s.vendor_name_normalized = None
        s.mpn_matched = "LM317T"
        s.mpn = "LM317T"
        s.source_type = "api"
        s.evidence_tier = None
        s.vendor_email = None
        s.vendor_phone = None
        s.raw_data = {}
        s.score = None
        s.confidence = None
        s.is_authorized = False
        s.unit_price = None
        s.qty_available = None
        s.created_at = datetime.now(timezone.utc)

        lead = upsert_lead_from_sighting(db_session, item, s)
        db_session.flush()
        assert lead.vendor_name == "Unknown Vendor"


class TestSyncLeadsForSightings:
    def test_empty_sightings_returns_zero(self, db_session: Session, req_pair: tuple):
        from app.services.sourcing_leads import sync_leads_for_sightings
        _, item = req_pair
        result = sync_leads_for_sightings(db_session, item, [])
        assert result == 0

    def test_sync_creates_leads(self, db_session: Session, req_pair: tuple, basic_sighting: Sighting):
        from app.services.sourcing_leads import sync_leads_for_sightings
        _, item = req_pair
        result = sync_leads_for_sightings(db_session, item, [basic_sighting])
        assert result == 1

    def test_skips_sightings_without_vendor(self, db_session: Session, req_pair: tuple):
        from unittest.mock import MagicMock

        from app.services.sourcing_leads import sync_leads_for_sightings
        _, item = req_pair
        # Mock sighting with no vendor_name to test filtering logic
        s = MagicMock(spec=Sighting)
        s.vendor_name = None
        result = sync_leads_for_sightings(db_session, item, [s])
        assert result == 0

    def test_multiple_sightings(self, db_session: Session, req_pair: tuple, basic_sighting: Sighting):
        from app.services.sourcing_leads import sync_leads_for_sightings
        _, item = req_pair
        s2 = Sighting(
            requirement_id=item.id,
            normalized_mpn="lm317t",
            mpn_matched="LM317T",
            vendor_name="Mouser Electronics",
            source_type="mouser",
            qty_available=500,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s2)
        db_session.commit()
        result = sync_leads_for_sightings(db_session, item, [basic_sighting, s2])
        assert result == 2


class TestGetRequisitionLeads:
    def test_empty_returns_empty_list(self, db_session: Session):
        from app.services.sourcing_leads import get_requisition_leads
        result = get_requisition_leads(db_session, 99999)
        assert result == []

    def test_returns_leads_for_requisition(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting
    ):
        from app.services.sourcing_leads import get_requisition_leads, sync_leads_for_sightings
        req, item = req_pair
        sync_leads_for_sightings(db_session, item, [basic_sighting])
        result = get_requisition_leads(db_session, req.id)
        assert len(result) == 1

    def test_filters_by_status(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting
    ):
        from app.services.sourcing_leads import get_requisition_leads, sync_leads_for_sightings
        req, item = req_pair
        sync_leads_for_sightings(db_session, item, [basic_sighting])
        result = get_requisition_leads(db_session, req.id, statuses=["new"])
        assert all(r.buyer_status == "new" for r in result)

    def test_invalid_status_ignored(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting
    ):
        from app.services.sourcing_leads import get_requisition_leads, sync_leads_for_sightings
        req, item = req_pair
        sync_leads_for_sightings(db_session, item, [basic_sighting])
        # Invalid status should be filtered out, returning all leads
        result = get_requisition_leads(db_session, req.id, statuses=["INVALID_STATUS"])
        assert isinstance(result, list)


class TestUpdateLeadStatus:
    def test_invalid_status_raises(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting
    ):
        from app.services.sourcing_leads import sync_leads_for_sightings, update_lead_status
        _, item = req_pair
        sync_leads_for_sightings(db_session, item, [basic_sighting])
        lead = db_session.query(SourcingLead).first()
        with pytest.raises(ValueError):
            update_lead_status(db_session, lead.id, "INVALID_STATUS")

    def test_valid_status_update(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting
    ):
        from app.services.sourcing_leads import sync_leads_for_sightings, update_lead_status
        _, item = req_pair
        sync_leads_for_sightings(db_session, item, [basic_sighting])
        lead = db_session.query(SourcingLead).first()
        updated = update_lead_status(db_session, lead.id, "contacted")
        assert updated.buyer_status == "contacted"

    def test_not_found_returns_none(self, db_session: Session):
        from app.services.sourcing_leads import update_lead_status
        result = update_lead_status(db_session, 99999, "contacted")
        assert result is None

    def test_has_stock_propagates_to_vendor_card(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting, vendor_card: VendorCard
    ):
        from app.services.sourcing_leads import sync_leads_for_sightings, update_lead_status
        _, item = req_pair
        sync_leads_for_sightings(db_session, item, [basic_sighting])
        lead = db_session.query(SourcingLead).first()
        original_wins = vendor_card.total_wins or 0
        update_lead_status(db_session, lead.id, "has_stock")
        db_session.refresh(vendor_card)
        assert (vendor_card.total_wins or 0) > original_wins

    def test_do_not_contact_blacklists_vendor(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting, vendor_card: VendorCard
    ):
        from app.services.sourcing_leads import sync_leads_for_sightings, update_lead_status
        _, item = req_pair
        sync_leads_for_sightings(db_session, item, [basic_sighting])
        lead = db_session.query(SourcingLead).order_by(SourcingLead.id.desc()).first()
        assert lead is not None
        # Attach vendor_card_id explicitly for the blacklist test
        lead.vendor_card_id = vendor_card.id
        db_session.commit()
        update_lead_status(db_session, lead.id, "do_not_contact")
        db_session.refresh(vendor_card)
        assert vendor_card.is_blacklisted is True


class TestAttachLeadMetadata:
    def test_empty_dict_no_op(self, db_session: Session):
        from app.services.sourcing_leads import attach_lead_metadata_to_results
        attach_lead_metadata_to_results(db_session, {})  # Should not raise

    def test_attaches_metadata_to_matching_rows(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting
    ):
        from app.services.sourcing_leads import attach_lead_metadata_to_results, sync_leads_for_sightings
        req, item = req_pair
        sync_leads_for_sightings(db_session, item, [basic_sighting])

        rows = [{"vendor_name": "Arrow Electronics", "mpn_matched": "LM317T"}]
        results_by_req = {item.id: rows}
        attach_lead_metadata_to_results(db_session, results_by_req)
        assert "lead_id" in rows[0]
        assert "confidence_score" in rows[0]

    def test_no_match_row_unchanged(
        self, db_session: Session, req_pair: tuple, basic_sighting: Sighting
    ):
        from app.services.sourcing_leads import attach_lead_metadata_to_results, sync_leads_for_sightings
        req, item = req_pair
        sync_leads_for_sightings(db_session, item, [basic_sighting])

        rows = [{"vendor_name": "Unknown Vendor XYZ", "mpn_matched": "UNKNOWN999"}]
        results_by_req = {item.id: rows}
        attach_lead_metadata_to_results(db_session, results_by_req)
        assert "lead_id" not in rows[0]
