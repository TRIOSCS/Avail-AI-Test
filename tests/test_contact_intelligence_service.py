"""
test_contact_intelligence_service.py — DB-level tests for contact intelligence.

Tests the service functions that require a database session:
  - process_inbound_email_contact
  - log_pipeline_event
  - compute_all_contact_scores
  - generate_contact_nudges
  - generate_contact_summary

Also tests the new API endpoints:
  - GET /api/vendors/{card_id}/contacts (enhanced response)
  - GET /api/vendors/{card_id}/contacts/{contact_id}/timeline
  - GET /api/vendors/{card_id}/contact-nudges
  - GET /api/vendors/{card_id}/contacts/{contact_id}/summary
  - POST /api/vendors/{card_id}/contacts/{contact_id}/log-call

Called by: pytest
Depends on: app/services/contact_intelligence.py, app/routers/vendors.py
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, VendorCard, VendorContact
from app.services.contact_intelligence import (
    compute_all_contact_scores,
    generate_contact_nudges,
    generate_contact_summary,
    log_pipeline_event,
    process_inbound_email_contact,
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
        created_at=datetime.now(timezone.utc),
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
        "occurred_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    a = ActivityLog(**defaults)
    db.add(a)
    db.flush()
    return a


# ── process_inbound_email_contact ──────────────────────────────────


class TestProcessInboundEmailContact:
    def test_creates_new_contact(self, db_session, test_user):
        """New email from known domain creates VendorContact + ActivityLog."""
        card = _make_card(db_session, "Acme Corp", "acme.com")
        db_session.commit()

        with patch(
            "app.services.contact_intelligence._run_sync_or_return_empty",
            return_value={"full_name": "Jane Sales", "title": "VP Sales", "phone": "+1-555-9999", "confidence": 0.8},
        ), patch("app.services.signature_parser.cache_signature_extract"):
            vc = process_inbound_email_contact(
                db_session,
                sender_email="jane@acme.com",
                sender_name="Jane Sales",
                body="Hello\n--\nJane Sales\nVP Sales\n+1-555-9999",
                subject="Re: RFQ",
                received_at=datetime.now(timezone.utc),
                user_id=test_user.id,
            )

        assert vc is not None
        assert vc.email == "jane@acme.com"
        assert vc.full_name == "Jane Sales"
        assert vc.vendor_card_id == card.id
        assert vc.interaction_count == 1

        # ActivityLog created
        al = db_session.query(ActivityLog).filter_by(vendor_contact_id=vc.id).first()
        assert al is not None
        assert al.activity_type == "email_received"
        assert al.auto_logged is True

    def test_updates_existing_contact(self, db_session, test_user):
        """Repeat email updates interaction count, not name."""
        card = _make_card(db_session, "Beta Inc", "beta.com")
        existing = _make_contact(db_session, card, "bob@beta.com", full_name="Bob Beta")
        existing.interaction_count = 5
        db_session.commit()

        with patch(
            "app.services.contact_intelligence._run_sync_or_return_empty",
            return_value={"confidence": 0.5},
        ), patch("app.services.signature_parser.cache_signature_extract"):
            vc = process_inbound_email_contact(
                db_session,
                sender_email="bob@beta.com",
                sender_name="Bob",
                body="Hi there",
                subject="Quote",
                received_at=datetime.now(timezone.utc),
                user_id=test_user.id,
            )

        assert vc is not None
        assert vc.full_name == "Bob Beta"  # Not overwritten
        assert vc.interaction_count == 6

    def test_no_card_returns_none(self, db_session, test_user):
        """Email from unknown domain returns None."""
        with patch(
            "app.services.contact_intelligence._run_sync_or_return_empty",
            return_value={"confidence": 0.5},
        ), patch("app.services.signature_parser.cache_signature_extract"):
            vc = process_inbound_email_contact(
                db_session,
                sender_email="nobody@unknown.com",
                sender_name="Nobody",
                body="Hello",
                subject="Hi",
                received_at=datetime.now(timezone.utc),
                user_id=test_user.id,
            )
        assert vc is None

    def test_invalid_email_returns_none(self, db_session, test_user):
        vc = process_inbound_email_contact(
            db_session,
            sender_email="not-an-email",
            sender_name="X",
            body="",
            subject="",
            received_at=None,
            user_id=test_user.id,
        )
        assert vc is None

    def test_empty_email_returns_none(self, db_session, test_user):
        vc = process_inbound_email_contact(
            db_session,
            sender_email="",
            sender_name="X",
            body="",
            subject="",
            received_at=None,
            user_id=test_user.id,
        )
        assert vc is None

    def test_no_name_no_sig_returns_none(self, db_session, test_user):
        """Can't create contact without a name."""
        card = _make_card(db_session, "Gamma Ltd", "gamma.com")
        db_session.commit()

        with patch(
            "app.services.contact_intelligence._run_sync_or_return_empty",
            return_value={"confidence": 0.3},
        ), patch("app.services.signature_parser.cache_signature_extract"):
            vc = process_inbound_email_contact(
                db_session,
                sender_email="anon@gamma.com",
                sender_name=None,
                body="",
                subject="",
                received_at=None,
                user_id=test_user.id,
            )
        assert vc is None

    def test_signature_extraction_failure_continues(self, db_session, test_user):
        """Signature extraction error doesn't break the pipeline."""
        card = _make_card(db_session, "Delta Co", "delta.com")
        db_session.commit()

        with patch(
            "app.services.contact_intelligence._run_sync_or_return_empty",
            side_effect=Exception("parse error"),
        ):
            vc = process_inbound_email_contact(
                db_session,
                sender_email="dan@delta.com",
                sender_name="Dan Delta",
                body="Hey",
                subject="Test",
                received_at=datetime.now(timezone.utc),
                user_id=test_user.id,
            )

        assert vc is not None
        assert vc.full_name == "Dan Delta"


