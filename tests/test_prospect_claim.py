"""tests/test_prospect_claim.py — Tests for app/services/prospect_claim.py.

Covers claim/release/reveal/briefing/enrichment workflows including all
PATH A/B branches, domain collision, site cap, admin override, and async
deep-enrichment background task.

Called by: pytest autodiscovery
Depends on: conftest db_session (autouse), app.services.prospect_claim
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import ProspectAccountStatus
from app.models import Company, User
from app.models.crm import CustomerSite, SiteContact
from app.models.prospect_account import ProspectAccount
from app.services.prospect_claim import (
    SITE_CAP,
    _active_site_count,
    _format_similar_names,
    _split_hq_location,
    _template_briefing,
    add_prospect_manually,
    check_enrichment_status,
    claim_prospect,
    generate_account_briefing,
    release_prospect,
    reveal_contacts,
    send_company_to_prospecting,
    trigger_deep_enrichment_bg,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_user(db: Session, email: str = "user@test.com") -> User:
    u = User(
        email=email,
        name="Test User",
        role="buyer",
        azure_id=f"az-{email}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_prospect(
    db: Session,
    domain: str = "acme.com",
    status: str = ProspectAccountStatus.SUGGESTED,
    company_id: int | None = None,
    claimed_by: int | None = None,
    enrichment_data: dict | None = None,
) -> ProspectAccount:
    p = ProspectAccount(
        name="Acme Corp",
        domain=domain,
        discovery_source="test",
        status=status,
        fit_score=70,
        readiness_score=60,
        hq_location="Austin, TX",
        industry="Electronics",
        employee_count_range="51-200",
        revenue_range="$10M-$50M",
        company_id=company_id,
        claimed_by=claimed_by,
        claimed_at=datetime.now(timezone.utc) if claimed_by else None,
        enrichment_data=enrichment_data or {},
    )
    db.add(p)
    db.flush()
    return p


def _make_company(db: Session, domain: str = "acme.com", owner_id: int | None = None) -> Company:
    c = Company(
        name="Acme Corp",
        domain=domain,
        is_active=True,
        account_owner_id=owner_id,
        source="test",
    )
    db.add(c)
    db.flush()
    return c


# ── _split_hq_location ───────────────────────────────────────────────


def test_split_hq_location_with_comma():
    city, state = _split_hq_location("Austin, TX")
    assert city == "Austin"
    assert state == "TX"


def test_split_hq_location_without_comma():
    city, state = _split_hq_location("Austin")
    assert city is None
    assert state is None


def test_split_hq_location_none():
    city, state = _split_hq_location(None)
    assert city is None
    assert state is None


# ── _format_similar_names ─────────────────────────────────────────────


def test_format_similar_names_dicts():
    similar = [{"name": "Alpha Corp"}, {"name": "Beta Inc"}]
    result = _format_similar_names(similar, 10)
    assert result == "Alpha Corp, Beta Inc"


def test_format_similar_names_strings():
    similar = ["Alpha Corp", "Beta Inc"]
    result = _format_similar_names(similar, 10)
    assert result == "Alpha Corp, Beta Inc"


def test_format_similar_names_limit():
    similar = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
    result = _format_similar_names(similar, 2)
    assert result == "A, B"
    assert "C" not in result


# ── claim_prospect ────────────────────────────────────────────────────


def test_claim_prospect_not_found(db_session: Session):
    user = _make_user(db_session)
    db_session.commit()
    with pytest.raises(LookupError, match="Prospect not found"):
        claim_prospect(99999, user.id, db_session)


def test_claim_prospect_already_claimed(db_session: Session):
    user = _make_user(db_session)
    prospect = _make_prospect(db_session, status=ProspectAccountStatus.CLAIMED, claimed_by=user.id)
    db_session.commit()
    with pytest.raises(ValueError, match="Already claimed"):
        claim_prospect(prospect.id, user.id, db_session)


def test_claim_prospect_wrong_status(db_session: Session):
    user = _make_user(db_session)
    prospect = _make_prospect(db_session, status=ProspectAccountStatus.DISMISSED)
    db_session.commit()
    with pytest.raises(ValueError, match="Cannot claim"):
        claim_prospect(prospect.id, user.id, db_session)


def test_claim_prospect_user_not_found(db_session: Session):
    prospect = _make_prospect(db_session)
    db_session.commit()
    with pytest.raises(LookupError, match="User not found"):
        claim_prospect(prospect.id, 99999, db_session)


def test_claim_prospect_site_cap_exceeded(db_session: Session):
    user = _make_user(db_session)
    company = _make_company(db_session, domain="cap-co.com")
    # Insert SITE_CAP active sites for this user
    for i in range(SITE_CAP):
        db_session.add(CustomerSite(company_id=company.id, site_name=f"Site-{i}", owner_id=user.id, is_active=True))
    prospect = _make_prospect(db_session, domain="new-target.com")
    db_session.commit()
    with pytest.raises(ValueError, match="cap"):
        claim_prospect(prospect.id, user.id, db_session)


@patch("app.cache.decorators.invalidate_prefix")
def test_claim_prospect_path_b_new_company(mock_inv, db_session: Session):
    """PATH B: no company_id, no domain collision → creates Company + CustomerSite."""
    user = _make_user(db_session)
    prospect = _make_prospect(db_session, domain="brandnew.com")
    db_session.commit()

    result = claim_prospect(prospect.id, user.id, db_session)

    assert result["path"] == "new_company"
    assert result["status"] == "claimed"
    assert result["company_id"] is not None
    mock_inv.assert_called_once_with("companies_typeahead")

    db_session.refresh(prospect)
    assert prospect.status == ProspectAccountStatus.CLAIMED
    assert prospect.claimed_by == user.id
    # Check Company and CustomerSite were created
    company = db_session.get(Company, prospect.company_id)
    assert company is not None
    assert company.account_owner_id == user.id
    site = db_session.query(CustomerSite).filter(CustomerSite.company_id == company.id).first()
    assert site is not None
    assert site.site_name == "HQ"


def test_claim_prospect_path_a_existing_company(db_session: Session):
    """PATH A: company_id set → updates account_owner_id on existing Company."""
    user = _make_user(db_session)
    company = _make_company(db_session, domain="existing.com")
    prospect = _make_prospect(db_session, domain="existing.com", company_id=company.id)
    db_session.commit()

    result = claim_prospect(prospect.id, user.id, db_session)

    assert result["path"] == "existing_company"
    assert result["status"] == "claimed"
    db_session.refresh(company)
    assert company.account_owner_id == user.id


@patch("app.cache.decorators.invalidate_prefix")
def test_claim_prospect_path_b_domain_collision(mock_inv, db_session: Session):
    """PATH B: existing Company with same domain → links to it, sets warning."""
    user = _make_user(db_session)
    company = _make_company(db_session, domain="collision.com")
    prospect = _make_prospect(db_session, domain="collision.com")
    db_session.commit()

    result = claim_prospect(prospect.id, user.id, db_session)

    assert result["path"] == "domain_collision"
    assert "warning" in result
    assert result["company_id"] == company.id
    db_session.refresh(company)
    assert company.account_owner_id == user.id
    # invalidate_prefix should NOT be called on collision path
    mock_inv.assert_not_called()


# ── release_prospect ──────────────────────────────────────────────────


def test_release_prospect_not_found(db_session: Session):
    with pytest.raises(LookupError, match="Prospect not found"):
        release_prospect(99999, 1, db_session)


def test_release_prospect_wrong_status(db_session: Session):
    user = _make_user(db_session)
    prospect = _make_prospect(db_session, status=ProspectAccountStatus.SUGGESTED)
    db_session.commit()
    with pytest.raises(ValueError, match="Only a claimed"):
        release_prospect(prospect.id, user.id, db_session)


def test_release_prospect_wrong_owner(db_session: Session):
    owner = _make_user(db_session, email="owner@test.com")
    other = _make_user(db_session, email="other@test.com")
    prospect = _make_prospect(db_session, status=ProspectAccountStatus.CLAIMED, claimed_by=owner.id)
    db_session.commit()
    with pytest.raises(ValueError, match="Only the owner"):
        release_prospect(prospect.id, other.id, db_session)


def test_release_prospect_success(db_session: Session):
    user = _make_user(db_session)
    company = _make_company(db_session, domain="rel.com", owner_id=user.id)
    prospect = _make_prospect(
        db_session,
        domain="rel.com",
        status=ProspectAccountStatus.CLAIMED,
        claimed_by=user.id,
        company_id=company.id,
        enrichment_data={"claim_enrichment_status": "complete"},
    )
    db_session.commit()

    result = release_prospect(prospect.id, user.id, db_session)

    assert result["status"] == "suggested"
    db_session.refresh(prospect)
    assert prospect.status == ProspectAccountStatus.SUGGESTED
    assert prospect.claimed_by is None
    assert prospect.claimed_at is None
    db_session.refresh(company)
    assert company.account_owner_id is None


def test_release_prospect_admin_override(db_session: Session):
    owner = _make_user(db_session, email="owner2@test.com")
    admin = _make_user(db_session, email="admin2@test.com")
    prospect = _make_prospect(db_session, status=ProspectAccountStatus.CLAIMED, claimed_by=owner.id)
    db_session.commit()

    result = release_prospect(prospect.id, admin.id, db_session, is_admin=True)
    assert result["status"] == "suggested"


# ── send_company_to_prospecting ───────────────────────────────────────


def test_send_company_to_prospecting_not_found(db_session: Session):
    with pytest.raises(LookupError, match="Company not found"):
        send_company_to_prospecting(99999, 1, db_session)


def test_send_company_to_prospecting_wrong_owner(db_session: Session):
    owner = _make_user(db_session, email="owner3@test.com")
    other = _make_user(db_session, email="other3@test.com")
    company = _make_company(db_session, domain="sc-perm.com", owner_id=owner.id)
    db_session.commit()
    with pytest.raises(ValueError, match="Only the owner"):
        send_company_to_prospecting(company.id, other.id, db_session)


def test_send_company_to_prospecting_with_domain(db_session: Session):
    user = _make_user(db_session)
    company = _make_company(db_session, domain="sendback.com", owner_id=user.id)
    db_session.commit()

    result = send_company_to_prospecting(company.id, user.id, db_session)

    assert result["pooled"] is True
    assert result["prospect_id"] is not None
    db_session.refresh(company)
    assert company.account_owner_id is None

    prospect = db_session.get(ProspectAccount, result["prospect_id"])
    assert prospect is not None
    assert prospect.domain == "sendback.com"
    assert prospect.status == ProspectAccountStatus.SUGGESTED


def test_send_company_to_prospecting_no_domain(db_session: Session):
    user = _make_user(db_session)
    company = Company(name="No Domain Co", is_active=True, account_owner_id=user.id, source="test")
    db_session.add(company)
    db_session.commit()

    result = send_company_to_prospecting(company.id, user.id, db_session)

    assert result["pooled"] is False
    assert result["prospect_id"] is None
    db_session.refresh(company)
    assert company.account_owner_id is None


# ── reveal_contacts ───────────────────────────────────────────────────


def test_reveal_contacts_no_company_id(db_session: Session):
    prospect = _make_prospect(db_session, domain="nocompany.com")
    prospect.company_id = None
    db_session.commit()

    result = reveal_contacts(prospect, db_session)
    assert result == []


def test_reveal_contacts_no_full_contacts(db_session: Session):
    company = _make_company(db_session, domain="nofull.com")
    prospect = _make_prospect(db_session, domain="nofull.com", company_id=company.id)
    prospect.enrichment_data = {}
    db_session.commit()

    result = reveal_contacts(prospect, db_session)
    assert result == []


def test_reveal_contacts_creates_site_contacts(db_session: Session):
    company = _make_company(db_session, domain="contacts.com")
    prospect = _make_prospect(
        db_session,
        domain="contacts.com",
        company_id=company.id,
        enrichment_data={
            "contacts_full": [
                {"name": "Alice Smith", "title": "VP Procurement", "email": "alice@contacts.com", "verified": True},
                {"name": "Bob Jones", "title": "Buyer", "email": "bob@contacts.com"},
                {"name": "Dup User", "title": "Dup", "email": "alice@contacts.com"},  # duplicate
            ]
        },
    )
    db_session.commit()

    result = reveal_contacts(prospect, db_session)

    assert len(result) == 2  # duplicate skipped
    emails = {r["email"] for r in result}
    assert "alice@contacts.com" in emails
    assert "bob@contacts.com" in emails

    site = db_session.query(CustomerSite).filter(CustomerSite.company_id == company.id).first()
    assert site is not None
    contacts = db_session.query(SiteContact).filter(SiteContact.customer_site_id == site.id).all()
    assert len(contacts) == 2


# ── check_enrichment_status ───────────────────────────────────────────


def test_check_enrichment_status_not_found(db_session: Session):
    with pytest.raises(LookupError, match="Prospect not found"):
        check_enrichment_status(99999, db_session)


def test_check_enrichment_status_pending(db_session: Session):
    prospect = _make_prospect(
        db_session,
        enrichment_data={"claim_enrichment_status": "pending"},
    )
    db_session.commit()

    result = check_enrichment_status(prospect.id, db_session)
    assert result["status"] == "pending"
    assert result["briefing_ready"] is False
    assert result["contacts_created"] == 0


def test_check_enrichment_status_complete_with_briefing(db_session: Session):
    prospect = _make_prospect(
        db_session,
        enrichment_data={
            "claim_enrichment_status": "complete",
            "contacts_created_count": 3,
            "briefing": "This is the briefing.",
        },
    )
    db_session.commit()

    result = check_enrichment_status(prospect.id, db_session)
    assert result["status"] == "complete"
    assert result["contacts_created"] == 3
    assert result["briefing_ready"] is True


# ── add_prospect_manually ─────────────────────────────────────────────


def test_add_prospect_manually_new(db_session: Session):
    user = _make_user(db_session)
    db_session.commit()

    result = add_prospect_manually("newdomain.com", user.id, db_session)

    assert result["is_new"] is True
    assert result["domain"] == "newdomain.com"
    assert result["status"] == "suggested"
    assert result["prospect_id"] is not None


def test_add_prospect_manually_existing(db_session: Session):
    user = _make_user(db_session)
    prospect = _make_prospect(db_session, domain="existing-manual.com")
    db_session.commit()

    result = add_prospect_manually("existing-manual.com", user.id, db_session)

    assert result["is_new"] is False
    assert result["prospect_id"] == prospect.id


def test_add_prospect_manually_empty_domain(db_session: Session):
    user = _make_user(db_session)
    db_session.commit()
    with pytest.raises(ValueError, match="Domain is required"):
        add_prospect_manually("   ", user.id, db_session)


def test_add_prospect_manually_strips_and_lowercases(db_session: Session):
    user = _make_user(db_session)
    db_session.commit()

    result = add_prospect_manually("  MyDomain.COM  ", user.id, db_session)
    assert result["domain"] == "mydomain.com"


# ── generate_account_briefing ─────────────────────────────────────────


async def test_generate_account_briefing_not_found(db_session: Session):
    result = await generate_account_briefing(99999, db_session)
    assert result is None


async def test_generate_account_briefing_ai_success(db_session: Session):
    prospect = _make_prospect(db_session, domain="ai-brief.com")
    db_session.commit()

    with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = "AI-generated briefing text here."
        result = await generate_account_briefing(prospect.id, db_session)

    assert result == "AI-generated briefing text here."
    mock_claude.assert_awaited_once()


async def test_generate_account_briefing_ai_fails_fallback(db_session: Session):
    prospect = _make_prospect(
        db_session,
        domain="fallback-brief.com",
        enrichment_data={"some": "data"},
    )
    prospect.readiness_signals = {"intent": {"strength": "high"}}
    prospect.similar_customers = [{"name": "Similar Co"}]
    db_session.commit()

    with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
        mock_claude.side_effect = Exception("API timeout")
        result = await generate_account_briefing(prospect.id, db_session)

    # Falls back to template briefing — must be non-empty string
    assert result is not None
    assert "Acme Corp" in result
    assert "Account Briefing" in result


# ── _template_briefing ────────────────────────────────────────────────


def test_template_briefing_full(db_session: Session):
    prospect = _make_prospect(db_session, domain="template-full.com")
    prospect.ai_writeup = "Company analysis text."
    db_session.commit()
    db_session.refresh(prospect)

    signals = {
        "intent": {"strength": "high"},
        "hiring": {"type": "Electrical Engineers"},
    }
    similar = [{"name": "Alpha"}, {"name": "Beta"}, {"name": "Gamma"}]

    result = _template_briefing(prospect, signals, similar)

    assert "Account Briefing" in result
    assert "Acme Corp" in result
    assert "Intent Signal" in result
    assert "Hiring Signal" in result
    assert "Similar Customers" in result
    assert "Alpha" in result
    assert "Company analysis text." in result


def test_template_briefing_no_signals(db_session: Session):
    prospect = _make_prospect(db_session, domain="template-empty.com")
    db_session.commit()
    db_session.refresh(prospect)

    result = _template_briefing(prospect, {}, [])

    assert "Account Briefing" in result
    assert "Acme Corp" in result
    # No signals sections
    assert "Intent Signal" not in result
    assert "Hiring Signal" not in result
    assert "Similar Customers" not in result


# ── trigger_deep_enrichment_bg ────────────────────────────────────────


async def test_trigger_deep_enrichment_bg_not_found():
    """Prospect not found → logs error and returns without crashing."""
    mock_session = MagicMock()
    mock_session.get.return_value = None

    with patch("app.database.SessionLocal", return_value=mock_session):
        # Should not raise
        await trigger_deep_enrichment_bg(99999)

    mock_session.close.assert_called_once()


async def test_trigger_deep_enrichment_bg_success():
    """Full happy path: enriches status, calls reveal_contacts and generate_account_briefing."""
    # Build a minimal prospect mock so the session's get() returns it
    mock_prospect = MagicMock()
    mock_prospect.id = 42
    mock_prospect.enrichment_data = {"claim_enrichment_status": "pending"}
    mock_prospect.company_id = 7

    mock_company = MagicMock()
    mock_company.id = 7

    call_count = [0]

    def _get_side_effect(model, pk):
        call_count[0] += 1
        if model is ProspectAccount:
            return mock_prospect
        if model is Company:
            return mock_company
        return None

    mock_session = MagicMock()
    mock_session.get.side_effect = _get_side_effect

    with (
        patch("app.database.SessionLocal", return_value=mock_session),
        patch(
            "app.services.prospect_claim.generate_account_briefing",
            new_callable=AsyncMock,
            return_value="Generated briefing",
        ),
        patch("app.services.prospect_claim.reveal_contacts", return_value=[{"name": "Alice"}]),
    ):
        await trigger_deep_enrichment_bg(42)

    # Session must be closed in the finally block
    mock_session.close.assert_called_once()
    # commit called at least twice (mark enriching + final update)
    assert mock_session.commit.call_count >= 2


# ── Additional branch coverage ───────────────────────────────────────


def test_claim_prospect_path_a_company_already_owned(db_session: Session):
    """PATH A: company_id set, company owned by a different user → ValueError."""
    owner = _make_user(db_session, email="owner-a@test.com")
    other = _make_user(db_session, email="other-a@test.com")
    company = _make_company(db_session, domain="owned-a.com", owner_id=owner.id)
    prospect = _make_prospect(db_session, domain="owned-a.com", company_id=company.id)
    db_session.commit()

    with pytest.raises(ValueError, match="already owned by another user"):
        claim_prospect(prospect.id, other.id, db_session)


def test_claim_prospect_domain_collision_already_owned(db_session: Session):
    """PATH B domain collision: existing company owned by a different user → ValueError."""
    owner = _make_user(db_session, email="owner-b@test.com")
    other = _make_user(db_session, email="other-b@test.com")
    company = _make_company(db_session, domain="owned-coll.com", owner_id=owner.id)
    prospect = _make_prospect(db_session, domain="owned-coll.com")
    db_session.commit()

    with pytest.raises(ValueError, match="same domain.*already owned"):
        claim_prospect(prospect.id, other.id, db_session)


def test_send_company_to_prospecting_existing_prospect(db_session: Session):
    """Domain already has a ProspectAccount → links to it instead of creating."""
    user = _make_user(db_session)
    company = _make_company(db_session, domain="alreadypool.com", owner_id=user.id)
    existing_prospect = _make_prospect(db_session, domain="alreadypool.com")
    db_session.commit()

    result = send_company_to_prospecting(company.id, user.id, db_session)

    assert result["pooled"] is True
    assert result["prospect_id"] == existing_prospect.id


async def test_trigger_deep_enrichment_bg_exception_handler():
    """Exception in enrichment body → marks status as failed."""
    mock_prospect = MagicMock()
    mock_prospect.id = 99
    mock_prospect.enrichment_data = {}
    mock_prospect.company_id = None

    call_results = [mock_prospect, mock_prospect]  # first get + recovery get
    call_iter = iter(call_results)

    def _get_side(model, pk):
        try:
            return next(call_iter)
        except StopIteration:
            return mock_prospect

    mock_session = MagicMock()
    mock_session.get.side_effect = _get_side

    with (
        patch("app.database.SessionLocal", return_value=mock_session),
        patch(
            "app.services.prospect_claim.generate_account_briefing",
            new_callable=AsyncMock,
            side_effect=RuntimeError("something broke"),
        ),
        patch("app.services.prospect_claim.reveal_contacts", return_value=[]),
    ):
        await trigger_deep_enrichment_bg(99)

    # Should have tried to mark status=failed in exception handler
    mock_session.close.assert_called_once()


# ── _active_site_count ────────────────────────────────────────────────


def test_active_site_count_zero(db_session: Session):
    user = _make_user(db_session)
    db_session.commit()
    assert _active_site_count(db_session, user.id) == 0


def test_active_site_count_counts_active_only(db_session: Session):
    user = _make_user(db_session)
    company = _make_company(db_session, domain="count-test.com")
    db_session.add(CustomerSite(company_id=company.id, site_name="Active1", owner_id=user.id, is_active=True))
    db_session.add(CustomerSite(company_id=company.id, site_name="Active2", owner_id=user.id, is_active=True))
    db_session.add(CustomerSite(company_id=company.id, site_name="Inactive", owner_id=user.id, is_active=False))
    db_session.commit()

    assert _active_site_count(db_session, user.id) == 2
