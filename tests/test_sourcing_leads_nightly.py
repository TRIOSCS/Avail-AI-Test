"""test_sourcing_leads_nightly.py — Nightly coverage tests for sourcing_leads.py.

Targets lines: 101 (_source_reliability salesforce/avail_history), 122/126/128 (_freshness_score),
139 (_historical_success_score no vendor), 155 (blacklisted), 204-205/225-226/230-231
(_compute_vendor_safety flags), 530-548 (_auto_merge_leads), 573-611 (_count_dedup_signals),
808/812 (_propagate_outcome_to_vendor early returns).

Called by: pytest
Depends on: conftest.py (db_session), app/services/sourcing_leads.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock


def _now():
    return datetime.now(timezone.utc)


# ── _source_reliability (line 101) ───────────────────────────────────


class TestSourceReliability:
    def test_salesforce_base_85(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("salesforce", None)
        assert 80 <= score <= 93  # base 85 ± tier

    def test_avail_history_base_85(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("avail_history", None)
        assert 80 <= score <= 93

    def test_digikey_base_90(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("digikey", None)
        assert score >= 88

    def test_unknown_base_60(self):
        from app.services.sourcing_leads import _source_reliability

        score = _source_reliability("unknown_xyz", None)
        assert 55 <= score <= 65

    def test_t1_tier_boosts(self):
        from app.services.sourcing_leads import _source_reliability

        base = _source_reliability("brokerbin", None)
        t1 = _source_reliability("brokerbin", "T1")
        assert t1 > base


# ── _freshness_score (lines 122, 126, 128) ───────────────────────────


class TestFreshnessScore:
    def test_12_hours_returns_95(self):
        from app.services.sourcing_leads import _freshness_score

        assert _freshness_score(_now() - timedelta(hours=12)) == 95.0

    def test_23_hours_returns_95(self):
        from app.services.sourcing_leads import _freshness_score

        assert _freshness_score(_now() - timedelta(hours=23)) == 95.0

    def test_2_days_returns_85(self):
        from app.services.sourcing_leads import _freshness_score

        assert _freshness_score(_now() - timedelta(days=2)) == 85.0

    def test_5_days_returns_72(self):
        from app.services.sourcing_leads import _freshness_score

        assert _freshness_score(_now() - timedelta(days=5)) == 72.0

    def test_none_returns_45(self):
        from app.services.sourcing_leads import _freshness_score

        assert _freshness_score(None) == 45.0


# ── _historical_success_score (lines 139, 155) ────────────────────────


class TestHistoricalSuccessScore:
    def test_no_vendor_returns_45(self):
        from app.services.sourcing_leads import _historical_success_score

        assert _historical_success_score(None) == 45.0

    def test_blacklisted_penalized(self):
        from app.services.sourcing_leads import _historical_success_score

        normal = MagicMock(vendor_score=70, is_blacklisted=False, total_wins=0, ghost_rate=None, cancellation_rate=None)
        blacklisted = MagicMock(
            vendor_score=70, is_blacklisted=True, total_wins=0, ghost_rate=None, cancellation_rate=None
        )
        assert _historical_success_score(blacklisted) < _historical_success_score(normal)

    def test_vendor_score_used(self):
        from app.services.sourcing_leads import _historical_success_score

        v = MagicMock(vendor_score=80.0, is_blacklisted=False, total_wins=0, ghost_rate=None, cancellation_rate=None)
        score = _historical_success_score(v)
        assert 70 <= score <= 90


# ── _compute_vendor_safety (lines 204-205, 225-226, 230-231) ─────────


class TestComputeVendorSafety:
    def _make_vendor(self, **kwargs):
        defaults = dict(
            website="https://example.com",
            domain="example.com",
            hq_city="NY",
            hq_country="US",
            legal_name="Corp",
            emails=["s@example.com"],
            phones=["+1-555-0100"],
            is_new_vendor=False,
            sighting_count=5,
            vendor_score=None,
            is_blacklisted=False,
            ghost_rate=None,
            cancellation_rate=None,
            relationship_months=None,
            total_wins=0,
        )
        defaults.update(kwargs)
        return MagicMock(**defaults)

    def test_domain_but_no_website_limited_footprint(self):
        """Vendor has domain but no website → limited_business_footprint (204-205)."""
        from app.services.sourcing_leads import _compute_vendor_safety

        v = self._make_vendor(website=None)
        _, flags, _ = _compute_vendor_safety(v, 60.0)
        assert "limited_business_footprint" in flags

    def test_high_ghost_rate_flag(self):
        """ghost_rate > 0.5 → repeated_bad_feedback (225-226)."""
        from app.services.sourcing_leads import _compute_vendor_safety

        v = self._make_vendor(ghost_rate=0.7)
        _, flags, _ = _compute_vendor_safety(v, 60.0)
        assert "repeated_bad_feedback" in flags

    def test_high_cancellation_rate_flag(self):
        """cancellation_rate > 0.2 → high_cancellation_rate (230-231)."""
        from app.services.sourcing_leads import _compute_vendor_safety

        v = self._make_vendor(cancellation_rate=0.4)
        _, flags, _ = _compute_vendor_safety(v, 60.0)
        assert "high_cancellation_rate" in flags

    def test_no_vendor_returns_unknown_flags(self):
        """No vendor card → no_internal_vendor_profile flag."""
        from app.services.sourcing_leads import _compute_vendor_safety

        _, flags, _ = _compute_vendor_safety(None, 60.0)
        assert "no_internal_vendor_profile" in flags


# ── Helpers for DB tests ──────────────────────────────────────────────


def _make_req_and_requirement(db):
    import uuid

    from app.models import Requisition, User
    from app.models.sourcing import Requirement

    u = User(
        email=f"u{uuid.uuid4().hex[:6]}@x.com", name="T", role="buyer", azure_id=uuid.uuid4().hex, created_at=_now()
    )
    db.add(u)
    db.flush()
    req = Requisition(name="R", status="active", created_by=u.id)
    db.add(req)
    db.flush()
    requirement = Requirement(requisition_id=req.id, primary_mpn="ABC123")
    db.add(requirement)
    db.flush()
    return req, requirement


def _make_lead(db, req_id, requirement_id, buyer_status="new", vendor_suffix=""):
    import uuid

    from app.models.sourcing_lead import SourcingLead

    uid = uuid.uuid4().hex[:8]
    name = f"TestVendor{vendor_suffix or uid}"
    lead = SourcingLead(
        lead_id=f"L-{uid}",
        requisition_id=req_id,
        requirement_id=requirement_id,
        vendor_name=name,
        vendor_name_normalized=name.lower(),
        part_number_requested="ABC123",
        part_number_matched="ABC123",
        primary_source_type="brokerbin",
        primary_source_name="BrokerBin",
        buyer_status=buyer_status,
        match_type="exact",
    )
    db.add(lead)
    db.flush()
    return lead


# ── _auto_merge_leads (lines 530-548) ────────────────────────────────


class TestAutoMergeLeads:
    def test_buyer_acted_flags_not_merged(self, db_session):
        """duplicate.buyer_status != 'new' → flags both, no deletion (530-534)."""
        from app.models.sourcing_lead import SourcingLead
        from app.services.sourcing_leads import _auto_merge_leads

        req, requirement = _make_req_and_requirement(db_session)
        survivor = _make_lead(db_session, req.id, requirement.id, "new")
        duplicate = _make_lead(db_session, req.id, requirement.id, "contacted")

        _auto_merge_leads(db_session, survivor, duplicate)

        still = db_session.query(SourcingLead).filter_by(id=duplicate.id).first()
        assert still is not None
        assert "duplicate_candidate" in (survivor.risk_flags or [])
        assert "duplicate_candidate" in (duplicate.risk_flags or [])

    def test_new_status_merges_duplicate(self, db_session):
        """duplicate.buyer_status == 'new' → duplicate deleted (536-548)."""
        from app.models.sourcing_lead import SourcingLead
        from app.services.sourcing_leads import _auto_merge_leads

        req, requirement = _make_req_and_requirement(db_session)
        survivor = _make_lead(db_session, req.id, requirement.id, "new")
        duplicate = _make_lead(db_session, req.id, requirement.id, "new")

        _auto_merge_leads(db_session, survivor, duplicate)
        db_session.flush()

        deleted = db_session.query(SourcingLead).filter_by(id=duplicate.id).first()
        assert deleted is None


# ── _count_dedup_signals (lines 573-611) ─────────────────────────────


class TestCountDedupSignals:
    def test_shared_vendor_card_id_strong_signal(self, db_session):
        """Same vendor_card_id → signals >= 2 (line 572-573)."""
        from app.models import VendorCard
        from app.services.sourcing_leads import _count_dedup_signals

        vendor = VendorCard(normalized_name="acme-test", display_name="Acme", domain="acme.com", emails=["a@acme.com"])
        db_session.add(vendor)
        db_session.flush()

        req, requirement = _make_req_and_requirement(db_session)
        lead = _make_lead(db_session, req.id, requirement.id)
        other = _make_lead(db_session, req.id, requirement.id)
        lead.vendor_card_id = vendor.id
        other.vendor_card_id = vendor.id
        db_session.flush()

        signals = _count_dedup_signals(db_session, lead, other, vendor)
        assert signals >= 2

    def test_shared_domain_adds_signal(self, db_session):
        """Different vendor cards with same domain → signals >= 1 (583-587)."""
        from app.models import VendorCard
        from app.services.sourcing_leads import _count_dedup_signals

        v1 = VendorCard(normalized_name="v1-shared", display_name="V1", domain="shared.com", emails=["a@shared.com"])
        v2 = VendorCard(normalized_name="v2-shared", display_name="V2", domain="shared.com", emails=["b@shared.com"])
        db_session.add_all([v1, v2])
        db_session.flush()

        req, requirement = _make_req_and_requirement(db_session)
        lead = _make_lead(db_session, req.id, requirement.id)
        other = _make_lead(db_session, req.id, requirement.id)
        lead.vendor_card_id = v1.id
        other.vendor_card_id = v2.id
        db_session.flush()

        signals = _count_dedup_signals(db_session, lead, other, v1)
        assert signals >= 1

    def test_shared_phone_adds_signal(self, db_session):
        """Same phone number on two vendor cards → +1 signal (590-596)."""
        from app.models import VendorCard
        from app.services.sourcing_leads import _count_dedup_signals

        v1 = VendorCard(normalized_name="vp1", display_name="VP1", phones=["+15559999"])
        v2 = VendorCard(normalized_name="vp2", display_name="VP2", phones=["+15559999"])
        db_session.add_all([v1, v2])
        db_session.flush()

        req, requirement = _make_req_and_requirement(db_session)
        lead = _make_lead(db_session, req.id, requirement.id)
        other = _make_lead(db_session, req.id, requirement.id)
        lead.vendor_card_id = v1.id
        other.vendor_card_id = v2.id
        db_session.flush()

        signals = _count_dedup_signals(db_session, lead, other, v1)
        assert signals >= 1

    def test_shared_email_domain_adds_signal(self, db_session):
        """Same email domain on two vendor cards → +1 signal (599-611)."""
        from app.models import VendorCard
        from app.services.sourcing_leads import _count_dedup_signals

        v1 = VendorCard(normalized_name="ve1", display_name="VE1", emails=["sales@common.com"])
        v2 = VendorCard(normalized_name="ve2", display_name="VE2", emails=["support@common.com"])
        db_session.add_all([v1, v2])
        db_session.flush()

        req, requirement = _make_req_and_requirement(db_session)
        lead = _make_lead(db_session, req.id, requirement.id)
        other = _make_lead(db_session, req.id, requirement.id)
        lead.vendor_card_id = v1.id
        other.vendor_card_id = v2.id
        db_session.flush()

        signals = _count_dedup_signals(db_session, lead, other, v1)
        assert signals >= 1


# ── _propagate_outcome_to_vendor (lines 808, 812) ────────────────────


class TestPropagateOutcomeToVendor:
    def test_no_vendor_card_id_returns_early(self, db_session):
        """vendor_card_id is None → no crash, early return (808)."""
        from app.services.sourcing_leads import _propagate_outcome_to_vendor

        req, requirement = _make_req_and_requirement(db_session)
        lead = _make_lead(db_session, req.id, requirement.id)
        lead.vendor_card_id = None
        _propagate_outcome_to_vendor(db_session, lead, "has_stock")

    def test_vendor_card_missing_returns_early(self, db_session):
        """vendor_card_id set but DB has no record → no crash, early return (812)."""
        from app.services.sourcing_leads import _propagate_outcome_to_vendor

        req, requirement = _make_req_and_requirement(db_session)
        lead = _make_lead(db_session, req.id, requirement.id)
        lead.vendor_card_id = 999999  # Non-existent
        _propagate_outcome_to_vendor(db_session, lead, "bad_lead")