# ── log_pipeline_event ─────────────────────────────────────────────


class TestLogPipelineEvent:
    def test_logs_event_with_contact_resolution(self, db_session, test_user):
        card = _make_card(db_session, "Epsilon", "epsilon.com")
        vc = _make_contact(db_session, card, "eve@epsilon.com", full_name="Eve Epsilon")
        vc.interaction_count = 3
        db_session.commit()

        al = log_pipeline_event(
            db_session,
            user_id=test_user.id,
            event_type="rfq_sent",
            vendor_card_id=card.id,
            contact_email="eve@epsilon.com",
            notes="Sent RFQ for LM317T",
        )

        assert al is not None
        assert al.activity_type == "rfq_sent"
        assert al.vendor_contact_id == vc.id
        assert al.auto_logged is True

        db_session.refresh(vc)
        assert vc.interaction_count == 4

    def test_logs_event_without_contact(self, db_session, test_user):
        card = _make_card(db_session, "Zeta", "zeta.com")
        db_session.commit()

        al = log_pipeline_event(
            db_session,
            user_id=test_user.id,
            event_type="po_issued",
            vendor_card_id=card.id,
        )

        assert al is not None
        assert al.vendor_contact_id is None
        assert al.activity_type == "po_issued"

    def test_logs_event_no_card(self, db_session, test_user):
        """Event without vendor_card_id still works."""
        al = log_pipeline_event(
            db_session,
            user_id=test_user.id,
            event_type="quote_received",
            notes="Generic event",
        )
        assert al is not None
        assert al.activity_type == "quote_received"


# ── compute_all_contact_scores ──────────────────────────────────────


class TestComputeAllContactScores:
    def test_empty_db(self, db_session):
        result = compute_all_contact_scores(db_session)
        assert result == {"updated": 0, "skipped": 0}

    def test_scores_contacts_with_activity(self, db_session, test_user):
        card = _make_card(db_session, "ScoreCo", "scoreco.com")
        vc = _make_contact(db_session, card, "rep@scoreco.com")
        vc.last_interaction_at = datetime.now(timezone.utc) - timedelta(days=3)

        # Create varied activities
        now = datetime.now(timezone.utc)
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
        vc.last_interaction_at = datetime.now(timezone.utc) - timedelta(days=1)

        now = datetime.now(timezone.utc)
        # 5 regular activities
        for i in range(5):
            _make_activity(db_session, test_user.id, vc, occurred_at=now - timedelta(days=i))
        # 3 win activities
        for i in range(3):
            _make_activity(
                db_session, test_user.id, vc,
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
        vc.last_interaction_at = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)
        _make_activity(db_session, test_user.id, vc, channel="outlook", occurred_at=now)
        _make_activity(db_session, test_user.id, vc, channel="phone", occurred_at=now)
        _make_activity(db_session, test_user.id, vc, channel="avail_system", occurred_at=now)
        db_session.commit()

        compute_all_contact_scores(db_session)
        db_session.refresh(vc)
        # 3 distinct channels → channel_score should be 100
        assert vc.relationship_score is not None


# ── generate_contact_nudges ─────────────────────────────────────────


