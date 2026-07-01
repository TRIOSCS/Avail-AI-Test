"""Tests for app/services/sourcing_leads.py — lead upsert, scoring, status transitions.

Called by: pytest
Depends on: conftest fixtures, sourcing lead models, vendor card models
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.sourcing_lead import LeadFeedbackEvent, SourcingLead
from app.models.vendors import VendorCard
from app.services.sourcing_leads import (
    BUYER_STATUSES,
    _clamp,
    _compute_vendor_safety,
    _confidence_band,
    _contactability_score,
    _freshness_score,
    _historical_success_score,
    _match_type_for_parts,
    _safety_band,
    _source_category,
    _source_reliability,
    append_lead_feedback,
    get_requisition_leads,
    sync_leads_for_sightings,
    update_lead_status,
    upsert_lead_from_sighting,
)
from app.utils.normalization import normalize_mpn_key

# ── Helpers ─────────────────────────────────────────────────────────


def _make_requisition(db: Session) -> Requisition:
    req = Requisition(
        name="REQ-SL-001",
        customer_name="Test Co",
        status="open",
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_requirement(db: Session, requisition_id: int, mpn: str = "LM317T") -> Requirement:
    r = Requirement(
        requisition_id=requisition_id,
        primary_mpn=mpn,
        target_qty=1000,
        target_price=0.50,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_sighting(
    db: Session,
    requisition_id: int,
    requirement_id: int,
    *,
    vendor_name: str = "Arrow Electronics",
    mpn: str = "LM317T",
    source_type: str = "brokerbin",
    unit_price: float = 0.45,
    qty_available: int = 5000,
    vendor_email: str | None = "sales@arrow.com",
    vendor_phone: str | None = "+15550100",
) -> Sighting:
    s = Sighting(
        requirement_id=requirement_id,
        vendor_name=vendor_name,
        mpn_matched=mpn,
        normalized_mpn=mpn.upper().replace("-", ""),
        source_type=source_type,
        unit_price=unit_price,
        qty_available=qty_available,
        vendor_email=vendor_email,
        vendor_phone=vendor_phone,
        score=72.0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


def _make_vendor_card(db: Session, name: str = "arrow electronics") -> VendorCard:
    vc = VendorCard(
        normalized_name=name,
        display_name=name.title(),
        emails=["sales@arrow.com"],
        phones=["+15550100"],
        website="https://arrow.com",
        domain="arrow.com",
        sighting_count=42,
        vendor_score=75.0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(vc)
    db.flush()
    return vc


# ── Pure function tests ─────────────────────────────────────────────


class TestNormalizeMpn:
    """sourcing_leads now uses the canonical normalize_mpn_key (lowercase, strips all
    non-alphanumeric) instead of a local re-implementation."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("LM-317 T", "lm317t"),
            ("LM_317.T", "lm317t"),
            ("", ""),
            (None, ""),
        ],
        ids=["dashes_spaces", "underscores_dots", "empty_string", "none"],
    )
    def test_normalize(self, raw, expected):
        assert normalize_mpn_key(raw) == expected


class TestClamp:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(50.0, 50.0), (-10.0, 0.0), (120.0, 100.0)],
        ids=["within_range", "below_minimum", "above_maximum"],
    )
    def test_clamp(self, value, expected):
        assert _clamp(value) == expected


class TestConfidenceBand:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [(80.0, "high"), (60.0, "medium"), (30.0, "low"), (75.0, "high")],
        ids=["high", "medium", "low", "boundary_75"],
    )
    def test_confidence_band(self, score, expected):
        assert _confidence_band(score) == expected


class TestSafetyBand:
    def test_low_risk(self):
        assert _safety_band(80.0) == "low_risk"

    def test_medium_risk(self):
        assert _safety_band(60.0) == "medium_risk"

    def test_high_risk(self):
        assert _safety_band(30.0) == "high_risk"

    def test_unknown_when_no_vendor_data(self):
        assert _safety_band(80.0, has_vendor_data=False) == "unknown"


