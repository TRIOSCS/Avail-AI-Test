"""Tests for Phase 5 — contact enrichment (Apollo people + Hunter verify).

All external API calls are mocked. Tests cover:
- Email masking logic
- Seniority classification with real title variations
- Personal email filtering
- Hunter verification + caching
- Credit limit tracking
- New hire detection
- Prospect contact enrichment orchestration
- Batch processing with credit exhaustion
- Domain pattern detection
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.prospect_account import ProspectAccount


# ── Helpers ──────────────────────────────────────────────────────────


def _make_prospect(db: Session, **overrides) -> ProspectAccount:
    defaults = {
        "name": "Test Corp",
        "domain": "testcorp.com",
        "industry": "Aerospace & Defense",
        "discovery_source": "explorium",
        "status": "suggested",
        "fit_score": 70,
        "readiness_score": 50,
        "enrichment_data": {},
    }
    defaults.update(overrides)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


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


# ── Credit Tracker ───────────────────────────────────────────────────


class TestCreditTracker:
    def test_initial_state(self):
        from app.services.prospect_contacts import CreditTracker

        t = CreditTracker(apollo_limit=100, hunter_search_limit=25, hunter_verify_limit=50)
        assert t.can_use_apollo() is True
        assert t.can_use_hunter_search() is True
        assert t.can_use_hunter_verify() is True

    def test_apollo_exhaustion(self):
        from app.services.prospect_contacts import CreditTracker

        t = CreditTracker(apollo_limit=2)
        t.use_apollo(2)
        assert t.can_use_apollo() is False

    def test_hunter_verify_exhaustion(self):
        from app.services.prospect_contacts import CreditTracker

        t = CreditTracker(hunter_verify_limit=3)
        t.use_hunter_verify(3)
        assert t.can_use_hunter_verify() is False

    def test_hunter_search_exhaustion(self):
        from app.services.prospect_contacts import CreditTracker

        t = CreditTracker(hunter_search_limit=1)
        t.use_hunter_search(1)
        assert t.can_use_hunter_search() is False

    def test_summary(self):
        from app.services.prospect_contacts import CreditTracker

        t = CreditTracker()
        t.use_apollo(5)
        t.use_hunter_search(2)
        t.use_hunter_verify(10)
        s = t.summary()
        assert s["apollo_credits_used"] == 5
        assert s["hunter_searches_used"] == 2
        assert s["hunter_verifications_used"] == 10


# ── Apollo Contact Search ────────────────────────────────────────────


class TestSearchContactsApollo:
    @patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock)
    def test_returns_normalized_contacts(self, mock_search, db_session):
        from app.services.prospect_contacts import search_contacts_apollo

        mock_search.return_value = [
            {
                "full_name": "Jane Doe",
                "title": "VP of Procurement",
                "email": "Jane.Doe@Company.COM",
                "linkedin_url": "https://linkedin.com/in/janedoe",
                "started_current_role_at": None,
            },
        ]

        result = asyncio.get_event_loop().run_until_complete(
            search_contacts_apollo("company.com")
        )

        assert len(result) == 1
        assert result[0]["name"] == "Jane Doe"
        assert result[0]["email"] == "jane.doe@company.com"  # normalized lowercase
        assert result[0]["seniority_level"] == "decision_maker"

    @patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock)
    def test_filters_personal_emails(self, mock_search, db_session):
        from app.services.prospect_contacts import search_contacts_apollo

        mock_search.return_value = [
            {
                "full_name": "Personal Pete",
                "title": "Buyer",
                "email": "pete@gmail.com",
                "linkedin_url": None,
            },
        ]

        result = asyncio.get_event_loop().run_until_complete(
            search_contacts_apollo("company.com")
        )

        assert result[0]["email"] is None  # personal email filtered

    @patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock)
    def test_handles_empty_results(self, mock_search, db_session):
        from app.services.prospect_contacts import search_contacts_apollo

        mock_search.return_value = []

        result = asyncio.get_event_loop().run_until_complete(
            search_contacts_apollo("nocorp.com")
        )

        assert result == []

    @patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock)
    def test_handles_missing_email(self, mock_search, db_session):
        from app.services.prospect_contacts import search_contacts_apollo

        mock_search.return_value = [
            {"full_name": "No Email", "title": "Manager", "email": None},
        ]

        result = asyncio.get_event_loop().run_until_complete(
            search_contacts_apollo("company.com")
        )

        assert result[0]["email"] is None


# ── Hunter Email Verification ───────────────────────────────────────


class TestVerifyEmailHunter:
    @patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock)
    def test_valid_email(self, mock_verify, db_session):
        from app.services.prospect_contacts import verify_email_hunter

        mock_verify.return_value = {"email": "test@co.com", "status": "valid", "score": 95, "sources": 3}

        result = asyncio.get_event_loop().run_until_complete(
            verify_email_hunter("test@co.com")
        )

        assert result["verified"] is True
        assert result["status"] == "valid"

    @patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock)
    def test_invalid_email(self, mock_verify, db_session):
        from app.services.prospect_contacts import verify_email_hunter

        mock_verify.return_value = {"email": "bad@co.com", "status": "invalid", "score": 10}

        result = asyncio.get_event_loop().run_until_complete(
            verify_email_hunter("bad@co.com")
        )

        assert result["verified"] is False

    @patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock)
    def test_accept_all_high_score(self, mock_verify, db_session):
        from app.services.prospect_contacts import verify_email_hunter

        mock_verify.return_value = {"email": "test@co.com", "status": "accept_all", "score": 90}

        result = asyncio.get_event_loop().run_until_complete(
            verify_email_hunter("test@co.com")
        )

        assert result["verified"] is True

    @patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock)
    def test_accept_all_low_score(self, mock_verify, db_session):
        from app.services.prospect_contacts import verify_email_hunter

        mock_verify.return_value = {"email": "test@co.com", "status": "accept_all", "score": 50}

        result = asyncio.get_event_loop().run_until_complete(
            verify_email_hunter("test@co.com")
        )

        assert result["verified"] is False

    @patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock)
    def test_hunter_unavailable(self, mock_verify, db_session):
        from app.services.prospect_contacts import verify_email_hunter

        mock_verify.return_value = None

        result = asyncio.get_event_loop().run_until_complete(
            verify_email_hunter("test@co.com")
        )

        assert result["verified"] is False
        assert result["status"] == "unknown"

    @patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock)
    def test_caching(self, mock_verify, db_session):
        from app.services.prospect_contacts import verify_email_hunter

        mock_verify.return_value = {"email": "test@co.com", "status": "valid", "score": 95}
        cache = {}

        # First call — API called
        asyncio.get_event_loop().run_until_complete(
            verify_email_hunter("test@co.com", verification_cache=cache)
        )
        assert mock_verify.call_count == 1

        # Second call — cached
        result = asyncio.get_event_loop().run_until_complete(
            verify_email_hunter("test@co.com", verification_cache=cache)
        )
        assert mock_verify.call_count == 1  # not called again
        assert result["verified"] is True

    def test_empty_email(self, db_session):
        from app.services.prospect_contacts import verify_email_hunter

        result = asyncio.get_event_loop().run_until_complete(
            verify_email_hunter("")
        )
        assert result["verified"] is False


# ── Domain Pattern Detection ─────────────────────────────────────────


class TestGetDomainPattern:
    @patch("app.connectors.hunter_client.find_domain_emails", new_callable=AsyncMock)
    def test_detects_first_dot_last(self, mock_find, db_session):
        from app.services.prospect_contacts import get_domain_pattern_hunter

        mock_find.return_value = [
            {"email": "john.smith@co.com", "first_name": "John", "last_name": "Smith"},
            {"email": "jane.doe@co.com", "first_name": "Jane", "last_name": "Doe"},
        ]

        result = asyncio.get_event_loop().run_until_complete(
            get_domain_pattern_hunter("co.com")
        )

        assert result == "{first}.{last}"

    @patch("app.connectors.hunter_client.find_domain_emails", new_callable=AsyncMock)
    def test_detects_f_last(self, mock_find, db_session):
        from app.services.prospect_contacts import get_domain_pattern_hunter

        mock_find.return_value = [
            {"email": "jsmith@co.com", "first_name": "John", "last_name": "Smith"},
            {"email": "jdoe@co.com", "first_name": "Jane", "last_name": "Doe"},
        ]

        result = asyncio.get_event_loop().run_until_complete(
            get_domain_pattern_hunter("co.com")
        )

        assert result == "{f}{last}"

    @patch("app.connectors.hunter_client.find_domain_emails", new_callable=AsyncMock)
    def test_no_contacts(self, mock_find, db_session):
        from app.services.prospect_contacts import get_domain_pattern_hunter

        mock_find.return_value = []

        result = asyncio.get_event_loop().run_until_complete(
            get_domain_pattern_hunter("empty.com")
        )

        assert result is None

    def test_empty_domain(self, db_session):
        from app.services.prospect_contacts import get_domain_pattern_hunter

        result = asyncio.get_event_loop().run_until_complete(
            get_domain_pattern_hunter("")
        )

        assert result is None


# ── Prospect Contact Enrichment Orchestrator ─────────────────────────


class TestEnrichProspectContacts:
    @patch("app.connectors.hunter_client.find_domain_emails", new_callable=AsyncMock)
    @patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock)
    @patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock)
    def test_full_enrichment(self, mock_apollo, mock_verify, mock_pattern, db_session):
        from app.services.prospect_contacts import enrich_prospect_contacts

        p = _make_prospect(db_session, domain="raytheon.com")

        mock_apollo.return_value = [
            {
                "full_name": "Jane VP",
                "title": "VP Procurement",
                "email": "jane.vp@raytheon.com",
                "linkedin_url": "https://linkedin.com/in/janevp",
            },
            {
                "full_name": "Bob Buyer",
                "title": "Buyer",
                "email": "bob@raytheon.com",
                "linkedin_url": None,
            },
        ]

        mock_verify.side_effect = [
            {"email": "jane.vp@raytheon.com", "status": "valid", "score": 95},
            {"email": "bob@raytheon.com", "status": "invalid", "score": 10},
        ]

        mock_pattern.return_value = [
            {"email": "jane.vp@raytheon.com", "first_name": "Jane", "last_name": "VP"},
        ]

        result = asyncio.get_event_loop().run_until_complete(
            enrich_prospect_contacts(p.id, db_session)
        )

        assert result["total_found"] == 2
        assert result["verified"] == 1  # only jane passes
        assert result["decision_makers"] == 1  # jane is VP

        db_session.refresh(p)
        # Preview has masked emails
        assert len(p.contacts_preview) == 2
        assert "***" in p.contacts_preview[0]["email_masked"]
        # Full contacts stored in enrichment_data
        assert len(p.enrichment_data["contacts_full"]) == 2
        assert p.enrichment_data["contacts_full"][0]["email"] == "jane.vp@raytheon.com"

    def test_nonexistent_prospect(self, db_session):
        from app.services.prospect_contacts import enrich_prospect_contacts

        result = asyncio.get_event_loop().run_until_complete(
            enrich_prospect_contacts(99999, db_session)
        )

        assert result["total_found"] == 0

    @patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock)
    def test_no_contacts_found(self, mock_apollo, db_session):
        from app.services.prospect_contacts import enrich_prospect_contacts

        p = _make_prospect(db_session, domain="empty.com")
        mock_apollo.return_value = []

        result = asyncio.get_event_loop().run_until_complete(
            enrich_prospect_contacts(p.id, db_session)
        )

        assert result["total_found"] == 0

    @patch("app.services.prospect_contacts.search_contacts_apollo", new_callable=AsyncMock)
    def test_apollo_credit_exhausted(self, mock_search, db_session):
        from app.services.prospect_contacts import CreditTracker, enrich_prospect_contacts

        p = _make_prospect(db_session, domain="creditless.com")
        tracker = CreditTracker(apollo_limit=0)

        result = asyncio.get_event_loop().run_until_complete(
            enrich_prospect_contacts(p.id, db_session, credit_tracker=tracker)
        )

        assert result["total_found"] == 0
        mock_search.assert_not_called()

    @patch("app.services.prospect_contacts.verify_email_hunter", new_callable=AsyncMock)
    @patch("app.services.prospect_contacts.search_contacts_apollo", new_callable=AsyncMock)
    def test_hunter_verify_exhausted(self, mock_search, mock_verify, db_session):
        from app.services.prospect_contacts import CreditTracker, enrich_prospect_contacts

        p = _make_prospect(db_session, domain="noverify.com")
        tracker = CreditTracker(hunter_verify_limit=0, hunter_search_limit=0)

        mock_search.return_value = [
            {"name": "Test User", "title": "Buyer", "email": "test@noverify.com"},
        ]

        result = asyncio.get_event_loop().run_until_complete(
            enrich_prospect_contacts(p.id, db_session, credit_tracker=tracker)
        )

        # Contact found but not verified (limit exhausted)
        assert result["total_found"] == 1
        assert result["verified"] == 0
        mock_verify.assert_not_called()

    @patch("app.connectors.hunter_client.find_domain_emails", new_callable=AsyncMock)
    @patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock)
    @patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock)
    def test_existing_verification_cache_used(self, mock_apollo, mock_verify, mock_pattern, db_session):
        from app.services.prospect_contacts import enrich_prospect_contacts

        p = _make_prospect(
            db_session,
            domain="cached.com",
            enrichment_data={
                "email_verifications": {
                    "cached@cached.com": {
                        "email": "cached@cached.com",
                        "status": "valid",
                        "score": 95,
                        "verified": True,
                    }
                }
            },
        )

        mock_apollo.return_value = [
            {"full_name": "Cached Person", "title": "Manager", "email": "cached@cached.com"},
        ]
        mock_pattern.return_value = []

        result = asyncio.get_event_loop().run_until_complete(
            enrich_prospect_contacts(p.id, db_session)
        )

        assert result["verified"] == 1
        # Hunter verify should NOT have been called — cache hit
        mock_verify.assert_not_called()


# ── Batch Processing ────────────────────────────────────────────────


class TestRunContactEnrichmentBatch:
    @patch("app.services.prospect_contacts.enrich_prospect_contacts", new_callable=AsyncMock)
    @patch("app.services.prospect_contacts.asyncio.sleep", new_callable=AsyncMock)
    def test_batch_processes_qualifying(self, mock_sleep, mock_enrich, db_session):
        from app.services.prospect_contacts import run_contact_enrichment_batch

        _make_prospect(db_session, domain="high.com", fit_score=80)
        _make_prospect(db_session, domain="low.com", fit_score=30)  # below threshold

        mock_enrich.return_value = {
            "total_found": 3, "verified": 2, "decision_makers": 1, "new_hires": 0,
        }

        with patch("app.database.SessionLocal", return_value=db_session):
            result = asyncio.get_event_loop().run_until_complete(
                run_contact_enrichment_batch(min_fit_score=60)
            )

        assert result["prospects_processed"] == 1
        assert result["total_contacts_found"] == 3

    @patch("app.services.prospect_contacts.enrich_prospect_contacts", new_callable=AsyncMock)
    @patch("app.services.prospect_contacts.asyncio.sleep", new_callable=AsyncMock)
    def test_batch_skips_already_enriched(self, mock_sleep, mock_enrich, db_session):
        from app.services.prospect_contacts import run_contact_enrichment_batch

        _make_prospect(
            db_session,
            domain="done.com",
            fit_score=80,
            enrichment_data={"contacts_full": [{"name": "Already Done"}]},
        )

        with patch("app.database.SessionLocal", return_value=db_session):
            result = asyncio.get_event_loop().run_until_complete(
                run_contact_enrichment_batch(min_fit_score=60)
            )

        assert result["skipped_already_enriched"] == 1
        assert result["prospects_processed"] == 0
        mock_enrich.assert_not_called()

    @patch("app.services.prospect_contacts.enrich_prospect_contacts", new_callable=AsyncMock)
    @patch("app.services.prospect_contacts.asyncio.sleep", new_callable=AsyncMock)
    def test_batch_handles_errors(self, mock_sleep, mock_enrich, db_session):
        from app.services.prospect_contacts import run_contact_enrichment_batch

        _make_prospect(db_session, domain="error.com", fit_score=80)
        mock_enrich.side_effect = Exception("API error")

        with patch("app.database.SessionLocal", return_value=db_session):
            result = asyncio.get_event_loop().run_until_complete(
                run_contact_enrichment_batch(min_fit_score=60)
            )

        assert result["errors"] == 1


# ── Coverage Gap Tests ──────────────────────────────────────────────


class TestDomainPatternCoverageGaps:
    """Cover email pattern detection branches."""

    @pytest.mark.asyncio
    async def test_pattern_f_last(self):
        """Line 315-318: {f}{last} and {f}.{last} patterns."""
        from app.services.prospect_contacts import get_domain_pattern_hunter

        contacts = [
            {"email": "jsmith@corp.com", "first_name": "John", "last_name": "Smith"},
        ]

        with patch("app.connectors.hunter_client.find_domain_emails",
                   new_callable=AsyncMock, return_value=contacts):
            pattern = await get_domain_pattern_hunter("corp.com")

        assert pattern == "{f}{last}"

    @pytest.mark.asyncio
    async def test_pattern_f_dot_last(self):
        """Lines 318-319: {f}.{last} pattern."""
        from app.services.prospect_contacts import get_domain_pattern_hunter

        contacts = [
            {"email": "j.smith@corp.com", "first_name": "John", "last_name": "Smith"},
        ]

        with patch("app.connectors.hunter_client.find_domain_emails",
                   new_callable=AsyncMock, return_value=contacts):
            pattern = await get_domain_pattern_hunter("corp.com")

        assert pattern == "{f}.{last}"

    @pytest.mark.asyncio
    async def test_pattern_first_underscore_last(self):
        """Lines 320-321: {first}_{last} pattern."""
        from app.services.prospect_contacts import get_domain_pattern_hunter

        contacts = [
            {"email": "john_smith@corp.com", "first_name": "John", "last_name": "Smith"},
        ]

        with patch("app.connectors.hunter_client.find_domain_emails",
                   new_callable=AsyncMock, return_value=contacts):
            pattern = await get_domain_pattern_hunter("corp.com")

        assert pattern == "{first}_{last}"

    @pytest.mark.asyncio
    async def test_pattern_last_dot_first(self):
        """Lines 322-323: {last}.{first} pattern."""
        from app.services.prospect_contacts import get_domain_pattern_hunter

        contacts = [
            {"email": "smith.john@corp.com", "first_name": "John", "last_name": "Smith"},
        ]

        with patch("app.connectors.hunter_client.find_domain_emails",
                   new_callable=AsyncMock, return_value=contacts):
            pattern = await get_domain_pattern_hunter("corp.com")

        assert pattern == "{last}.{first}"

    @pytest.mark.asyncio
    async def test_pattern_first_only(self):
        """Lines 324-325: {first} pattern."""
        from app.services.prospect_contacts import get_domain_pattern_hunter

        contacts = [
            {"email": "john@corp.com", "first_name": "John", "last_name": "Smith"},
        ]

        with patch("app.connectors.hunter_client.find_domain_emails",
                   new_callable=AsyncMock, return_value=contacts):
            pattern = await get_domain_pattern_hunter("corp.com")

        assert pattern == "{first}"

    @pytest.mark.asyncio
    async def test_no_pattern_detected(self):
        """Line 333: returns None when no pattern detected."""
        from app.services.prospect_contacts import get_domain_pattern_hunter

        contacts = [
            {"email": "xq7@corp.com", "first_name": "John", "last_name": "Smith"},
        ]

        with patch("app.connectors.hunter_client.find_domain_emails",
                   new_callable=AsyncMock, return_value=contacts):
            pattern = await get_domain_pattern_hunter("corp.com")

        assert pattern is None

    @pytest.mark.asyncio
    async def test_missing_email_or_name_skipped(self):
        """Line 308: contacts without email/first/last are skipped."""
        from app.services.prospect_contacts import get_domain_pattern_hunter

        contacts = [
            {"email": None, "first_name": "John", "last_name": "Smith"},
            {"email": "john@corp.com", "first_name": "", "last_name": "Smith"},
            {"email": "john.smith@corp.com", "first_name": "John", "last_name": "Smith"},
        ]

        with patch("app.connectors.hunter_client.find_domain_emails",
                   new_callable=AsyncMock, return_value=contacts):
            pattern = await get_domain_pattern_hunter("corp.com")

        assert pattern == "{first}.{last}"


class TestEnrichContactsCoverageGaps:
    """Cover enrichment orchestration gaps."""

    @pytest.mark.asyncio
    async def test_new_hire_counted(self, db_session):
        """Line 427: new_hires counter incremented."""
        from app.services.prospect_contacts import enrich_prospect_contacts, CreditTracker

        recent_start = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        prospect = _make_prospect(db_session, domain="newhire.com")

        contacts = [
            {
                "name": "New Hire",
                "email": "new@newhire.com", "title": "Buyer",
                "linkedin_url": None,
                "seniority_level": "executor",
                "started_current_role_at": recent_start,
            },
        ]

        tracker = CreditTracker(apollo_limit=100, hunter_search_limit=25, hunter_verify_limit=50)

        with patch("app.services.prospect_contacts.search_contacts_apollo",
                   new_callable=AsyncMock, return_value=contacts):
            with patch("app.services.prospect_contacts.verify_email_hunter",
                       new_callable=AsyncMock):
                with patch("app.services.prospect_contacts.get_domain_pattern_hunter",
                           new_callable=AsyncMock, return_value=None):
                    stats = await enrich_prospect_contacts(prospect.id, db_session, credit_tracker=tracker)

        assert stats["new_hires"] >= 1

    @pytest.mark.asyncio
    async def test_credit_limit_stops_batch(self, db_session):
        """Lines 517-519: Apollo credit exhaustion stops the batch."""
        from app.services.prospect_contacts import run_contact_enrichment_batch, CreditTracker

        _make_prospect(db_session, domain="credit1.com", fit_score=80)
        _make_prospect(db_session, domain="credit2.com", fit_score=80)

        tracker = CreditTracker(apollo_limit=0, hunter_search_limit=25, hunter_verify_limit=50)

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch("app.services.prospect_contacts.CreditTracker", return_value=tracker):
                result = asyncio.get_event_loop().run_until_complete(
                    run_contact_enrichment_batch(min_fit_score=60)
                )

        assert result["skipped_credit_limit"] > 0
