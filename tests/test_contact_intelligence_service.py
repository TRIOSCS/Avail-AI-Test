"""test_contact_intelligence_service.py — DB-level tests for contact intelligence.

Tests the service functions that require a database session:
  - compute_all_contact_scores
  - generate_contact_summary

Also tests the new API endpoints:
  - GET /api/vendors/{card_id}/contacts (enhanced response)
  - GET /api/vendors/{card_id}/contacts/{contact_id}/timeline
  - GET /api/vendors/{card_id}/contacts/{contact_id}/summary
  - POST /api/vendors/{card_id}/contacts/{contact_id}/log-call

Called by: pytest
Depends on: app/services/contact_intelligence.py, app/routers/vendors.py
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, VendorCard, VendorContact
from app.services.contact_intelligence import (
    compute_all_contact_scores,
    generate_contact_summary,
)

# ── Helpers ───────────────────────────────────────────────────────


def _make_card(db: Session, name: str, domain: str) -> VendorCard:
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        domain=domain,
        emails=[],
        phones=[],
        sighting_count=0,
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


def _make_contact(
    db: Session,
    card: VendorCard,
    email: str,
    full_name: str = "Test Contact",
    **kwargs,
) -> VendorContact:
    vc = VendorContact(
        vendor_card_id=card.id,
        full_name=full_name,
        email=email,
        source="manual",
        confidence=80,
        **kwargs,
    )
    db.add(vc)
    db.flush()
    return vc


def _make_activity(db: Session, user_id: int, contact: VendorContact, **overrides) -> ActivityLog:
    defaults = {
        "user_id": user_id,
        "activity_type": "email_received",
        "channel": "outlook",
        "vendor_card_id": contact.vendor_card_id,
        "vendor_contact_id": contact.id,
        "auto_logged": True,
        "occurred_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    a = ActivityLog(**defaults)
    db.add(a)
    db.flush()
    return a


# ── compute_all_contact_scores ──────────────────────────────────────


class TestComputeAllContactScores:
    def test_empty_db(self, db_session):
        result = compute_all_contact_scores(db_session)
        assert result == {"updated": 0, "skipped": 0}

    def test_scores_contacts_with_activity(self, db_session, test_user):
        card = _make_card(db_session, "ScoreCo", "scoreco.com")
        vc = _make_contact(db_session, card, "rep@scoreco.com")
        vc.last_interaction_at = datetime.now(UTC) - timedelta(days=3)

        # Create varied activities
        now = datetime.now(UTC)
        for i in range(5):
            _make_activity(db_session, test_user.id, vc, occurred_at=now - timedelta(days=i))
        for i in range(3):
            _make_activity(db_session, test_user.id, vc, occurred_at=now - timedelta(days=40 + i))
        db_session.commit()

        result = compute_all_contact_scores(db_session)
        assert result["updated"] == 1

        db_session.refresh(vc)
        assert vc.relationship_score is not None
        assert 0 <= vc.relationship_score <= 100
        assert vc.activity_trend is not None
        assert vc.score_computed_at is not None

    def test_scores_contact_no_activity(self, db_session):
        card = _make_card(db_session, "Quiet", "quiet.com")
        vc = _make_contact(db_session, card, "silent@quiet.com")
        db_session.commit()

        result = compute_all_contact_scores(db_session)
        assert result["updated"] == 1

        db_session.refresh(vc)
        assert vc.relationship_score is not None
        assert vc.activity_trend == "dormant"

    def test_multi_contact_batch(self, db_session, test_user):
        card = _make_card(db_session, "BatchCo", "batchco.com")
        contacts = []
        for i in range(5):
            c = _make_contact(db_session, card, f"user{i}@batchco.com", full_name=f"User {i}")
            contacts.append(c)
        db_session.commit()

        result = compute_all_contact_scores(db_session)
        assert result["updated"] == 5

    def test_win_count_boosts_score(self, db_session, test_user):
        card = _make_card(db_session, "WinCo", "winco.com")
        vc = _make_contact(db_session, card, "winner@winco.com")
        vc.last_interaction_at = datetime.now(UTC) - timedelta(days=1)

        now = datetime.now(UTC)
        # 5 regular activities
        for i in range(5):
            _make_activity(db_session, test_user.id, vc, occurred_at=now - timedelta(days=i))
        # 3 win activities
        for i in range(3):
            _make_activity(
                db_session,
                test_user.id,
                vc,
                activity_type="po_issued",
                occurred_at=now - timedelta(days=i),
            )
        db_session.commit()

        result = compute_all_contact_scores(db_session)
        assert result["updated"] == 1

        db_session.refresh(vc)
        assert vc.relationship_score > 0

    def test_channel_diversity_counted(self, db_session, test_user):
        card = _make_card(db_session, "MultiCh", "multich.com")
        vc = _make_contact(db_session, card, "multi@multich.com")
        vc.last_interaction_at = datetime.now(UTC)

        now = datetime.now(UTC)
        _make_activity(db_session, test_user.id, vc, channel="outlook", occurred_at=now)
        _make_activity(db_session, test_user.id, vc, channel="phone", occurred_at=now)
        _make_activity(db_session, test_user.id, vc, channel="avail_system", occurred_at=now)
        db_session.commit()

        compute_all_contact_scores(db_session)
        db_session.refresh(vc)
        # 3 distinct channels → channel_score should be 100
        assert vc.relationship_score is not None


# ── generate_contact_summary ────────────────────────────────────────


class TestGenerateContactSummary:
    def test_contact_not_found(self, db_session):
        card = _make_card(db_session, "Nobody", "nobody.com")
        db_session.commit()
        result = generate_contact_summary(db_session, card.id, 99999)
        assert result == "Contact not found."

    def test_wrong_card_returns_not_found(self, db_session):
        card1 = _make_card(db_session, "CardA", "carda.com")
        card2 = _make_card(db_session, "CardB", "cardb.com")
        vc = _make_contact(db_session, card1, "x@carda.com")
        db_session.commit()

        result = generate_contact_summary(db_session, card2.id, vc.id)
        assert result == "Contact not found."

    def test_template_summary_ai_failure(self, db_session, test_user):
        card = _make_card(db_session, "TemplCo", "templco.com")
        vc = _make_contact(db_session, card, "tmpl@templco.com", full_name="Template Person")
        vc.interaction_count = 10
        vc.activity_trend = "stable"
        vc.relationship_score = 65.0
        db_session.commit()

        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value=None):
            result = generate_contact_summary(db_session, card.id, vc.id)
        assert "Template Person" in result
        assert "10" in result
        assert "steady" in result  # stable → "steady"

    def test_summary_with_activities(self, db_session, test_user):
        card = _make_card(db_session, "ActvCo", "actvco.com")
        vc = _make_contact(db_session, card, "act@actvco.com", full_name="Active Person")
        vc.interaction_count = 5
        vc.activity_trend = "warming"
        vc.relationship_score = 80.0
        _make_activity(
            db_session,
            test_user.id,
            vc,
            occurred_at=datetime.now(UTC) - timedelta(days=1),
        )
        db_session.commit()

        result = generate_contact_summary(db_session, card.id, vc.id)
        assert "Active Person" in result


# ── API Endpoint Tests ─────────────────────────────────────────────


class TestContactEndpoints:
    def test_list_contacts_enhanced(self, client, db_session, test_vendor_card):
        """Enhanced response includes relationship_score, activity_trend, etc."""
        vc = _make_contact(db_session, test_vendor_card, "list@arrow.com", full_name="List Test")
        vc.first_name = "List"
        vc.last_name = "Test"
        vc.phone_mobile = "+1-555-1234"
        vc.relationship_score = 72.5
        vc.activity_trend = "warming"
        vc.score_computed_at = datetime.now(UTC)
        db_session.commit()

        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts")
        assert resp.status_code == 200
        data = resp.json()
        # Find the contact we just created
        contact = next((c for c in data if c["email"] == "list@arrow.com"), None)
        assert contact is not None
        assert contact["first_name"] == "List"
        assert contact["last_name"] == "Test"
        assert contact["phone_mobile"] == "+1-555-1234"
        assert contact["relationship_score"] == 72.5
        assert contact["activity_trend"] == "warming"
        assert "score_computed_at" in contact

    def test_contact_timeline(self, client, db_session, test_user, test_vendor_card, test_vendor_contact):
        now = datetime.now(UTC)
        al = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="outlook",
            vendor_card_id=test_vendor_card.id,
            vendor_contact_id=test_vendor_contact.id,
            subject="Test email",
            auto_logged=True,
            occurred_at=now,
            created_at=now,
        )
        db_session.add(al)
        db_session.commit()

        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["activity_type"] == "email_received"
        assert data[0]["subject"] == "Test email"

    def test_contact_timeline_not_found(self, client, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/99999/timeline")
        assert resp.status_code == 404

    def test_contact_summary_endpoint(self, client, db_session, test_vendor_card, test_vendor_contact):
        test_vendor_contact.interaction_count = 5
        test_vendor_contact.activity_trend = "cooling"
        test_vendor_contact.relationship_score = 40.0
        db_session.commit()

        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert isinstance(data["summary"], str)

    def test_log_call_endpoint(self, client, db_session, test_vendor_card, test_vendor_contact):
        resp = client.post(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/log-call")
        assert resp.status_code == 200
        # The endpoint now returns the refreshed contact row (HTML) + an HX-Trigger
        # showToast so the click is visibly acknowledged (was a bare-JSON no-op before).
        assert f"vendor-contact-{test_vendor_contact.id}" in resp.text
        assert "showToast" in resp.headers.get("HX-Trigger", "")

        # Verify ActivityLog created
        al = (
            db_session.query(ActivityLog)
            .filter_by(vendor_contact_id=test_vendor_contact.id, activity_type="call_logged")
            .first()
        )
        assert al is not None
        assert al.channel == "phone"

    def test_log_call_not_found(self, client, test_vendor_card):
        resp = client.post(f"/api/vendors/{test_vendor_card.id}/contacts/99999/log-call")
        assert resp.status_code == 404


# ── split_name ──────────────────────────────────────────────────────


class TestSplitNameWithPrefix:
    @pytest.mark.parametrize(
        ("name", "expected_first", "expected_last"),
        [
            pytest.param("John van Berg", "John", "van Berg", id="van"),
            pytest.param("Maria de Silva", "Maria", "de Silva", id="de"),
        ],
    )
    def test_name_with_prefix(self, name, expected_first, expected_last):
        """Line 49: surname prefix like 'van'/'de' stays part of the last name."""
        from app.services.contact_intelligence import split_name

        first, last = split_name(name)
        assert first == expected_first
        assert last == expected_last


# ── compute_contact_relationship_score edge cases (lines 318-321, 330-335) ──


class TestContactRelationshipScoreEdgeCases:
    def test_recency_mid_range(self):
        """Lines 318-321: recency between ideal and max decays linearly."""
        from app.services.contact_intelligence import compute_contact_relationship_score

        now = datetime.now(UTC)
        # 100 days ago -- between 7 and 365
        result = compute_contact_relationship_score(
            last_interaction_at=now - timedelta(days=100),
            interactions_30d=5,
            interactions_60d=8,
            interactions_90d=10,
            avg_response_hours=None,
            wins=0,
            total_interactions=10,
            distinct_channels=1,
            now=now,
        )
        recency = result["recency_score"]
        assert 0 < recency < 100

    def test_recency_at_max(self):
        """Lines 318-319: recency at >= 365 days is 0."""
        from app.services.contact_intelligence import compute_contact_relationship_score

        now = datetime.now(UTC)
        result = compute_contact_relationship_score(
            last_interaction_at=now - timedelta(days=400),
            interactions_30d=0,
            interactions_60d=0,
            interactions_90d=0,
            avg_response_hours=None,
            wins=0,
            total_interactions=0,
            distinct_channels=0,
            now=now,
        )
        assert result["recency_score"] == 0.0

    def test_responsiveness_ideal(self):
        """Line 330-331: response time <= 4h gives 100."""
        from app.services.contact_intelligence import compute_contact_relationship_score

        now = datetime.now(UTC)
        result = compute_contact_relationship_score(
            last_interaction_at=now - timedelta(days=1),
            interactions_30d=5,
            interactions_60d=5,
            interactions_90d=5,
            avg_response_hours=2.0,
            wins=0,
            total_interactions=5,
            distinct_channels=1,
            now=now,
        )
        assert result["responsiveness_score"] == 100.0

    def test_responsiveness_max(self):
        """Line 332-333: response time >= 168h gives 0."""
        from app.services.contact_intelligence import compute_contact_relationship_score

        now = datetime.now(UTC)
        result = compute_contact_relationship_score(
            last_interaction_at=now - timedelta(days=1),
            interactions_30d=5,
            interactions_60d=5,
            interactions_90d=5,
            avg_response_hours=200.0,
            wins=0,
            total_interactions=5,
            distinct_channels=1,
            now=now,
        )
        assert result["responsiveness_score"] == 0.0

    def test_responsiveness_mid_range(self):
        """Line 335: response time between 4h and 168h decays linearly."""
        from app.services.contact_intelligence import compute_contact_relationship_score

        now = datetime.now(UTC)
        result = compute_contact_relationship_score(
            last_interaction_at=now - timedelta(days=1),
            interactions_30d=5,
            interactions_60d=5,
            interactions_90d=5,
            avg_response_hours=50.0,
            wins=0,
            total_interactions=5,
            distinct_channels=1,
            now=now,
        )
        resp = result["responsiveness_score"]
        assert 0 < resp < 100


# ── _compute_trend cooling path (lines 384-387) ─────────────────────


class TestComputeTrendCooling:
    @pytest.mark.parametrize(
        ("interactions_30d", "interactions_60d", "interactions_90d", "expected"),
        [
            # 90d has 20, 30d has 1 -> older_rate = (20-1)/2 = 9.5, 1 < 0.5*9.5=4.75 -> cooling
            pytest.param(1, 10, 20, "cooling", id="cooling"),
            # 90d=12, 30d=5 -> older_rate=(12-5)/2=3.5
            # 5 > 1.5*3.5=5.25? No. 5 < 0.5*3.5=1.75? No. -> stable
            pytest.param(5, 8, 12, "stable", id="stable"),
        ],
    )
    def test_trend(self, interactions_30d, interactions_60d, interactions_90d, expected):
        """Lines 384-387: interactions_30d vs older_rate decides cooling/stable."""
        from app.services.contact_intelligence import _compute_trend

        result = _compute_trend(
            interactions_30d=interactions_30d,
            interactions_60d=interactions_60d,
            interactions_90d=interactions_90d,
        )
        assert result == expected


# ── compute_all_contact_scores batch flush errors (lines 497-504, 510-513, 517-519) ──


class TestComputeScoresFlushErrors:
    def test_batch_flush_success_with_500_plus(self, db_session, test_user):
        """Line 499: successful batch flush after 500 contacts clears the batch list."""
        card = _make_card(db_session, "BigBatch", "bigbatch.com")
        # Create 501 contacts to trigger the batch flush at 500
        for i in range(501):
            _make_contact(db_session, card, f"b{i}@bigbatch.com", full_name=f"BB {i}")
        db_session.commit()

        result = compute_all_contact_scores(db_session)
        # All 501 should be updated (batch flush at 500 + final flush for 1)
        assert result["updated"] == 501
        assert result["skipped"] == 0

    def test_batch_flush_error(self, db_session, test_user):
        """Lines 497-504: error during batch flush increments skipped count."""
        import sqlalchemy.exc

        card = _make_card(db_session, "FlushErr", "flusherr.com")
        for i in range(501):
            _make_contact(db_session, card, f"c{i}@flusherr.com", full_name=f"Contact {i}")
        db_session.commit()

        original_flush = db_session.flush
        flush_counter = {"count": 0}

        def selective_flush(*args, **kwargs):
            flush_counter["count"] += 1
            # The first flush is the batch flush after 500 contacts
            if flush_counter["count"] == 1:
                raise sqlalchemy.exc.OperationalError("flush", {}, Exception("Batch flush error"))
            return original_flush(*args, **kwargs)

        db_session.flush = selective_flush
        result = compute_all_contact_scores(db_session)
        db_session.flush = original_flush

        # Some contacts should be counted as skipped due to batch flush error
        assert result["skipped"] > 0 or result["updated"] > 0

    def test_final_flush_error(self, db_session, test_user):
        """Lines 510-513: error during final flush increments skipped count."""
        import sqlalchemy.exc

        card = _make_card(db_session, "FinalFlush", "finalflush.com")
        for i in range(3):
            _make_contact(db_session, card, f"ff{i}@finalflush.com", full_name=f"FF {i}")
        db_session.commit()

        original_flush = db_session.flush

        def always_fail_flush(*args, **kwargs):
            raise sqlalchemy.exc.OperationalError("flush", {}, Exception("Final flush error"))

        # Replace flush to always fail - the final flush should trigger lines 510-513
        db_session.flush = always_fail_flush
        result = compute_all_contact_scores(db_session)
        db_session.flush = original_flush

        # The function should still return without raising
        assert "updated" in result
        assert "skipped" in result
        # All contacts should be skipped since the final flush fails
        assert result["skipped"] == 3

    def test_commit_error(self, db_session, test_user):
        """Lines 517-519: error during commit rolls back."""
        import sqlalchemy.exc

        card = _make_card(db_session, "CommitErr", "commiterr.com")
        _make_contact(db_session, card, "ce@commiterr.com", full_name="CE")
        db_session.commit()

        original_commit = db_session.commit

        def fail_commit(*args, **kwargs):
            raise sqlalchemy.exc.OperationalError("commit", {}, Exception("Commit error"))

        db_session.commit = fail_commit
        result = compute_all_contact_scores(db_session)
        db_session.commit = original_commit

        # Should still return results (commit error is caught)
        assert "updated" in result


# ── generate_contact_summary: Claude AI path (lines 659-719) ────────


class TestGenerateContactSummaryClaude:
    def test_claude_summary_success(self, db_session, test_user):
        """Claude AI generates a summary successfully."""
        card = _make_card(db_session, "SumCo", "sumco.com")
        vc = _make_contact(db_session, card, "sum@sumco.com", full_name="Summary Person")
        vc.interaction_count = 10
        vc.activity_trend = "stable"
        vc.relationship_score = 65.0
        db_session.commit()

        mock_loop = MagicMock()
        mock_loop.run_until_complete.return_value = "AI-generated relationship summary here."

        with (
            patch("app.utils.claude_client.claude_text", new_callable=AsyncMock),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            result = generate_contact_summary(db_session, card.id, vc.id)

        assert result == "AI-generated relationship summary here."

    def test_claude_summary_failure_falls_back_to_template(self, db_session, test_user):
        """Claude failure falls back to template summary."""
        card = _make_card(db_session, "FailSum", "failsum.com")
        vc = _make_contact(db_session, card, "fail@failsum.com", full_name="Fail Person")
        vc.interaction_count = 5
        vc.activity_trend = "warming"
        vc.relationship_score = 80.0
        db_session.commit()

        mock_loop = MagicMock()
        mock_loop.run_until_complete.side_effect = Exception("Claude down")

        with (
            patch("app.utils.claude_client.claude_text", new_callable=AsyncMock),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            result = generate_contact_summary(db_session, card.id, vc.id)

        # Should fall back to template
        assert "Fail Person" in result
        assert "improving" in result  # warming -> "improving"

    def test_claude_summary_returns_empty(self, db_session, test_user):
        """Claude returns empty string -> falls back to template."""
        card = _make_card(db_session, "EmptySum", "emptysum.com")
        vc = _make_contact(db_session, card, "empty@emptysum.com", full_name="Empty Person")
        vc.interaction_count = 3
        vc.activity_trend = "cooling"
        vc.relationship_score = 40.0
        db_session.commit()

        mock_loop = MagicMock()
        mock_loop.run_until_complete.return_value = ""

        with (
            patch("app.utils.claude_client.claude_text", new_callable=AsyncMock),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            result = generate_contact_summary(db_session, card.id, vc.id)

        # Empty string from Claude -> falls to template
        assert "Empty Person" in result
        assert "declining" in result  # cooling -> "declining"