class TestSourceReliability:
    @pytest.mark.parametrize(
        ("source", "tier", "expected"),
        [
            ("digikey", None, 90),
            ("brokerbin", None, 72),
            ("ai", None, 40),
            ("digikey", "T1", 98),  # 90 + 8 tier bonus
            ("digikey", "T7", 75),  # 90 - 15 tier penalty
            ("unknown_thing", None, 60),
        ],
        ids=[
            "authorized_distributor",
            "marketplace_source",
            "ai_source",
            "tier_bonus",
            "tier_penalty",
            "unknown_source",
        ],
    )
    def test_source_reliability(self, source, tier, expected):
        assert _source_reliability(source, tier) == expected


class TestFreshnessScore:
    @pytest.mark.parametrize(
        ("days_ago", "expected"),
        [(0, 95.0), (5, 72.0), (60, 25.0)],
        ids=["recent", "week_old", "old"],
    )
    def test_freshness_score(self, days_ago, expected):
        when = datetime.now(timezone.utc) - timedelta(days=days_ago)
        assert _freshness_score(when) == expected

    def test_none_date(self):
        assert _freshness_score(None) == 45.0


class TestMatchType:
    @pytest.mark.parametrize(
        ("requirement_mpn", "candidate_mpn", "substitutes", "expected"),
        [
            ("LM317T", "LM317T", None, "exact"),
            ("LM317T", "LM-317T", None, "exact"),
            ("LM317", "LM317TANOPB", None, "normalized"),
            ("LM317T", "MC7805", [{"mpn": "MC7805"}, {"mpn": "LM340"}], "cross_ref"),
            ("ABC123", "XYZ789", None, "fuzzy"),
            ("", "", None, "exact"),
        ],
        ids=["exact", "normalized", "substring_normalized", "cross_ref", "fuzzy", "empty_parts"],
    )
    def test_match_type(self, requirement_mpn, candidate_mpn, substitutes, expected):
        assert _match_type_for_parts(requirement_mpn, candidate_mpn, substitutes=substitutes) == expected


class TestSourceCategory:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("digikey", "api"),
            ("mouser", "api"),
            ("brokerbin", "marketplace"),
            ("ai", "web_ai"),
            ("something_random", "marketplace"),
        ],
        ids=["api_digikey", "api_mouser", "marketplace", "web_ai", "unknown_defaults_marketplace"],
    )
    def test_source_category(self, source, expected):
        assert _source_category(source) == expected


# ── DB-integrated tests ─────────────────────────────────────────────


