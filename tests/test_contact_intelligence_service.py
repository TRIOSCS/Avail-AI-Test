"""test_contact_intelligence_service.py — tests for the residual contact-intelligence
service (generate_contact_summary) and the vendor-contact API endpoints.

The auto-discovery / scoring / nudge layer was deleted in the Wave 2 simplification
sweep (spec §5.4) — its tests went with it.

Called by: pytest
Depends on: app/services/contact_intelligence.py, app/routers/vendor_contacts.py
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.models import ActivityLog, VendorCard, VendorContact
from app.services.contact_intelligence import generate_contact_summary

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


# ── _run_sync_or_return_empty ────────────────────────────────────────


# ── Field-level update tests for existing contacts (lines 124-136) ─────


# ── VendorContact flush conflict (lines 162-165) ──────────────────────


# ── ActivityLog flush error (lines 187-189) ──────────────────────────


# ── Pipeline event flush error (lines 270-273) ───────────────────────


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
