"""tests/test_prospect_claim.py — Tests for app/services/prospect_claim.py.

Covers all public functions: _split_hq_location, _format_similar_names,
_active_account_count, claim_prospect, release_prospect, reveal_contacts,
generate_account_briefing, _template_briefing, check_enrichment_status,
add_prospect_manually.

Called by: pytest
Depends on: conftest.py (db_session, test_user), ProspectAccount, Company,
            CustomerSite, SiteContact, User models
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import ProspectAccountStatus
from app.models import Company, User
from app.models.crm import CustomerSite, SiteContact
from app.models.prospect_account import ProspectAccount
from app.services.prospect_claim import (
    _active_account_count,
    _format_similar_names,
    _split_hq_location,
    _template_briefing,
    add_prospect_manually,
    check_enrichment_status,
    claim_prospect,
    generate_account_briefing,
    release_prospect,
    reveal_contacts,
    trigger_deep_enrichment_bg,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_prospect(
    db: Session,
    *,
    name: str = "Acme Corp",
    domain: str = "acme.com",
    status: str = ProspectAccountStatus.SUGGESTED,
    company_id: int | None = None,
    claimed_by: int | None = None,
    enrichment_data: dict | None = None,
    hq_location: str | None = None,
    fit_score: int = 50,
    readiness_score: int = 40,
) -> ProspectAccount:
    p = ProspectAccount(
        name=name,
        domain=domain,
        discovery_source="manual",
        status=status,
        company_id=company_id,
        claimed_by=claimed_by,
        fit_score=fit_score,
        readiness_score=readiness_score,
        hq_location=hq_location,
        enrichment_data=enrichment_data or {},
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_company(
    db: Session,
    *,
    name: str = "Acme Corp",
    domain: str = "acme.com",
    owner_id: int | None = None,
) -> Company:
    co = Company(
        name=name,
        domain=domain,
        is_active=True,
        account_owner_id=owner_id,
        source="manual",
    )
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


# ── _split_hq_location ───────────────────────────────────────────────


class TestSplitHqLocation:
    def test_none_input(self):
        assert _split_hq_location(None) == (None, None)

    def test_empty_string(self):
        assert _split_hq_location("") == (None, None)

    def test_no_comma(self):
        assert _split_hq_location("Austin") == (None, None)

    def test_valid_city_state(self):
        city, state = _split_hq_location("Austin, TX")
        assert city == "Austin"
        assert state == "TX"

    def test_strips_whitespace(self):
        city, state = _split_hq_location("  San Jose ,  CA  ")
        assert city == "San Jose"
        assert state == "CA"

    def test_multiple_commas_uses_first_two(self):
        city, state = _split_hq_location("Austin, TX, USA")
        assert city == "Austin"
        assert state == "TX"


# ── _format_similar_names ────────────────────────────────────────────


class TestFormatSimilarNames:
    def test_empty_list(self):
        assert _format_similar_names([], 5) == ""

    def test_dict_entries_uses_name_key(self):
        similar = [{"name": "Alpha Inc"}, {"name": "Beta LLC"}]
        result = _format_similar_names(similar, 5)
        assert result == "Alpha Inc, Beta LLC"

    def test_string_entries(self):
        similar = ["Alpha Inc", "Beta LLC"]
        result = _format_similar_names(similar, 5)
        assert result == "Alpha Inc, Beta LLC"

    def test_mixed_dict_and_string(self):
        similar = [{"name": "Alpha Inc"}, "Beta LLC"]
        result = _format_similar_names(similar, 5)
        assert result == "Alpha Inc, Beta LLC"

    def test_limit_truncates(self):
        similar = [{"name": f"Company {i}"} for i in range(10)]
        result = _format_similar_names(similar, 3)
        assert result == "Company 0, Company 1, Company 2"

    def test_limit_zero(self):
        similar = [{"name": "Alpha Inc"}]
        assert _format_similar_names(similar, 0) == ""

    def test_dict_missing_name_key_raises_type_error(self):
        # dict.get("name", s) returns the dict itself when key missing — str.join
        # then receives a dict instead of a str, which raises TypeError.
        similar = [{"other_key": "value"}]
        with pytest.raises(TypeError):
            _format_similar_names(similar, 5)


# ── _active_account_count ────────────────────────────────────────────
# Regression axis for H9: claim assigns *company-level* ownership
# (Company.account_owner_id), so the anti-hoarding cap must count owned Companies.
# The old guard counted CustomerSite.owner_id — an axis claim never sets — so it was a
# dead no-op that never tripped.


class TestActiveAccountCount:
    def test_no_accounts_returns_zero(self, db_session: Session, test_user: User):
        assert _active_account_count(db_session, test_user.id) == 0

    def test_counts_active_owned_companies(self, db_session: Session, test_user: User):
        _make_company(db_session, domain="owned-1.com", owner_id=test_user.id)
        _make_company(db_session, domain="owned-2.com", owner_id=test_user.id)
        assert _active_account_count(db_session, test_user.id) == 2

    def test_ignores_inactive_companies(self, db_session: Session, test_user: User):
        co = _make_company(db_session, domain="inactive-co.com", owner_id=test_user.id)
        co.is_active = False
        db_session.commit()
        assert _active_account_count(db_session, test_user.id) == 0

    def test_ignores_other_users_companies(self, db_session: Session, test_user: User, admin_user: User):
        _make_company(db_session, domain="other-owned-co.com", owner_id=admin_user.id)
        assert _active_account_count(db_session, test_user.id) == 0


# ── claim_prospect ───────────────────────────────────────────────────


class TestClaimProspect:
    def test_path_b_creates_company_and_site(self, db_session: Session, test_user: User):
        prospect = _make_prospect(db_session, domain="newdisco.com", hq_location="Dallas, TX")
        with patch("app.cache.decorators.invalidate_prefix") as mock_inv:
            result = claim_prospect(prospect.id, test_user.id, db_session)

        assert result["status"] == "claimed"
        assert result["path"] == "new_company"
        assert result["enrichment_status"] == "pending"
        mock_inv.assert_called_once_with("companies_typeahead")

        db_session.expire_all()
        company = db_session.query(Company).filter(Company.domain == "newdisco.com").first()
        assert company is not None
        assert company.account_owner_id == test_user.id
        assert company.hq_city == "Dallas"
        assert company.hq_state == "TX"

        site = db_session.query(CustomerSite).filter(CustomerSite.company_id == company.id).first()
        assert site is not None
        assert site.site_name == "HQ"

        db_session.refresh(prospect)
        assert prospect.status == ProspectAccountStatus.CLAIMED
        assert prospect.claimed_by == test_user.id
        assert prospect.company_id == company.id
        ed = prospect.enrichment_data or {}
        assert ed.get("claim_enrichment_status") == "pending"

    def test_path_a_updates_existing_company(self, db_session: Session, test_user: User):
        company = _make_company(db_session, domain="sf-company.com")
        prospect = _make_prospect(db_session, domain="sf-company.com", company_id=company.id)
        result = claim_prospect(prospect.id, test_user.id, db_session)

        assert result["path"] == "existing_company"
        assert result["status"] == "claimed"
        db_session.refresh(company)
        assert company.account_owner_id == test_user.id

    def test_path_a_existing_company_same_owner_allowed(self, db_session: Session, test_user: User):
        company = _make_company(db_session, domain="already-owned.com", owner_id=test_user.id)
        prospect = _make_prospect(db_session, domain="already-owned.com", company_id=company.id)
        result = claim_prospect(prospect.id, test_user.id, db_session)
        assert result["status"] == "claimed"

    def test_path_a_raises_if_company_owned_by_other(self, db_session: Session, test_user: User, admin_user: User):
        company = _make_company(db_session, domain="other-owned.com", owner_id=admin_user.id)
        prospect = _make_prospect(db_session, domain="other-owned.com", company_id=company.id)
        with pytest.raises(ValueError, match="already owned by another user"):
            claim_prospect(prospect.id, test_user.id, db_session)

    def test_domain_collision_links_to_existing_company(self, db_session: Session, test_user: User):
        existing_company = _make_company(db_session, name="Existing Corp", domain="collision.com")
        prospect = _make_prospect(db_session, domain="collision.com", company_id=None)
        with patch("app.cache.decorators.invalidate_prefix"):
            result = claim_prospect(prospect.id, test_user.id, db_session)

        assert result["path"] == "domain_collision"
        assert "warning" in result
        assert "Existing Corp" in result["warning"]
        db_session.refresh(existing_company)
        assert existing_company.account_owner_id == test_user.id
        db_session.refresh(prospect)
        assert prospect.company_id == existing_company.id

    def test_domain_collision_raises_if_other_owner(self, db_session: Session, test_user: User, admin_user: User):
        _make_company(db_session, domain="owned-collision.com", owner_id=admin_user.id)
        prospect = _make_prospect(db_session, domain="owned-collision.com", company_id=None)
        with pytest.raises(ValueError, match="same domain.*already owned"):
            claim_prospect(prospect.id, test_user.id, db_session)

    def test_raises_lookup_error_if_prospect_not_found(self, db_session: Session, test_user: User):
        with pytest.raises(LookupError, match="Prospect not found"):
            claim_prospect(99999, test_user.id, db_session)

    def test_raises_value_error_if_already_claimed(self, db_session: Session, test_user: User):
        prospect = _make_prospect(
            db_session,
            domain="already-claimed.com",
            status=ProspectAccountStatus.CLAIMED,
            claimed_by=test_user.id,
        )
        with pytest.raises(ValueError, match="Already claimed"):
            claim_prospect(prospect.id, test_user.id, db_session)

    def test_raises_value_error_if_status_not_suggested(self, db_session: Session, test_user: User):
        prospect = _make_prospect(
            db_session,
            domain="dismissed.com",
            status=ProspectAccountStatus.DISMISSED,
        )
        with pytest.raises(ValueError, match="Cannot claim prospect with status"):
            claim_prospect(prospect.id, test_user.id, db_session)

    def test_raises_value_error_if_account_cap_hit(self, db_session: Session, test_user: User):
        _make_company(db_session, domain="cap-test-company.com")
        # Patch ACCOUNT_CAP to 0 to simulate the cap being hit
        with patch("app.services.prospect_claim.ACCOUNT_CAP", 0):
            prospect = _make_prospect(db_session, domain="cap-hit.com")
            with pytest.raises(ValueError, match="cap"):
                claim_prospect(prospect.id, test_user.id, db_session)

    def test_cap_counts_claimed_accounts(self, db_session: Session, test_user: User):
        """Regression (H9): the cap must count what claim assigns (owned Companies).

        With ACCOUNT_CAP=1 a first claim succeeds (the rep now owns one Company) and a
        second is blocked. The old guard counted CustomerSite.owner_id — which claim
        never sets — so the count stayed 0 and this second claim was NOT blocked.
        """
        with patch("app.services.prospect_claim.ACCOUNT_CAP", 1):
            p1 = _make_prospect(db_session, domain="cap-first.com")
            with patch("app.cache.decorators.invalidate_prefix"):
                claim_prospect(p1.id, test_user.id, db_session)
            assert _active_account_count(db_session, test_user.id) == 1

            p2 = _make_prospect(db_session, domain="cap-second.com")
            with pytest.raises(ValueError, match="cap"):
                claim_prospect(p2.id, test_user.id, db_session)

    def test_raises_lookup_error_if_user_not_found(self, db_session: Session):
        prospect = _make_prospect(db_session, domain="user-not-found.com")
        with pytest.raises(LookupError, match="User not found"):
            claim_prospect(prospect.id, 99999, db_session)

    def test_no_domain_on_prospect_skips_collision_check(self, db_session: Session, test_user: User):
        # Test with domain that has no existing company and no collision
        prospect = _make_prospect(db_session, domain="unique-no-collision.io")
        with patch("app.cache.decorators.invalidate_prefix"):
            result = claim_prospect(prospect.id, test_user.id, db_session)
        assert result["path"] == "new_company"


# ── release_prospect ─────────────────────────────────────────────────


class TestReleaseProspect:
    def test_valid_release_by_owner(self, db_session: Session, test_user: User):
        company = _make_company(db_session, domain="release-me.com", owner_id=test_user.id)
        prospect = _make_prospect(
            db_session,
            domain="release-me.com",
            status=ProspectAccountStatus.CLAIMED,
            claimed_by=test_user.id,
            company_id=company.id,
            enrichment_data={"claim_enrichment_status": "complete"},
        )
        result = release_prospect(prospect.id, test_user.id, db_session)

        assert result["status"] == "suggested"
        assert result["prospect_id"] == prospect.id

        db_session.refresh(prospect)
        assert prospect.status == ProspectAccountStatus.SUGGESTED
        assert prospect.claimed_by is None
        assert prospect.claimed_at is None
        ed = prospect.enrichment_data or {}
        assert "claim_enrichment_status" not in ed

        db_session.refresh(company)
        assert company.account_owner_id is None

    def test_admin_can_release_others_prospect(self, db_session: Session, test_user: User, admin_user: User):
        prospect = _make_prospect(
            db_session,
            domain="admin-release.com",
            status=ProspectAccountStatus.CLAIMED,
            claimed_by=test_user.id,
        )
        result = release_prospect(prospect.id, admin_user.id, db_session, is_admin=True)
        assert result["status"] == "suggested"

    def test_non_owner_cannot_release(self, db_session: Session, test_user: User, admin_user: User):
        prospect = _make_prospect(
            db_session,
            domain="not-your-prospect.com",
            status=ProspectAccountStatus.CLAIMED,
            claimed_by=test_user.id,
        )
        with pytest.raises(ValueError, match="Only the owner or an admin"):
            release_prospect(prospect.id, admin_user.id, db_session, is_admin=False)

    def test_raises_if_not_claimed(self, db_session: Session, test_user: User):
        prospect = _make_prospect(
            db_session,
            domain="not-claimed.com",
            status=ProspectAccountStatus.SUGGESTED,
        )
        with pytest.raises(ValueError, match="Only a claimed prospect can be released"):
            release_prospect(prospect.id, test_user.id, db_session)

    def test_raises_lookup_error_if_not_found(self, db_session: Session, test_user: User):
        with pytest.raises(LookupError, match="Prospect not found"):
            release_prospect(99999, test_user.id, db_session)

    def test_company_not_unowned_if_different_owner(self, db_session: Session, test_user: User, admin_user: User):
        # Company is owned by admin_user, prospect.claimed_by = test_user
        # Release should NOT clear admin_user's ownership
        company = _make_company(db_session, domain="mixed-owner.com", owner_id=admin_user.id)
        prospect = _make_prospect(
            db_session,
            domain="mixed-owner.com",
            status=ProspectAccountStatus.CLAIMED,
            claimed_by=test_user.id,
            company_id=company.id,
        )
        release_prospect(prospect.id, test_user.id, db_session)
        db_session.refresh(company)
        # Company.account_owner_id should be unchanged (admin_user.id)
        assert company.account_owner_id == admin_user.id


# ── reveal_contacts ──────────────────────────────────────────────────


class TestRevealContacts:
    def test_no_company_id_returns_empty(self, db_session: Session):
        prospect = _make_prospect(db_session, domain="no-company.com", company_id=None)
        result = reveal_contacts(prospect, db_session)
        assert result == []

    def test_empty_contacts_full_returns_empty(self, db_session: Session):
        company = _make_company(db_session, domain="empty-contacts.com")
        prospect = _make_prospect(
            db_session,
            domain="empty-contacts.com",
            company_id=company.id,
            enrichment_data={"contacts_full": []},
        )
        result = reveal_contacts(prospect, db_session)
        assert result == []

    def test_no_enrichment_data_returns_empty(self, db_session: Session):
        company = _make_company(db_session, domain="no-enrichment.com")
        prospect = _make_prospect(
            db_session,
            domain="no-enrichment.com",
            company_id=company.id,
            enrichment_data={},
        )
        result = reveal_contacts(prospect, db_session)
        assert result == []

    def test_creates_site_contact_records(self, db_session: Session):
        company = _make_company(db_session, domain="contacts-reveal.com")
        prospect = _make_prospect(
            db_session,
            domain="contacts-reveal.com",
            company_id=company.id,
            hq_location="Chicago, IL",
            enrichment_data={
                "contacts_full": [
                    {
                        "name": "Jane Doe",
                        "title": "Buyer",
                        "email": "jane@contacts-reveal.com",
                        "verified": True,
                        "seniority": "senior",
                    },
                    {
                        "name": "Bob Smith",
                        "title": "Engineer",
                        "email": "bob@contacts-reveal.com",
                        "verified": False,
                        "seniority": "mid",
                    },
                ]
            },
        )
        result = reveal_contacts(prospect, db_session)

        assert len(result) == 2
        assert result[0]["email"] == "jane@contacts-reveal.com"
        assert result[0]["name"] == "Jane Doe"
        assert result[1]["email"] == "bob@contacts-reveal.com"

        contacts = db_session.query(SiteContact).join(CustomerSite).filter(CustomerSite.company_id == company.id).all()
        assert len(contacts) == 2
        primary_contact = next(c for c in contacts if c.is_primary)
        assert primary_contact.email == "jane@contacts-reveal.com"

    def test_deduplicates_by_email(self, db_session: Session):
        company = _make_company(db_session, domain="dedup-email.com")
        # Pre-create a site with an existing contact
        site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        existing_contact = SiteContact(
            customer_site_id=site.id,
            full_name="Existing Person",
            email="existing@dedup-email.com",
            is_active=True,
            contact_status="new",
        )
        db_session.add(existing_contact)
        db_session.commit()

        prospect = _make_prospect(
            db_session,
            domain="dedup-email.com",
            company_id=company.id,
            enrichment_data={
                "contacts_full": [
                    {
                        "name": "Existing Person",
                        "email": "existing@dedup-email.com",
                    },
                    {
                        "name": "New Person",
                        "email": "new@dedup-email.com",
                    },
                ]
            },
        )
        result = reveal_contacts(prospect, db_session)
        # Only the new one should be created
        assert len(result) == 1
        assert result[0]["email"] == "new@dedup-email.com"

    def test_skips_contacts_with_no_email(self, db_session: Session):
        company = _make_company(db_session, domain="no-email-contact.com")
        prospect = _make_prospect(
            db_session,
            domain="no-email-contact.com",
            company_id=company.id,
            enrichment_data={
                "contacts_full": [
                    {"name": "No Email Person", "email": ""},
                    {"name": "Null Email Person"},
                ]
            },
        )
        result = reveal_contacts(prospect, db_session)
        assert result == []

    def test_creates_site_when_none_exists(self, db_session: Session):
        company = _make_company(db_session, domain="site-create.com")
        prospect = _make_prospect(
            db_session,
            domain="site-create.com",
            company_id=company.id,
            hq_location="Seattle, WA",
            enrichment_data={"contacts_full": [{"name": "Alice", "email": "alice@site-create.com"}]},
        )
        reveal_contacts(prospect, db_session)
        site = db_session.query(CustomerSite).filter(CustomerSite.company_id == company.id).first()
        assert site is not None
        assert site.city == "Seattle"
        assert site.state == "WA"

    def test_uses_existing_site_when_present(self, db_session: Session):
        company = _make_company(db_session, domain="existing-site.com")
        existing_site = CustomerSite(company_id=company.id, site_name="Main HQ", is_active=True)
        db_session.add(existing_site)
        db_session.commit()

        prospect = _make_prospect(
            db_session,
            domain="existing-site.com",
            company_id=company.id,
            enrichment_data={"contacts_full": [{"name": "Bob", "email": "bob@existing-site.com"}]},
        )
        reveal_contacts(prospect, db_session)

        site_count = db_session.query(CustomerSite).filter(CustomerSite.company_id == company.id).count()
        assert site_count == 1


# ── generate_account_briefing ────────────────────────────────────────


class TestGenerateAccountBriefing:
    async def test_returns_none_for_missing_prospect(self, db_session: Session):
        result = await generate_account_briefing(99999, db_session)
        assert result is None

    async def test_ai_success_returns_briefing(self, db_session: Session):
        prospect = _make_prospect(
            db_session,
            domain="ai-briefing.com",
            readiness_score=70,
            fit_score=80,
        )
        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = "AI-generated briefing text"
            result = await generate_account_briefing(prospect.id, db_session)

        assert result == "AI-generated briefing text"
        mock_claude.assert_called_once()

    async def test_ai_failure_falls_back_to_template(self, db_session: Session):
        prospect = _make_prospect(
            db_session,
            domain="ai-fallback.com",
            fit_score=60,
            readiness_score=55,
        )
        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.side_effect = Exception("Claude API down")
            result = await generate_account_briefing(prospect.id, db_session)

        assert result is not None
        assert "Account Briefing" in result

    async def test_ai_returns_empty_string_falls_back_to_template(self, db_session: Session):
        prospect = _make_prospect(
            db_session,
            domain="ai-empty.com",
            fit_score=50,
            readiness_score=40,
        )
        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = ""
            result = await generate_account_briefing(prospect.id, db_session)

        assert result is not None
        assert "Account Briefing" in result

    async def test_uses_similar_customers_in_prompt(self, db_session: Session):
        prospect = _make_prospect(db_session, domain="similar-in-prompt.com")
        prospect.similar_customers = [{"name": "Widget Co"}, {"name": "Gadget Inc"}]
        db_session.commit()

        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = "Briefing with similar customers"
            result = await generate_account_briefing(prospect.id, db_session)

        assert result == "Briefing with similar customers"
        call_args = mock_claude.call_args
        prompt = call_args[0][0]
        assert "Widget Co" in prompt


# ── _template_briefing ───────────────────────────────────────────────


def _make_db_prospect(db: Session, domain: str, **kwargs) -> ProspectAccount:
    """Create a persisted ProspectAccount for _template_briefing tests."""
    p = ProspectAccount(
        name=kwargs.get("name", "Test Co"),
        domain=domain,
        discovery_source="manual",
        status=ProspectAccountStatus.SUGGESTED,
        fit_score=kwargs.get("fit_score", 50),
        readiness_score=kwargs.get("readiness_score", 40),
        industry=kwargs.get("industry", None),
        employee_count_range=kwargs.get("employee_count_range", None),
        hq_location=kwargs.get("hq_location", None),
        ai_writeup=kwargs.get("ai_writeup", None),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


class TestTemplateBriefing:
    def test_basic_output_contains_name(self, db_session: Session):
        p = _make_db_prospect(db_session, "template-basic.com", name="MyCompany")
        result = _template_briefing(p, {}, [])
        assert "MyCompany" in result
        assert "Account Briefing" in result

    def test_includes_industry_and_size(self, db_session: Session):
        p = _make_db_prospect(
            db_session,
            "template-industry.com",
            industry="Aerospace",
            employee_count_range="500-1000",
        )
        result = _template_briefing(p, {}, [])
        assert "Aerospace" in result
        assert "500-1000" in result

    def test_not_specified_for_missing_fields(self, db_session: Session):
        p = _make_db_prospect(db_session, "template-missing.com")
        result = _template_briefing(p, {}, [])
        assert "Not specified" in result

    def test_intent_signal_rendered_when_present(self, db_session: Session):
        p = _make_db_prospect(db_session, "template-intent.com")
        signals = {"intent": {"strength": "high"}}
        result = _template_briefing(p, signals, [])
        assert "high" in result
        assert "Intent Signal" in result

    def test_hiring_signal_rendered_when_present(self, db_session: Session):
        p = _make_db_prospect(db_session, "template-hiring.com")
        signals = {"hiring": {"type": "Procurement Engineers"}}
        result = _template_briefing(p, signals, [])
        assert "Procurement Engineers" in result
        assert "Hiring Signal" in result

    def test_similar_customers_rendered(self, db_session: Session):
        p = _make_db_prospect(db_session, "template-similar.com")
        similar = [{"name": "Alpha Corp"}, {"name": "Beta Inc"}]
        result = _template_briefing(p, {}, similar)
        assert "Alpha Corp" in result
        assert "Similar Customers" in result

    def test_ai_writeup_appended(self, db_session: Session):
        p = _make_db_prospect(db_session, "template-writeup.com", ai_writeup="They source a lot of FPGAs.")
        result = _template_briefing(p, {}, [])
        assert "FPGAs" in result

    def test_no_signals_no_similar_minimal_output(self, db_session: Session):
        p = _make_db_prospect(db_session, "template-minimal.com")
        result = _template_briefing(p, {}, [])
        assert "Account Briefing" in result

    def test_non_dict_intent_signal_skipped(self, db_session: Session):
        p = _make_db_prospect(db_session, "template-intent-str.com")
        signals = {"intent": "high-string"}
        result = _template_briefing(p, signals, [])
        assert "Account Briefing" in result

    def test_non_dict_hiring_signal_skipped(self, db_session: Session):
        p = _make_db_prospect(db_session, "template-hiring-str.com")
        signals = {"hiring": "some text"}
        result = _template_briefing(p, signals, [])
        assert "Account Briefing" in result


# ── check_enrichment_status ──────────────────────────────────────────


class TestCheckEnrichmentStatus:
    def test_raises_if_not_found(self, db_session: Session):
        with pytest.raises(LookupError, match="Prospect not found"):
            check_enrichment_status(99999, db_session)

    def test_status_none_when_no_enrichment_data(self, db_session: Session):
        prospect = _make_prospect(db_session, domain="no-status.com", enrichment_data={})
        result = check_enrichment_status(prospect.id, db_session)
        assert result["status"] == "none"
        assert result["contacts_created"] == 0
        assert result["briefing_ready"] is False
        assert result["error"] is None

    def test_status_pending(self, db_session: Session):
        prospect = _make_prospect(
            db_session,
            domain="pending-status.com",
            enrichment_data={"claim_enrichment_status": "pending"},
        )
        result = check_enrichment_status(prospect.id, db_session)
        assert result["status"] == "pending"

    def test_status_complete_with_briefing(self, db_session: Session):
        prospect = _make_prospect(
            db_session,
            domain="complete-status.com",
            enrichment_data={
                "claim_enrichment_status": "complete",
                "contacts_created_count": 3,
                "briefing": "Full briefing text here",
            },
        )
        result = check_enrichment_status(prospect.id, db_session)
        assert result["status"] == "complete"
        assert result["contacts_created"] == 3
        assert result["briefing_ready"] is True
        assert result["error"] is None

    def test_status_failed_with_error(self, db_session: Session):
        prospect = _make_prospect(
            db_session,
            domain="failed-status.com",
            enrichment_data={
                "claim_enrichment_status": "failed",
                "enrichment_error": "Connection timeout",
            },
        )
        result = check_enrichment_status(prospect.id, db_session)
        assert result["status"] == "failed"
        assert result["error"] == "Connection timeout"


# ── add_prospect_manually ────────────────────────────────────────────


class TestAddProspectManually:
    def test_new_domain_creates_prospect(self, db_session: Session, test_user: User):
        result = add_prospect_manually("acmerobotics.com", test_user.id, db_session)

        assert result["is_new"] is True
        assert result["domain"] == "acmerobotics.com"
        assert result["status"] == "suggested"
        assert result["name"] == "Acmerobotics"

        prospect = db_session.query(ProspectAccount).filter(ProspectAccount.domain == "acmerobotics.com").first()
        assert prospect is not None
        assert prospect.discovery_source == "manual"
        assert prospect.enrichment_data.get("submitted_by") == test_user.id

    def test_existing_domain_returns_existing(self, db_session: Session, test_user: User):
        existing = _make_prospect(
            db_session,
            name="Existing Prospect",
            domain="existing-manual.com",
        )
        result = add_prospect_manually("existing-manual.com", test_user.id, db_session)

        assert result["is_new"] is False
        assert result["prospect_id"] == existing.id
        assert result["domain"] == "existing-manual.com"

    def test_empty_domain_raises_value_error(self, db_session: Session, test_user: User):
        with pytest.raises(ValueError, match="Domain is required"):
            add_prospect_manually("", test_user.id, db_session)

    def test_whitespace_only_domain_raises(self, db_session: Session, test_user: User):
        with pytest.raises(ValueError, match="Domain is required"):
            add_prospect_manually("   ", test_user.id, db_session)

    def test_domain_is_lowercased(self, db_session: Session, test_user: User):
        result = add_prospect_manually("UPPER-CASE.COM", test_user.id, db_session)
        assert result["domain"] == "upper-case.com"

    def test_domain_is_stripped(self, db_session: Session, test_user: User):
        result = add_prospect_manually("  stripped.com  ", test_user.id, db_session)
        assert result["domain"] == "stripped.com"

    def test_name_derived_from_domain(self, db_session: Session, test_user: User):
        result = add_prospect_manually("my-cool-startup.io", test_user.id, db_session)
        assert result["name"] == "My Cool Startup"

    def test_fit_and_readiness_scores_zero(self, db_session: Session, test_user: User):
        add_prospect_manually("zero-score.com", test_user.id, db_session)
        prospect = db_session.query(ProspectAccount).filter(ProspectAccount.domain == "zero-score.com").first()
        assert prospect.fit_score == 0
        assert prospect.readiness_score == 0


# ── trigger_deep_enrichment_bg ───────────────────────────────────────


class TestTriggerDeepEnrichmentBg:
    """Tests for trigger_deep_enrichment_bg.

    The function calls db.close() in its finally block. We patch SessionLocal to return
    the test session, and also patch db.close() to a no-op so the session stays open for
    post-call assertions.
    """

    async def test_happy_path_sets_complete_status(self, db_session: Session):
        company = _make_company(db_session, domain="bg-enrich.com")
        prospect = _make_prospect(
            db_session,
            domain="bg-enrich.com",
            company_id=company.id,
            enrichment_data={"claim_enrichment_status": "pending"},
        )
        prospect_id = prospect.id

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude,
        ):
            mock_claude.return_value = "Briefing from bg task"
            await trigger_deep_enrichment_bg(prospect_id)

        db_session.expire_all()
        updated = db_session.get(ProspectAccount, prospect_id)
        ed = updated.enrichment_data or {}
        assert ed.get("claim_enrichment_status") == "complete"
        assert "deep_enrichment_at" in ed

    async def test_updates_company_enrichment_timestamps(self, db_session: Session):
        company = _make_company(db_session, domain="bg-company-ts.com")
        prospect = _make_prospect(
            db_session,
            domain="bg-company-ts.com",
            company_id=company.id,
        )
        company_id = company.id

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude,
        ):
            mock_claude.return_value = "Briefing"
            await trigger_deep_enrichment_bg(prospect.id)

        db_session.expire_all()
        updated_company = db_session.get(Company, company_id)
        assert updated_company.deep_enrichment_at is not None

    async def test_prospect_not_found_returns_early(self, db_session: Session):
        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
        ):
            # Should not raise — logs error and returns
            await trigger_deep_enrichment_bg(99999)

    async def test_exception_sets_failed_status(self, db_session: Session):
        prospect = _make_prospect(
            db_session,
            domain="bg-fail.com",
            enrichment_data={"claim_enrichment_status": "pending"},
        )
        prospect_id = prospect.id

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.prospect_claim.reveal_contacts",
                side_effect=RuntimeError("DB exploded"),
            ),
        ):
            await trigger_deep_enrichment_bg(prospect_id)

        db_session.expire_all()
        updated = db_session.get(ProspectAccount, prospect_id)
        ed = updated.enrichment_data or {}
        assert ed.get("claim_enrichment_status") == "failed"
        assert "DB exploded" in (ed.get("enrichment_error") or "")

    async def test_contacts_created_count_stored(self, db_session: Session):
        company = _make_company(db_session, domain="bg-contacts.com")
        prospect = _make_prospect(
            db_session,
            domain="bg-contacts.com",
            company_id=company.id,
            enrichment_data={
                "contacts_full": [
                    {"name": "Alice", "email": "alice@bg-contacts.com"},
                    {"name": "Bob", "email": "bob@bg-contacts.com"},
                ]
            },
        )
        prospect_id = prospect.id

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude,
        ):
            mock_claude.return_value = "Briefing"
            await trigger_deep_enrichment_bg(prospect_id)

        db_session.expire_all()
        updated = db_session.get(ProspectAccount, prospect_id)
        ed = updated.enrichment_data or {}
        assert ed.get("contacts_created_count") == 2


# ── Fix 1: claim cooldown bypass prevention ──────────────────────────


def _make_swept_prospect(
    db: Session,
    *,
    former_owner_id: int,
    domain: str = "swept.com",
    reclaim_blocked_until: "datetime | None" = None,
) -> ProspectAccount:
    """Helper: prospect that was swept from former_owner_id, optionally with cooldown."""
    p = ProspectAccount(
        name="Swept Corp",
        domain=domain,
        discovery_source="auto_sweep",
        status=ProspectAccountStatus.SUGGESTED,
        fit_score=0,
        readiness_score=0,
        swept_from_owner_id=former_owner_id,
        swept_at=datetime.now(UTC),
        reclaim_blocked_until=reclaim_blocked_until,
        enrichment_data={},
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


class TestClaimCooldown:
    """Fix 1: former owner cannot claim their swept account during the 30-day cooldown."""

    def test_former_owner_claim_within_cooldown_denied(self, db_session: Session, test_user: User):
        blocked_until = datetime.now(UTC) + timedelta(days=15)
        prospect = _make_swept_prospect(
            db_session,
            former_owner_id=test_user.id,
            domain="cooldown-block.com",
            reclaim_blocked_until=blocked_until,
        )
        with pytest.raises(ValueError, match="30-day cooldown"):
            claim_prospect(prospect.id, test_user.id, db_session)

        db_session.refresh(prospect)
        assert prospect.status == ProspectAccountStatus.SUGGESTED

    def test_former_owner_claim_after_cooldown_allowed(self, db_session: Session, test_user: User):
        past = datetime.now(UTC) - timedelta(days=1)
        prospect = _make_swept_prospect(
            db_session,
            former_owner_id=test_user.id,
            domain="cooldown-past.com",
            reclaim_blocked_until=past,
        )
        with patch("app.cache.decorators.invalidate_prefix"):
            result = claim_prospect(prospect.id, test_user.id, db_session)
        assert result["status"] == "claimed"

    def test_different_rep_not_affected_by_cooldown(self, db_session: Session, test_user: User, admin_user: User):
        """A different rep claiming is unaffected by the former owner's cooldown."""
        blocked_until = datetime.now(UTC) + timedelta(days=15)
        prospect = _make_swept_prospect(
            db_session,
            former_owner_id=admin_user.id,  # admin_user is the former owner
            domain="cooldown-other.com",
            reclaim_blocked_until=blocked_until,
        )
        # test_user is NOT the former owner → cooldown doesn't apply
        with patch("app.cache.decorators.invalidate_prefix"):
            result = claim_prospect(prospect.id, test_user.id, db_session)
        assert result["status"] == "claimed"

    def test_manager_can_claim_despite_cooldown(self, db_session: Session):
        """A manager/admin who happens to be the former owner is not blocked by the
        cooldown — they can always claim (and should use reassign, but claim is allowed
        too)."""
        from app.constants import UserRole

        mgr = User(
            email="mgr-claim@test.com",
            name="Manager",
            role=UserRole.MANAGER,
            is_active=True,
            azure_id="mgr-claim-az",
        )
        db_session.add(mgr)
        db_session.commit()
        db_session.refresh(mgr)

        blocked_until = datetime.now(UTC) + timedelta(days=15)
        prospect = _make_swept_prospect(
            db_session,
            former_owner_id=mgr.id,
            domain="cooldown-manager.com",
            reclaim_blocked_until=blocked_until,
        )
        with patch("app.cache.decorators.invalidate_prefix"):
            result = claim_prospect(prospect.id, mgr.id, db_session)
        assert result["status"] == "claimed"