class TestGenerateContactNudges:
    def test_no_contacts_returns_empty(self, db_session):
        card = _make_card(db_session, "Empty", "empty.com")
        db_session.commit()
        assert generate_contact_nudges(db_session, card.id) == []

    def test_dormant_contact_produces_nudge(self, db_session, test_user):
        card = _make_card(db_session, "DormantCo", "dormantco.com")
        vc = _make_contact(db_session, card, "old@dormantco.com", full_name="Old Contact")
        vc.activity_trend = "dormant"
        vc.last_interaction_at = datetime.now(timezone.utc) - timedelta(days=45)
        db_session.commit()

        nudges = generate_contact_nudges(db_session, card.id)
        assert len(nudges) == 1
        assert nudges[0]["nudge_type"] == "dormant"
        assert nudges[0]["contact_name"] == "Old Contact"
        assert nudges[0]["days_since_contact"] >= 44

    def test_cooling_contact_produces_nudge(self, db_session, test_user):
        card = _make_card(db_session, "CoolCo", "coolco.com")
        vc = _make_contact(db_session, card, "cool@coolco.com", full_name="Cool Guy")
        vc.activity_trend = "cooling"
        vc.last_interaction_at = datetime.now(timezone.utc) - timedelta(days=20)
        db_session.commit()

        nudges = generate_contact_nudges(db_session, card.id)
        assert len(nudges) == 1
        assert nudges[0]["nudge_type"] == "cooling"

    def test_stable_healthy_no_nudge(self, db_session, test_user):
        card = _make_card(db_session, "StableCo", "stableco.com")
        vc = _make_contact(db_session, card, "ok@stableco.com")
        vc.activity_trend = "stable"
        vc.relationship_score = 75.0
        vc.last_interaction_at = datetime.now(timezone.utc) - timedelta(days=5)
        db_session.commit()

        nudges = generate_contact_nudges(db_session, card.id)
        assert len(nudges) == 0

    def test_warming_no_nudge(self, db_session, test_user):
        card = _make_card(db_session, "WarmCo", "warmco.com")
        vc = _make_contact(db_session, card, "hot@warmco.com")
        vc.activity_trend = "warming"
        vc.relationship_score = 60.0
        vc.last_interaction_at = datetime.now(timezone.utc) - timedelta(days=2)
        db_session.commit()

        nudges = generate_contact_nudges(db_session, card.id)
        assert len(nudges) == 0

    def test_no_trend_dormant_threshold(self, db_session, test_user):
        """Contact with no trend but old last interaction → dormant nudge."""
        card = _make_card(db_session, "NullTrend", "nulltrend.com")
        vc = _make_contact(db_session, card, "null@nulltrend.com", full_name="Null")
        vc.activity_trend = None
        vc.last_interaction_at = datetime.now(timezone.utc) - timedelta(days=40)
        db_session.commit()

        nudges = generate_contact_nudges(db_session, card.id)
        assert len(nudges) == 1
        assert nudges[0]["nudge_type"] == "dormant"


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

    def test_template_summary_no_gradient(self, db_session, test_user):
        card = _make_card(db_session, "TemplCo", "templco.com")
        vc = _make_contact(db_session, card, "tmpl@templco.com", full_name="Template Person")
        vc.interaction_count = 10
        vc.activity_trend = "stable"
        vc.relationship_score = 65.0
        db_session.commit()

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
            db_session, test_user.id, vc,
            occurred_at=datetime.now(timezone.utc) - timedelta(days=1),
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
        vc.score_computed_at = datetime.now(timezone.utc)
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
        now = datetime.now(timezone.utc)
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

        resp = client.get(
            f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/timeline"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["activity_type"] == "email_received"
        assert data[0]["subject"] == "Test email"

    def test_contact_timeline_not_found(self, client, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/99999/timeline")
        assert resp.status_code == 404

    def test_contact_nudges_endpoint(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "dormant@arrow.com", full_name="Dormant Person")
        vc.activity_trend = "dormant"
        vc.last_interaction_at = datetime.now(timezone.utc) - timedelta(days=45)
        db_session.commit()

        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contact-nudges")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["nudge_type"] == "dormant"

    def test_contact_nudges_not_found(self, client):
        resp = client.get("/api/vendors/99999/contact-nudges")
        assert resp.status_code == 404

    def test_contact_summary_endpoint(self, client, db_session, test_vendor_card, test_vendor_contact):
        test_vendor_contact.interaction_count = 5
        test_vendor_contact.activity_trend = "cooling"
        test_vendor_contact.relationship_score = 40.0
        db_session.commit()

        resp = client.get(
            f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/summary"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert isinstance(data["summary"], str)

    def test_log_call_endpoint(self, client, db_session, test_vendor_card, test_vendor_contact):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/log-call"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "activity_id" in data

        # Verify ActivityLog created
        al = (
            db_session.query(ActivityLog)
            .filter_by(vendor_contact_id=test_vendor_contact.id, activity_type="call_initiated")
            .first()
        )
        assert al is not None
        assert al.channel == "phone"

    def test_log_call_not_found(self, client, test_vendor_card):
        resp = client.post(f"/api/vendors/{test_vendor_card.id}/contacts/99999/log-call")
        assert resp.status_code == 404


# ── _run_sync_or_return_empty ────────────────────────────────────────


class TestRunSyncHelper:
    def test_returns_empty_on_exception(self):
        from app.services.contact_intelligence import _run_sync_or_return_empty

        async def bad_fn():
            raise RuntimeError("boom")

        result = _run_sync_or_return_empty(bad_fn)
        assert result == {}

    def test_runs_sync_successfully(self):
        from app.services.contact_intelligence import _run_sync_or_return_empty

        async def good_fn(x):
            return {"value": x}

        result = _run_sync_or_return_empty(good_fn, 42)
        assert result == {"value": 42}
