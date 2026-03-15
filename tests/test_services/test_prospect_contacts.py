"""Tests for prospect contact utility functions.

All external API calls are mocked. Tests cover:
- Email masking logic
- Seniority classification with real title variations
- Personal email filtering
- New hire detection
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timedelta, timezone

# ── Seniority Classification ────────────────────────────────────────


class TestClassifyContactSeniority:
    def test_vp(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("VP of Procurement") == "decision_maker"

    def test_vice_president(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Vice President, Supply Chain") == "decision_maker"

    def test_director(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Director of Sourcing") == "decision_maker"

    def test_sr_director(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Sr. Dir. Global Procurement") == "decision_maker"

    def test_svp(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("SVP Operations") == "decision_maker"

    def test_chief(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Chief Procurement Officer") == "decision_maker"

    def test_head_of(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Head of Purchasing") == "decision_maker"

    def test_general_manager(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("General Manager, Procurement") == "decision_maker"

    def test_manager(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Procurement Manager") == "influencer"

    def test_senior(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Senior Buyer") == "influencer"

    def test_lead(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Lead Component Engineer") == "influencer"

    def test_commodity_manager(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Commodity Manager - Electronics") == "influencer"

    def test_buyer(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Buyer") == "executor"

    def test_purchasing_agent(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Purchasing Agent") == "executor"

    def test_coordinator(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Supply Chain Coordinator") == "executor"

    def test_analyst(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Procurement Analyst") == "executor"

    def test_other(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("Software Engineer") == "other"

    def test_empty(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("") == "other"

    def test_none(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority(None) == "other"

    def test_cpo(self):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority("CPO") == "decision_maker"


# ── Email Masking ────────────────────────────────────────────────────


class TestMaskEmail:
    def test_standard_email(self):
        from app.services.prospect_contacts import mask_email

        result = mask_email("john.smith@company.com")
        assert result == "j***@comp..."

    def test_short_domain(self):
        from app.services.prospect_contacts import mask_email

        result = mask_email("a@b.co")
        assert result == "a***@b.co"

    def test_empty(self):
        from app.services.prospect_contacts import mask_email

        assert mask_email("") == ""

    def test_none(self):
        from app.services.prospect_contacts import mask_email

        assert mask_email(None) == ""

    def test_no_at_sign(self):
        from app.services.prospect_contacts import mask_email

        assert mask_email("notanemail") == ""


# ── Personal Email Filter ────────────────────────────────────────────


class TestPersonalEmailFilter:
    def test_gmail(self):
        from app.services.prospect_contacts import _is_personal_email

        assert _is_personal_email("john@gmail.com") is True

    def test_yahoo(self):
        from app.services.prospect_contacts import _is_personal_email

        assert _is_personal_email("jane@yahoo.com") is True

    def test_corporate(self):
        from app.services.prospect_contacts import _is_personal_email

        assert _is_personal_email("john@raytheon.com") is False

    def test_empty(self):
        from app.services.prospect_contacts import _is_personal_email

        assert _is_personal_email("") is False

    def test_hotmail(self):
        from app.services.prospect_contacts import _is_personal_email

        assert _is_personal_email("user@hotmail.com") is True

    def test_outlook(self):
        from app.services.prospect_contacts import _is_personal_email

        assert _is_personal_email("user@outlook.com") is True


# ── New Hire Detection ───────────────────────────────────────────────


class TestNewHireDetection:
    def test_recent_start(self):
        from app.services.prospect_contacts import _is_new_hire

        recent = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        assert _is_new_hire(recent) is True

    def test_old_start(self):
        from app.services.prospect_contacts import _is_new_hire

        old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        assert _is_new_hire(old) is False

    def test_none(self):
        from app.services.prospect_contacts import _is_new_hire

        assert _is_new_hire(None) is False

    def test_invalid_date(self):
        from app.services.prospect_contacts import _is_new_hire

        assert _is_new_hire("not-a-date") is False
