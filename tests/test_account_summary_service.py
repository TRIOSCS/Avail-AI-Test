"""tests/test_account_summary_service.py -- Tests for AI account summary generation.

Covers: app/services/account_summary_service.generate_account_summary()
Depends on: conftest.py (db_session, test SQLite engine)
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.models import ActivityLog, Company, CustomerSite, Requirement, Requisition, SiteContact, User
from app.services.account_summary_service import generate_account_summary
from tests.conftest import engine  # noqa: F401


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


# Use naive datetimes throughout — SQLite strips tzinfo, and the service
# creates ``now = datetime.now(timezone.utc)`` which is tz-aware.  We patch
# ``datetime`` inside the service so ``now`` is also naive, avoiding the
# "can't subtract offset-naive and offset-aware datetimes" error.

_NOW_NAIVE = datetime(2026, 3, 1, 12, 0, 0)


class _FakeDatetime(datetime):
    """Datetime subclass that returns a naive 'now' for the service."""

    @classmethod
    def now(cls, tz=None):
        return _NOW_NAIVE


# ── Helpers ───────────────────────────────────────────────────────────


def _make_user(db, name="Owner User", email="owner@trioscs.com"):
    user = User(
        name=name,
        email=email,
        role="buyer",
        azure_id=f"az-{email}",
        created_at=_NOW_NAIVE,
    )
    db.add(user)
    db.flush()
    return user


def _make_company(db, **kwargs):
    defaults = dict(
        name="Test Corp",
        is_active=True,
        created_at=_NOW_NAIVE,
    )
    defaults.update(kwargs)
    co = Company(**defaults)
    db.add(co)
    db.flush()
    return co


def _make_site(db, company_id, site_name="HQ Site"):
    site = CustomerSite(
        company_id=company_id,
        site_name=site_name,
        is_active=True,
    )
    db.add(site)
    db.flush()
    return site


def _make_contact(db, customer_site_id, full_name="John Doe", title=None, is_primary=False):
    contact = SiteContact(
        customer_site_id=customer_site_id,
        full_name=full_name,
        title=title,
        is_primary=is_primary,
        is_active=True,
    )
    db.add(contact)
    db.flush()
    return contact


def _make_requisition(db, customer_site_id, name="REQ-001", status="open", created_at=None):
    req = Requisition(
        customer_site_id=customer_site_id,
        name=name,
        status=status,
        created_at=created_at if created_at is not None else _NOW_NAIVE,
    )
    db.add(req)
    db.flush()
    return req


def _make_requirement(db, requisition_id, primary_mpn="LM317T"):
    item = Requirement(
        requisition_id=requisition_id,
        primary_mpn=primary_mpn,
        created_at=_NOW_NAIVE,
    )
    db.add(item)
    db.flush()
    return item


def _make_activity(db, company_id, user_id, activity_type="email_sent", created_at=None):
    act = ActivityLog(
        company_id=company_id,
        user_id=user_id,
        activity_type=activity_type,
        channel="email",
        created_at=created_at if created_at is not None else _NOW_NAIVE,
    )
    db.add(act)
    db.flush()
    return act


# ── Tests ─────────────────────────────────────────────────────────────

_DT_PATCH = "app.services.account_summary_service.datetime"


@pytest.mark.asyncio
class TestAccountSummaryService:
    """Tests for generate_account_summary()."""

    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_company_not_found(self, mock_claude, db_session):
        """company_id doesn't exist -> returns {}."""
        result = _run(generate_account_summary(99999, db_session))
        assert result == {}
        mock_claude.assert_not_called()

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_minimal_company(self, mock_claude, db_session):
        """Company with no sites/contacts/reqs/activities -> builds minimal prompt,
        calls claude_json."""
        mock_claude.return_value = {
            "situation": "Minimal data",
            "development": "No pipeline",
            "next_steps": ["Gather data"],
        }
        co = _make_company(db_session)
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))

        assert result["situation"] == "Minimal data"
        assert result["development"] == "No pipeline"
        assert result["next_steps"] == ["Gather data"]
        mock_claude.assert_called_once()

        # Verify prompt content includes company name and "No activity recorded yet"
        call_args = mock_claude.call_args
        prompt = call_args[0][0]
        assert "Test Corp" in prompt
        assert "No activity recorded yet" in prompt
        assert "Account owner: Unassigned" in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_full_context(self, mock_claude, db_session):
        """Company with all optional fields populated -> prompt includes all context."""
        mock_claude.return_value = {
            "situation": "Full context",
            "development": "Active pipeline",
            "next_steps": ["Expand", "Upsell"],
        }

        owner = _make_user(db_session)
        co = _make_company(
            db_session,
            name="Full Corp",
            industry="Semiconductors",
            employee_size="51-200",
            hq_city="Dallas",
            hq_state="TX",
            account_type="Customer",
            is_strategic=True,
            credit_terms="Net 30",
            domain="fullcorp.com",
            account_owner_id=owner.id,
            brand_tags=["TI", "NXP"],
            commodity_tags=["Resistors", "Capacitors"],
            notes="Important strategic account with growth potential.",
        )

        site = _make_site(db_session, co.id, "Main Plant")
        _make_contact(db_session, site.id, "Alice VP", title="VP Procurement", is_primary=True)
        _make_contact(db_session, site.id, "Bob Buyer", title="Buyer")

        req1 = _make_requisition(db_session, site.id, "REQ-100", "open", _NOW_NAIVE - timedelta(days=5))
        req2 = _make_requisition(db_session, site.id, "REQ-101", "closed", _NOW_NAIVE - timedelta(days=30))
        _make_requirement(db_session, req1.id, "LM317T")
        _make_requirement(db_session, req1.id, "LM7805")
        _make_requirement(db_session, req2.id, "NE555")

        _make_activity(db_session, co.id, owner.id, "email_sent", _NOW_NAIVE - timedelta(days=1))
        _make_activity(db_session, co.id, owner.id, "call", _NOW_NAIVE - timedelta(days=3))
        _make_activity(db_session, co.id, owner.id, "email_sent", _NOW_NAIVE - timedelta(days=7))
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))

        assert result["situation"] == "Full context"
        assert result["next_steps"] == ["Expand", "Upsell"]

        # Verify all context fields were included in the prompt
        prompt = mock_claude.call_args[0][0]
        assert "Full Corp" in prompt
        assert "Industry: Semiconductors" in prompt
        assert "Size: 51-200 employees" in prompt
        assert "HQ: Dallas, TX" in prompt
        assert "Account type: Customer" in prompt
        assert "STRATEGIC" in prompt
        assert "Credit terms: Net 30" in prompt
        assert "Domain: fullcorp.com" in prompt
        assert f"Account owner: {owner.name}" in prompt
        assert "Brand focus: TI, NXP" in prompt
        assert "Commodity focus: Resistors, Capacitors" in prompt
        assert "Important strategic account" in prompt
        assert "Alice VP (VP Procurement) [PRIMARY]" in prompt
        assert "Bob Buyer (Buyer)" in prompt
        assert "Pipeline (2 total)" in prompt
        assert "REQ-" in prompt
        assert "2 MPNs" in prompt
        assert "email_sent: 2" in prompt
        assert "call: 1" in prompt
        assert "Last activity: 1 day(s) ago (email_sent)" in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_claude_json_exception(self, mock_claude, db_session):
        """claude_json raises -> returns {}."""
        mock_claude.side_effect = RuntimeError("API down")
        co = _make_company(db_session)
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))
        assert result == {}

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_claude_json_returns_none(self, mock_claude, db_session):
        """claude_json returns None -> returns {}."""
        mock_claude.return_value = None
        co = _make_company(db_session)
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))
        assert result == {}

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_claude_json_returns_non_dict(self, mock_claude, db_session):
        """claude_json returns string -> returns {}."""
        mock_claude.return_value = "not a dict"
        co = _make_company(db_session)
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))
        assert result == {}

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_claude_json_returns_empty_dict(self, mock_claude, db_session):
        """claude_json returns {} (falsy) -> 'not result' is True -> returns {}."""
        mock_claude.return_value = {}
        co = _make_company(db_session)
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))
        assert result == {}

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_successful_summary(self, mock_claude, db_session):
        """claude_json returns valid dict -> returns formatted result with str-coerced
        values."""
        mock_claude.return_value = {
            "situation": 123,  # non-string to test str() coercion
            "development": None,  # test str(None)
            "next_steps": ["Action 1", "Action 2"],
            "extra_key": "ignored",  # extra key not included
        }
        co = _make_company(db_session)
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))

        assert result["situation"] == "123"
        assert result["development"] == "None"
        assert result["next_steps"] == ["Action 1", "Action 2"]
        assert "extra_key" not in result

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_successful_summary_missing_keys(self, mock_claude, db_session):
        """claude_json returns dict with missing keys -> defaults to empty
        string/list."""
        mock_claude.return_value = {"situation": "only this"}
        co = _make_company(db_session)
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))

        assert result["situation"] == "only this"
        assert result["development"] == ""
        assert result["next_steps"] == []

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_company_no_sites_still_works(self, mock_claude, db_session):
        """Company with no sites -> contacts/reqs empty, activities still queried."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        owner = _make_user(db_session)
        co = _make_company(db_session, account_owner_id=owner.id)
        # Add activity directly on company (no site needed)
        _make_activity(db_session, co.id, owner.id, "note", _NOW_NAIVE)
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))

        assert result["situation"] == "ok"
        prompt = mock_claude.call_args[0][0]
        assert "Sites: 0" in prompt
        assert "Contacts: 0" in prompt
        assert "note: 1" in prompt  # activity should still appear

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_hq_city_without_state(self, mock_claude, db_session):
        """company.hq_city set but hq_state is None -> HQ shows city only."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session, hq_city="Austin", hq_state=None)
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        assert "HQ: Austin" in prompt
        # Should NOT have a trailing comma
        assert "HQ: Austin," not in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_activities_with_no_created_at(self, mock_claude, db_session):
        """Activity with created_at=None -> last_act.created_at branch skipped (line
        164)."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        owner = _make_user(db_session)
        co = _make_company(db_session)
        # Must set created_at=None AFTER add/flush to bypass SQLAlchemy default
        act = ActivityLog(
            company_id=co.id,
            user_id=owner.id,
            activity_type="manual_note",
            channel="manual",
        )
        db_session.add(act)
        db_session.flush()
        act.created_at = None
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))

        assert result["situation"] == "ok"
        prompt = mock_claude.call_args[0][0]
        # Should have activity count but NOT "Last activity: X day(s) ago"
        assert "manual_note: 1" in prompt
        assert "Last activity:" not in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_req_with_no_created_at(self, mock_claude, db_session):
        """Requisition with created_at=None -> age shows '?' (line 150)."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = Requisition(
            customer_site_id=site.id,
            name="REQ-NODATE",
            status="open",
        )
        db_session.add(req)
        db_session.flush()
        # Must set created_at=None AFTER flush to bypass SQLAlchemy default
        req.created_at = None
        _make_requirement(db_session, req.id)
        db_session.commit()

        result = _run(generate_account_summary(co.id, db_session))

        assert result["situation"] == "ok"
        prompt = mock_claude.call_args[0][0]
        assert "?d ago)" in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_contact_without_title_not_primary(self, mock_claude, db_session):
        """Contact with no title and not primary -> name only, no parens or
        [PRIMARY]."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        _make_contact(db_session, site.id, "Plain Jane", title=None, is_primary=False)
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        assert "Plain Jane" in prompt
        assert "()" not in prompt
        assert "[PRIMARY]" not in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_req_status_none_defaults_to_open(self, mock_claude, db_session):
        """Requisition with status=None -> treated as 'open' in status_counts."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = Requisition(
            customer_site_id=site.id,
            name="REQ-NOSTATUS",
            created_at=_NOW_NAIVE,
        )
        db_session.add(req)
        db_session.flush()
        # Must set status=None AFTER flush to bypass SQLAlchemy default ("active")
        req.status = None
        _make_requirement(db_session, req.id)
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        assert "open: 1" in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_inactive_site_excluded(self, mock_claude, db_session):
        """Inactive site should be excluded from site_ids."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session)
        # Active site with a contact
        active_site = _make_site(db_session, co.id, "Active Site")
        _make_contact(db_session, active_site.id, "Active Contact")

        # Inactive site with a contact
        inactive_site = CustomerSite(
            company_id=co.id,
            site_name="Inactive Site",
            is_active=False,
        )
        db_session.add(inactive_site)
        db_session.flush()
        _make_contact(db_session, inactive_site.id, "Ghost Contact")
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        assert "Sites: 1" in prompt
        assert "Contacts: 1" in prompt
        assert "Active Contact" in prompt
        assert "Ghost Contact" not in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_claude_called_with_correct_kwargs(self, mock_claude, db_session):
        """Verify claude_json is called with expected system, model_tier, max_tokens."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session)
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        call_kwargs = mock_claude.call_args[1]
        assert "strategic account advisor" in call_kwargs["system"]
        assert call_kwargs["model_tier"] == "fast"
        assert call_kwargs["max_tokens"] == 600

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_no_brand_or_commodity_tags(self, mock_claude, db_session):
        """Company with empty brand_tags/commodity_tags -> no 'Brand focus'/'Commodity
        focus' in prompt."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session, brand_tags=[], commodity_tags=[])
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        assert "Brand focus" not in prompt
        assert "Commodity focus" not in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_no_account_owner(self, mock_claude, db_session):
        """Company with no account_owner -> shows 'Unassigned'."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session, account_owner_id=None)
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        assert "Account owner: Unassigned" in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_company_not_strategic(self, mock_claude, db_session):
        """Company with is_strategic=False -> no STRATEGIC label in prompt."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session, is_strategic=False)
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        assert "STRATEGIC" not in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_notes_truncated_at_500(self, mock_claude, db_session):
        """Company notes longer than 500 chars -> truncated at 500."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        long_notes = "A" * 600
        co = _make_company(db_session, notes=long_notes)
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        # Notes should be included but truncated
        assert "Account notes: " in prompt
        assert "A" * 500 in prompt
        assert "A" * 501 not in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_inactive_contact_excluded(self, mock_claude, db_session):
        """Inactive contact should not appear in contacts list."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        _make_contact(db_session, site.id, "Active Person")
        # Inactive contact
        inactive = SiteContact(
            customer_site_id=site.id,
            full_name="Ghost Person",
            is_active=False,
        )
        db_session.add(inactive)
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        assert "Active Person" in prompt
        assert "Ghost Person" not in prompt
        assert "Contacts: 1" in prompt

    @patch(_DT_PATCH, _FakeDatetime)
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_req_counts_zero_when_no_requirements(self, mock_claude, db_session):
        """Requisition with zero requirements -> mpn_count shows 0."""
        mock_claude.return_value = {"situation": "ok", "development": "ok", "next_steps": []}

        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        _make_requisition(db_session, site.id, "REQ-EMPTY", "open")
        db_session.commit()

        _run(generate_account_summary(co.id, db_session))

        prompt = mock_claude.call_args[0][0]
        assert "0 MPNs" in prompt
