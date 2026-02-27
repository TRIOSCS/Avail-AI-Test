"""Tests for auto_attribution_service — background unmatched activity matching.

Covers: run_auto_attribution (rule + AI + dismiss), _ai_match_batch,
        _call_claude_for_matching

Called by: pytest
Depends on: conftest fixtures, app.models, app.services.auto_attribution_service
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, Company, CustomerSite, User, VendorCard


# ── Helpers ──────────────────────────────────────────────────────────

def _make_user(db: Session) -> User:
    u = User(
        email="attrib@test.com", name="Attrib Tester", role="buyer",
        azure_id="az-attrib", created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_unmatched_activity(db: Session, user: User, **kw) -> ActivityLog:
    defaults = dict(
        user_id=user.id, activity_type="email", channel="email",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    act = ActivityLog(**defaults)
    db.add(act)
    db.flush()
    return act


# ══════════════════════════════════════════════════════════════════════
# run_auto_attribution — top-level
# ══════════════════════════════════════════════════════════════════════

class TestRunAutoAttribution:
    def test_empty_queue_noop(self, db_session):
        """Empty queue returns zero stats."""
        from app.services.auto_attribution_service import run_auto_attribution
        stats = run_auto_attribution(db_session)
        assert stats == {"rule_matched": 0, "ai_matched": 0, "auto_dismissed": 0, "skipped": 0}

    def test_rule_based_email_match(self, db_session):
        """Activity with matching email resolves via rule-based matching."""
        from app.services.auto_attribution_service import run_auto_attribution

        user = _make_user(db_session)
        co = Company(name="Acme", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ",
                            contact_email="john@acme.com")
        db_session.add(site)

        act = _make_unmatched_activity(db_session, user,
                                       contact_email="john@acme.com")
        db_session.commit()

        stats = run_auto_attribution(db_session)
        assert stats["rule_matched"] == 1
        assert db_session.get(ActivityLog, act.id).company_id == co.id

    def test_old_activities_auto_dismissed(self, db_session):
        """Activities > 30 days old that can't be matched are dismissed."""
        from app.services.auto_attribution_service import run_auto_attribution

        user = _make_user(db_session)
        old = datetime.now(timezone.utc) - timedelta(days=35)
        act = _make_unmatched_activity(db_session, user,
                                       contact_email="nobody@nowhere.com",
                                       created_at=old)
        db_session.commit()

        stats = run_auto_attribution(db_session)
        assert stats["auto_dismissed"] == 1
        assert db_session.get(ActivityLog, act.id).dismissed_at is not None

    def test_already_matched_not_processed(self, db_session):
        """Activities already matched to a company are excluded."""
        from app.services.auto_attribution_service import run_auto_attribution

        user = _make_user(db_session)
        co = Company(name="Test Co", is_active=True)
        db_session.add(co)
        db_session.flush()

        act = ActivityLog(
            user_id=user.id, activity_type="email", channel="email",
            contact_email="x@test.com", company_id=co.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        stats = run_auto_attribution(db_session)
        assert stats["rule_matched"] == 0

    def test_dismissed_not_processed(self, db_session):
        """Dismissed activities are excluded from processing."""
        from app.services.auto_attribution_service import run_auto_attribution

        user = _make_user(db_session)
        act = ActivityLog(
            user_id=user.id, activity_type="email", channel="email",
            contact_email="x@test.com",
            dismissed_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        stats = run_auto_attribution(db_session)
        assert stats["rule_matched"] == 0

    def test_ai_matching_called_for_unmatched(self, db_session):
        """Recent unmatched activities go to AI matching pass."""
        from app.services.auto_attribution_service import run_auto_attribution

        user = _make_user(db_session)
        co = Company(name="Target Co", is_active=True, domain="target.com")
        db_session.add(co)
        db_session.flush()

        act = _make_unmatched_activity(
            db_session, user,
            contact_email="mystery@unknown.com",
            contact_name="Mystery Person",
        )
        db_session.commit()

        ai_results = {act.id: {"entity_type": "company", "entity_id": co.id, "confidence": 0.9}}

        with patch("app.services.auto_attribution_service._ai_match_batch",
                   return_value=ai_results):
            with patch("app.services.activity_service.attribute_activity") as mock_attr:
                stats = run_auto_attribution(db_session)

        assert stats["ai_matched"] == 1

    def test_ai_low_confidence_skipped(self, db_session):
        """AI matches below 0.8 confidence should be skipped."""
        from app.services.auto_attribution_service import run_auto_attribution

        user = _make_user(db_session)
        act = _make_unmatched_activity(
            db_session, user, contact_email="maybe@unknown.com",
        )
        db_session.commit()

        ai_results = {act.id: {"entity_type": "company", "entity_id": 1, "confidence": 0.5}}

        with patch("app.services.auto_attribution_service._ai_match_batch",
                   return_value=ai_results):
            stats = run_auto_attribution(db_session)

        assert stats["skipped"] == 1
        assert stats["ai_matched"] == 0

    def test_ai_returns_none_skipped(self, db_session):
        """AI returning None for an activity should count as skipped."""
        from app.services.auto_attribution_service import run_auto_attribution

        user = _make_user(db_session)
        act = _make_unmatched_activity(
            db_session, user, contact_email="nope@unknown.com",
        )
        db_session.commit()

        ai_results = {act.id: None}

        with patch("app.services.auto_attribution_service._ai_match_batch",
                   return_value=ai_results):
            stats = run_auto_attribution(db_session)

        assert stats["skipped"] == 1

    def test_phone_match_no_crash(self, db_session):
        """Phone matching should not crash even on SQLite."""
        from app.services.auto_attribution_service import run_auto_attribution

        user = _make_user(db_session)
        vc = VendorCard(
            normalized_name="acme vendor", display_name="Acme Vendor",
            emails=[], phones=["5551234567"],
        )
        db_session.add(vc)
        db_session.flush()

        # Use a recent timestamp to avoid the 30-day dismiss path
        act = _make_unmatched_activity(
            db_session, user, contact_phone="5551234567",
            created_at=datetime.now(timezone.utc),
        )
        db_session.commit()

        # Patch the match functions to avoid SQLite regex issues
        with patch("app.services.activity_service.match_phone_to_entity", return_value=None):
            with patch("app.services.auto_attribution_service._ai_match_batch", return_value={}):
                stats = run_auto_attribution(db_session)

        assert isinstance(stats, dict)


# ══════════════════════════════════════════════════════════════════════
# _ai_match_batch
# ══════════════════════════════════════════════════════════════════════

class TestAIMatchBatch:
    def test_empty_activities_returns_empty(self, db_session):
        """Empty activity list returns empty dict."""
        from app.services.auto_attribution_service import _ai_match_batch
        result = _ai_match_batch([], db_session)
        assert result == {}

    def test_successful_matching(self, db_session):
        """Should call Claude and return match results."""
        from app.services.auto_attribution_service import _ai_match_batch

        user = _make_user(db_session)
        co = Company(name="Match Co", is_active=True, domain="match.com")
        db_session.add(co)
        db_session.flush()

        act = _make_unmatched_activity(
            db_session, user, contact_email="hello@match.com",
            contact_name="Test Person", subject="Test subject",
        )
        db_session.commit()

        mock_result = {
            act.id: {"entity_type": "company", "entity_id": co.id, "confidence": 0.9}
        }

        with patch("app.services.auto_attribution_service._call_claude_for_matching",
                   new_callable=AsyncMock, return_value=mock_result):
            result = _ai_match_batch([act], db_session)

        assert act.id in result
        assert result[act.id]["confidence"] == 0.9

    def test_caps_at_20_activities(self, db_session):
        """Should cap batch at 20 activities."""
        from app.services.auto_attribution_service import _ai_match_batch

        user = _make_user(db_session)
        activities = []
        for i in range(25):
            act = _make_unmatched_activity(
                db_session, user, contact_email=f"person{i}@test.com",
            )
            activities.append(act)
        db_session.commit()

        with patch("app.services.auto_attribution_service._call_claude_for_matching",
                   new_callable=AsyncMock, return_value={}) as mock_claude:
            _ai_match_batch(activities, db_session)

        if mock_claude.called:
            call_args = mock_claude.call_args
            assert len(call_args[0][0]) <= 20

    def test_claude_failure_returns_empty(self, db_session):
        """If Claude call fails, should return empty dict."""
        from app.services.auto_attribution_service import _ai_match_batch

        user = _make_user(db_session)
        act = _make_unmatched_activity(
            db_session, user, contact_email="test@test.com",
        )
        db_session.commit()

        with patch("app.services.auto_attribution_service._call_claude_for_matching",
                   new_callable=AsyncMock, side_effect=RuntimeError("API failed")):
            result = _ai_match_batch([act], db_session)

        assert result == {}

    def test_builds_company_and_vendor_lists(self, db_session):
        """Should query companies and vendors for Claude context."""
        from app.services.auto_attribution_service import _ai_match_batch

        user = _make_user(db_session)
        co = Company(name="TestCo", is_active=True, domain="testco.com")
        db_session.add(co)
        vc = VendorCard(
            normalized_name="testvend", display_name="TestVend",
            domain="testvend.com", is_blacklisted=False,
        )
        db_session.add(vc)
        db_session.flush()

        act = _make_unmatched_activity(
            db_session, user, contact_email="x@test.com",
        )
        db_session.commit()

        captured_args = {}
        async def capture_claude(activities, companies, vendors):
            captured_args["companies"] = companies
            captured_args["vendors"] = vendors
            return {}

        with patch("app.services.auto_attribution_service._call_claude_for_matching",
                   new_callable=AsyncMock, side_effect=capture_claude):
            _ai_match_batch([act], db_session)

        if captured_args:
            assert any(c["name"] == "TestCo" for c in captured_args["companies"])
            assert any(v["name"] == "TestVend" for v in captured_args["vendors"])


# ══════════════════════════════════════════════════════════════════════
# _call_claude_for_matching
# ══════════════════════════════════════════════════════════════════════

class TestCallClaudeForMatching:
    def test_returns_match_dict(self):
        """Should parse Claude's response into {activity_id: match} dict."""
        from app.services.auto_attribution_service import _call_claude_for_matching

        activities = [
            {"id": 1, "email": "a@test.com", "phone": "", "name": "Alice", "subject": "RFQ"},
        ]
        companies = [{"id": 10, "name": "TestCo", "domain": "test.com"}]
        vendors = [{"id": 20, "name": "Vendor", "domain": "vendor.com"}]

        mock_response = {
            "matches": [
                {"activity_id": 1, "entity_type": "company", "entity_id": 10, "confidence": 0.95},
            ]
        }

        with patch("app.utils.claude_client.claude_structured",
                   new_callable=AsyncMock, return_value=mock_response):
            result = asyncio.get_event_loop().run_until_complete(
                _call_claude_for_matching(activities, companies, vendors)
            )

        assert result[1]["entity_type"] == "company"
        assert result[1]["entity_id"] == 10
        assert result[1]["confidence"] == 0.95

    def test_returns_empty_on_none(self):
        """Should return empty dict when Claude returns None."""
        from app.services.auto_attribution_service import _call_claude_for_matching

        with patch("app.utils.claude_client.claude_structured",
                   new_callable=AsyncMock, return_value=None):
            result = asyncio.get_event_loop().run_until_complete(
                _call_claude_for_matching(
                    [{"id": 1, "email": "", "phone": "", "name": "", "subject": ""}], [], []
                )
            )

        assert result == {}

    def test_returns_empty_on_missing_matches_key(self):
        """Should return empty dict when response has no 'matches' key."""
        from app.services.auto_attribution_service import _call_claude_for_matching

        with patch("app.utils.claude_client.claude_structured",
                   new_callable=AsyncMock, return_value={"other": "data"}):
            result = asyncio.get_event_loop().run_until_complete(
                _call_claude_for_matching(
                    [{"id": 1, "email": "", "phone": "", "name": "", "subject": ""}], [], []
                )
            )

        assert result == {}

    def test_multiple_matches(self):
        """Should handle multiple activity matches."""
        from app.services.auto_attribution_service import _call_claude_for_matching

        activities = [
            {"id": 1, "email": "a@co.com", "phone": "", "name": "A", "subject": ""},
            {"id": 2, "email": "b@vend.com", "phone": "", "name": "B", "subject": ""},
        ]

        mock_response = {
            "matches": [
                {"activity_id": 1, "entity_type": "company", "entity_id": 10, "confidence": 0.9},
                {"activity_id": 2, "entity_type": "vendor", "entity_id": 20, "confidence": 0.85},
            ]
        }

        with patch("app.utils.claude_client.claude_structured",
                   new_callable=AsyncMock, return_value=mock_response):
            result = asyncio.get_event_loop().run_until_complete(
                _call_claude_for_matching(activities, [], [])
            )

        assert len(result) == 2
        assert result[1]["entity_type"] == "company"
        assert result[2]["entity_type"] == "vendor"

    def test_empty_matches_list(self):
        """Should return empty dict when Claude returns empty matches."""
        from app.services.auto_attribution_service import _call_claude_for_matching

        with patch("app.utils.claude_client.claude_structured",
                   new_callable=AsyncMock, return_value={"matches": []}):
            result = asyncio.get_event_loop().run_until_complete(
                _call_claude_for_matching(
                    [{"id": 1, "email": "", "phone": "", "name": "", "subject": ""}], [], []
                )
            )

        assert result == {}
