"""Tests for 4 data-quality bug fixes: TT-036, TT-100, TT-103, TT-043.

Covers:
- TT-036: _cap_outlier default cap lowered to $500K
- TT-100: Morning brief uses target user name (salesperson_id param)
- TT-103: AI prompt uses same quotes_awaiting as stats response
- TT-043: log_call_activity populates subject field
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
)

# ═══════════════════════════════════════════════════════════════════════
#  TT-036: Proactive scorecard $500K cap
# ═══════════════════════════════════════════════════════════════════════


class TestCapOutlierLowered:
    """_cap_outlier default cap should be $500K, not $10M."""

    def test_values_under_500k_pass_through(self):
        from app.services.proactive_service import _cap_outlier

        assert _cap_outlier(100.0) == 100.0
        assert _cap_outlier(250_000.0) == 250_000.0
        assert _cap_outlier(500_000.0) == 500_000.0

    def test_values_over_500k_zeroed(self):
        from app.services.proactive_service import _cap_outlier

        assert _cap_outlier(500_001.0) == 0.0
        assert _cap_outlier(4_000_000.0) == 0.0
        assert _cap_outlier(10_000_000.0) == 0.0

    def test_typical_inflated_offers_zeroed(self):
        """Offers of $4-6M each (the TT-036 scenario) should be zeroed."""
        from app.services.proactive_service import _cap_outlier

        for val in [4_000_000, 5_000_000, 6_000_000]:
            assert _cap_outlier(float(val)) == 0.0

    def test_realistic_component_deals_kept(self):
        """Real component deals ($1K-$500K) should pass through."""
        from app.services.proactive_service import _cap_outlier

        for val in [1_000, 10_000, 50_000, 100_000, 250_000, 500_000]:
            assert _cap_outlier(float(val)) == float(val)

    def test_custom_cap_still_works(self):
        from app.services.proactive_service import _cap_outlier

        assert _cap_outlier(200.0, cap=100) == 0.0
        assert _cap_outlier(50.0, cap=100) == 50.0


# ═══════════════════════════════════════════════════════════════════════
#  TT-100: Morning brief uses selected user's name
# ═══════════════════════════════════════════════════════════════════════


class TestMorningBriefTargetUser:
    """Morning brief should use salesperson_id user's name in AI prompt."""

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_default_uses_logged_in_user(self, mock_claude, client, db_session, test_user):
        mock_claude.return_value = {"text": "Brief for you."}

        resp = client.get("/api/dashboard/morning-brief")
        assert resp.status_code == 200

        # Verify Claude was called with the logged-in user's name
        call_args = mock_claude.call_args
        prompt = call_args.kwargs.get("prompt", call_args[1].get("prompt", ""))
        assert test_user.name in prompt

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_morning_brief_always_uses_logged_in_user(self, mock_claude, client, db_session, test_user):
        """Morning brief always uses the logged-in user's name (salesperson_id removed)."""
        mock_claude.return_value = {"text": "Brief for logged-in user."}

        # salesperson_id query param is ignored — always uses logged-in user
        resp = client.get("/api/dashboard/morning-brief?salesperson_id=99999")
        assert resp.status_code == 200

        call_args = mock_claude.call_args
        prompt = call_args.kwargs.get("prompt", call_args[1].get("prompt", ""))
        assert test_user.name in prompt


# ═══════════════════════════════════════════════════════════════════════
#  TT-103: Quotes awaiting matches between stats and AI prompt
# ═══════════════════════════════════════════════════════════════════════


class TestQuotesAwaitingConsistency:
    """The AI prompt should use the same quotes_awaiting value as stats."""

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_ai_prompt_matches_stats(self, mock_claude, client, db_session, test_user):
        """AI prompt's quotes count should match the returned stats.quotes_awaiting."""
        from app.models.quotes import Quote
        from app.models.sourcing import Requisition

        mock_claude.return_value = {"text": "Brief."}

        # Set up data: company + site + quotes
        c = Company(
            name="TestCo",
            is_active=True,
            account_owner_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(c)
        db_session.flush()

        s = CustomerSite(
            company_id=c.id,
            site_name="HQ",
            owner_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.flush()

        req = Requisition(
            name="REQ-QA",
            customer_site_id=s.id,
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        # 2 quotes awaiting response
        for i in range(2):
            q = Quote(
                requisition_id=req.id,
                customer_site_id=s.id,
                quote_number=f"Q-CONSIST-{i}",
                status="sent",
                subtotal=1000,
                sent_at=datetime.now(timezone.utc) - timedelta(days=1),
                created_by_id=test_user.id,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(q)
        db_session.commit()

        resp = client.get("/api/dashboard/morning-brief")
        data = resp.json()

        # Stats should show 2 awaiting
        assert data["stats"]["quotes_awaiting"] == 2

        # AI prompt should also mention 2
        call_args = mock_claude.call_args
        prompt = call_args.kwargs.get("prompt", call_args[1].get("prompt", ""))
        assert "2 quotes sent but awaiting customer response" in prompt


# ═══════════════════════════════════════════════════════════════════════
#  TT-043: Call activities populate subject field
# ═══════════════════════════════════════════════════════════════════════


class TestCallActivitySubject:
    """log_call_activity should populate the subject field."""

    def _make_company(self, db, name="Acme"):
        c = Company(
            name=name,
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(c)
        db.flush()
        return c

    def _make_site(self, db, company_id, phone=None):
        s = CustomerSite(
            company_id=company_id,
            site_name="HQ",
            contact_phone=phone,
            created_at=datetime.now(timezone.utc),
        )
        db.add(s)
        db.flush()
        return s

    def test_outbound_call_auto_subject_with_name(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        co = self._make_company(db_session)
        self._make_site(db_session, co.id, phone="+15559990001")
        db_session.commit()

        record = log_call_activity(test_user.id, "outbound", "5559990001", 120, "ext-sub-1", "Bob Smith", db_session)
        assert record is not None
        assert record.subject == "Call to Bob Smith"

    def test_inbound_call_auto_subject_with_name(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        co = self._make_company(db_session)
        self._make_site(db_session, co.id, phone="+15559990002")
        db_session.commit()

        record = log_call_activity(test_user.id, "inbound", "5559990002", 60, "ext-sub-2", "Jane Doe", db_session)
        assert record is not None
        assert record.subject == "Call from Jane Doe"

    def test_call_subject_falls_back_to_phone(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        record = log_call_activity(test_user.id, "outbound", "5559990003", 30, "ext-sub-3", None, db_session)
        assert record is not None
        assert record.subject == "Call to 5559990003"

    def test_call_subject_falls_back_to_unknown(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        record = log_call_activity(test_user.id, "inbound", "", None, "ext-sub-4", None, db_session)
        assert record is not None
        assert record.subject == "Call from unknown"

    def test_explicit_subject_overrides_auto(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        record = log_call_activity(
            test_user.id,
            "outbound",
            "5559990005",
            45,
            "ext-sub-5",
            "Bob",
            db_session,
            subject="Follow-up re: PO-1234",
        )
        assert record is not None
        assert record.subject == "Follow-up re: PO-1234"

    def test_subject_persisted_in_db(self, db_session, test_user):
        from app.services.activity_service import log_call_activity

        record = log_call_activity(test_user.id, "outbound", "5559990006", 10, "ext-sub-6", "Charlie", db_session)
        db_session.commit()

        fetched = db_session.query(ActivityLog).filter(ActivityLog.external_id == "ext-sub-6").first()
        assert fetched is not None
        assert fetched.subject == "Call to Charlie"