class TestUpsertLead:
    def test_creates_new_lead(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        _make_vendor_card(db_session)
        sighting = _make_sighting(db_session, req.id, requirement.id)

        lead = upsert_lead_from_sighting(db_session, requirement, sighting)
        db_session.flush()

        assert lead.id is not None
        assert lead.vendor_name == "Arrow Electronics"
        assert lead.buyer_status == "new"
        assert lead.confidence_score > 0
        assert lead.vendor_safety_score > 0

    def test_updates_existing_lead(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        _make_vendor_card(db_session)
        sighting1 = _make_sighting(db_session, req.id, requirement.id)

        lead1 = upsert_lead_from_sighting(db_session, requirement, sighting1)
        db_session.flush()
        lead_id = lead1.id

        sighting2 = _make_sighting(db_session, req.id, requirement.id, unit_price=0.40)
        lead2 = upsert_lead_from_sighting(db_session, requirement, sighting2)
        db_session.flush()

        assert lead2.id == lead_id  # same lead updated

    def test_null_vendor_name_skipped_in_sync(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        sighting = Sighting(
            requirement_id=requirement.id,
            vendor_name="",
            mpn_matched="LM317T",
            source_type="brokerbin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.flush()

        count = sync_leads_for_sightings(db_session, requirement, [sighting])
        assert count == 0

    def test_no_vendor_card_still_creates_lead(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        sighting = _make_sighting(db_session, req.id, requirement.id, vendor_name="Unknown Vendor XYZ")

        lead = upsert_lead_from_sighting(db_session, requirement, sighting)
        db_session.flush()

        assert lead.id is not None
        assert lead.vendor_card_id is None
        assert lead.vendor_safety_band == "unknown"


class TestSyncLeads:
    def test_sync_multiple_sightings(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        _make_vendor_card(db_session)
        s1 = _make_sighting(db_session, req.id, requirement.id, vendor_name="Arrow Electronics")
        s2 = _make_sighting(db_session, req.id, requirement.id, vendor_name="Digi-Key", vendor_email="dk@digikey.com")

        count = sync_leads_for_sightings(db_session, requirement, [s1, s2])
        assert count == 2

    def test_sync_empty_list(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        assert sync_leads_for_sightings(db_session, requirement, []) == 0


class TestComputeVendorSafety:
    def test_with_good_vendor_card(self, db_session: Session):
        vc = _make_vendor_card(db_session)
        # Add business footprint data so the vendor scores as low_risk
        vc.hq_city = "Centennial"
        vc.hq_country = "US"
        vc.legal_name = "Arrow Electronics Inc."
        vc.total_wins = 5
        vc.relationship_months = 12
        db_session.flush()
        score, flags, summary = _compute_vendor_safety(vc, contactability=80.0)
        assert score > 50
        assert "lower risk" in summary.lower() or "Lower risk" in summary

    def test_with_no_vendor_card(self, db_session: Session):
        score, flags, summary = _compute_vendor_safety(None, contactability=80.0)
        assert "no_internal_vendor_profile" in flags
        assert "unknown" in summary.lower() or "Unknown" in summary

    def test_blacklisted_vendor(self, db_session: Session):
        vc = _make_vendor_card(db_session)
        vc.is_blacklisted = True
        db_session.flush()
        score, flags, summary = _compute_vendor_safety(vc, contactability=80.0)
        assert "internal_do_not_contact_history" in flags
        assert score < 50

    def test_low_contactability_penalized(self, db_session: Session):
        vc = _make_vendor_card(db_session)
        score_high, _, _ = _compute_vendor_safety(vc, contactability=80.0)
        score_low, flags, _ = _compute_vendor_safety(vc, contactability=20.0)
        assert score_low < score_high


class TestContactabilityScore:
    def test_email_and_phone(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        sighting = _make_sighting(db_session, req.id, requirement.id)
        score = _contactability_score(sighting, None)
        assert score >= 70  # email + phone

    def test_no_contact_info(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        sighting = _make_sighting(db_session, req.id, requirement.id, vendor_email=None, vendor_phone=None)
        score = _contactability_score(sighting, None)
        assert score == 0


class TestHistoricalSuccessScore:
    def test_with_vendor_card(self, db_session: Session):
        vc = _make_vendor_card(db_session)
        score = _historical_success_score(vc)
        assert score == 75.0  # vendor_score from card

    def test_without_vendor_card(self):
        assert _historical_success_score(None) == 45.0


class TestUpdateLeadStatus:
    def _setup_lead(self, db_session: Session) -> SourcingLead:
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        _make_vendor_card(db_session)
        sighting = _make_sighting(db_session, req.id, requirement.id)
        lead = upsert_lead_from_sighting(db_session, requirement, sighting)
        db_session.flush()
        db_session.commit()
        return lead

    def test_transition_to_contacted(self, db_session: Session):
        lead = self._setup_lead(db_session)
        result = update_lead_status(db_session, lead.id, "contacted", note="Called vendor")
        assert result is not None
        assert result.buyer_status == "contacted"

    def test_transition_to_has_stock_boosts_confidence(self, db_session: Session):
        lead = self._setup_lead(db_session)
        original_score = lead.confidence_score
        result = update_lead_status(db_session, lead.id, "has_stock")
        assert result.confidence_score >= original_score

    def test_transition_to_bad_lead_lowers_confidence(self, db_session: Session):
        lead = self._setup_lead(db_session)
        original_score = lead.confidence_score
        result = update_lead_status(db_session, lead.id, "bad_lead")
        assert result.confidence_score <= original_score

    def test_do_not_contact_lowers_safety(self, db_session: Session):
        lead = self._setup_lead(db_session)
        original_safety = lead.vendor_safety_score
        result = update_lead_status(db_session, lead.id, "do_not_contact")
        assert result.vendor_safety_score < original_safety
        assert "buyer_marked_do_not_contact" in result.vendor_safety_flags

    def test_invalid_status_raises(self, db_session: Session):
        lead = self._setup_lead(db_session)
        with pytest.raises(ValueError, match="Unsupported lead status"):
            update_lead_status(db_session, lead.id, "invalid_status")

    def test_nonexistent_lead_returns_none(self, db_session: Session):
        result = update_lead_status(db_session, 99999, "contacted")
        assert result is None

    def test_creates_feedback_event(self, db_session: Session):
        lead = self._setup_lead(db_session)
        update_lead_status(db_session, lead.id, "contacted", note="test note")
        events = db_session.query(LeadFeedbackEvent).filter(LeadFeedbackEvent.lead_id == lead.id).all()
        assert len(events) == 1
        assert events[0].status == "contacted"
        assert events[0].note == "test note"

    def test_resync_preserves_buyer_lowered_confidence(self, db_session: Session):
        """Regression: a buyer 'no_stock' outcome lowers confidence_score; re-syncing the
        same sighting must NOT restore the source-computed band and mask the outcome."""
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        _make_vendor_card(db_session)
        sighting = _make_sighting(db_session, req.id, requirement.id)

        lead = upsert_lead_from_sighting(db_session, requirement, sighting)
        db_session.flush()
        db_session.commit()
        source_computed = lead.confidence_score

        # Buyer marks the lead 'no_stock' — update_lead_status applies a -14 delta.
        lead = update_lead_status(db_session, lead.id, "no_stock")
        lowered = lead.confidence_score
        assert lowered < source_computed  # buyer feedback dropped confidence

        # Re-sight the same vendor/part — confidence must stay lowered, not be restored.
        resynced = upsert_lead_from_sighting(db_session, requirement, sighting)
        db_session.flush()
        assert resynced.buyer_status == "no_stock"
        assert resynced.confidence_score == lowered
        assert resynced.confidence_band == _confidence_band(lowered)

    def test_corroboration_bump_skips_buyer_touched_lead(self, db_session: Session):
        """The +5 corroboration bump (production _refresh_lead_evidence_rollups) must
        not touch a buyer-owned score — else a 'no_stock' lead climbs back to a high
        band and, since upsert now preserves the score, accumulates unbounded on every
        re-sync."""
        from app.models.sourcing_lead import LeadEvidence
        from app.services.sourcing_leads import _refresh_lead_evidence_rollups

        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        _make_vendor_card(db_session)
        sighting = _make_sighting(db_session, req.id, requirement.id)
        lead = upsert_lead_from_sighting(db_session, requirement, sighting)
        db_session.flush()
        db_session.commit()

        lead = update_lead_status(db_session, lead.id, "no_stock")
        lowered = lead.confidence_score

        # Two evidence rows in DISTINCT source categories (api + marketplace) → corroborated.
        for i, st in enumerate(("nexar", "brokerbin")):
            db_session.add(
                LeadEvidence(
                    evidence_id=f"ev_test_{i}",
                    lead_id=lead.id,
                    signal_type="stock_listing",
                    source_type=st,
                    source_name=st,
                )
            )
        db_session.flush()

        _refresh_lead_evidence_rollups(db_session, lead)

        assert lead.corroborated is True  # corroboration fired …
        assert lead.confidence_score == lowered  # … but the buyer-owned score was NOT bumped

    def test_corroboration_bump_still_applies_to_untouched_lead(self, db_session: Session):
        """A 'new' (untouched) corroborated lead still gets the +5 — the guard is buyer-
        only."""
        from app.models.sourcing_lead import LeadEvidence
        from app.services.sourcing_leads import _refresh_lead_evidence_rollups

        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        _make_vendor_card(db_session)
        sighting = _make_sighting(db_session, req.id, requirement.id)
        lead = upsert_lead_from_sighting(db_session, requirement, sighting)
        db_session.flush()
        before = float(lead.confidence_score)

        for i, st in enumerate(("nexar", "brokerbin")):
            db_session.add(
                LeadEvidence(
                    evidence_id=f"ev_new_{i}",
                    lead_id=lead.id,
                    signal_type="stock_listing",
                    source_type=st,
                    source_name=st,
                )
            )
        db_session.flush()

        _refresh_lead_evidence_rollups(db_session, lead)
        assert lead.corroborated is True
        assert float(lead.confidence_score) == min(before + 5.0, 100.0)  # +5 applied for 'new'

    def test_resync_untouched_lead_still_recomputes_confidence(self, db_session: Session):
        """A lead the buyer has NOT acted on (buyer_status == 'new') must still pick up
        a freshly source-computed confidence on re-sync — preservation is buyer-only."""
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        _make_vendor_card(db_session)
        # Stale sighting → lower freshness contribution.
        stale = _make_sighting(db_session, req.id, requirement.id)
        stale.created_at = datetime.now(timezone.utc) - timedelta(days=20)
        db_session.flush()
        lead = upsert_lead_from_sighting(db_session, requirement, stale)
        db_session.flush()
        stale_score = lead.confidence_score

        # Fresh re-sight of the same vendor/part while still 'new' → recompute upward.
        fresh = _make_sighting(db_session, req.id, requirement.id)
        resynced = upsert_lead_from_sighting(db_session, requirement, fresh)
        db_session.flush()
        assert resynced.id == lead.id
        assert resynced.buyer_status == "new"
        assert resynced.confidence_score >= stale_score


class TestAppendLeadFeedback:
    def test_append_feedback(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        sighting = _make_sighting(db_session, req.id, requirement.id)
        lead = upsert_lead_from_sighting(db_session, requirement, sighting)
        db_session.flush()
        db_session.commit()

        result = append_lead_feedback(db_session, lead.id, note="Follow-up needed")
        assert result is not None
        assert result.buyer_feedback_summary == "Follow-up needed"

    def test_nonexistent_lead(self, db_session: Session):
        result = append_lead_feedback(db_session, 99999, note="test")
        assert result is None


class TestGetRequisitionLeads:
    def test_returns_leads_for_requisition(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        _make_vendor_card(db_session)
        sighting = _make_sighting(db_session, req.id, requirement.id)
        sync_leads_for_sightings(db_session, requirement, [sighting])

        leads = get_requisition_leads(db_session, req.id)
        assert len(leads) == 1

    def test_filter_by_status(self, db_session: Session):
        req = _make_requisition(db_session)
        requirement = _make_requirement(db_session, req.id)
        sighting = _make_sighting(db_session, req.id, requirement.id)
        sync_leads_for_sightings(db_session, requirement, [sighting])

        leads = get_requisition_leads(db_session, req.id, statuses=["contacted"])
        assert len(leads) == 0  # all leads are "new"

    def test_empty_requisition(self, db_session: Session):
        leads = get_requisition_leads(db_session, 99999)
        assert len(leads) == 0


class TestBuyerStatuses:
    def test_all_statuses_valid(self):
        expected = {"new", "contacted", "replied", "no_stock", "has_stock", "bad_lead", "do_not_contact"}
        assert BUYER_STATUSES == expected
