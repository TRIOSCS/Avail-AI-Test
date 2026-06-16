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

import pytest

# ── Seniority Classification ────────────────────────────────────────


class TestClassifyContactSeniority:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("VP of Procurement", "decision_maker"),
            ("Vice President, Supply Chain", "decision_maker"),
            ("Director of Sourcing", "decision_maker"),
            ("Sr. Dir. Global Procurement", "decision_maker"),
            ("SVP Operations", "decision_maker"),
            ("Chief Procurement Officer", "decision_maker"),
            ("Head of Purchasing", "decision_maker"),
            ("General Manager, Procurement", "decision_maker"),
            ("Procurement Manager", "influencer"),
            ("Senior Buyer", "influencer"),
            ("Lead Component Engineer", "influencer"),
            ("Commodity Manager - Electronics", "influencer"),
            ("Buyer", "executor"),
            ("Purchasing Agent", "executor"),
            ("Supply Chain Coordinator", "executor"),
            ("Procurement Analyst", "executor"),
            ("Software Engineer", "other"),
            ("", "other"),
            (None, "other"),
            ("CPO", "decision_maker"),
        ],
        ids=[
            "vp",
            "vice_president",
            "director",
            "sr_director",
            "svp",
            "chief",
            "head_of",
            "general_manager",
            "manager",
            "senior",
            "lead",
            "commodity_manager",
            "buyer",
            "purchasing_agent",
            "coordinator",
            "analyst",
            "other",
            "empty",
            "none",
            "cpo",
        ],
    )
    def test_classify(self, title, expected):
        from app.services.prospect_contacts import classify_contact_seniority

        assert classify_contact_seniority(title) == expected


# ── Email Masking ────────────────────────────────────────────────────


class TestMaskEmail:
    @pytest.mark.parametrize(
        ("email", "expected"),
        [
            ("john.smith@company.com", "j***@comp..."),
            ("a@b.co", "a***@b.co"),
            ("", ""),
            (None, ""),
            ("notanemail", ""),
        ],
        ids=["standard_email", "short_domain", "empty", "none", "no_at_sign"],
    )
    def test_mask(self, email, expected):
        from app.services.prospect_contacts import mask_email

        assert mask_email(email) == expected


# ── Personal Email Filter ────────────────────────────────────────────


class TestPersonalEmailFilter:
    @pytest.mark.parametrize(
        ("email", "expected"),
        [
            ("john@gmail.com", True),
            ("jane@yahoo.com", True),
            ("john@raytheon.com", False),
            ("", False),
            ("user@hotmail.com", True),
            ("user@outlook.com", True),
        ],
        ids=["gmail", "yahoo", "corporate", "empty", "hotmail", "outlook"],
    )
    def test_is_personal(self, email, expected):
        from app.services.prospect_contacts import _is_personal_email

        assert _is_personal_email(email) is expected


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
