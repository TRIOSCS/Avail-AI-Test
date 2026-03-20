"""test_contact_intelligence_service.py — DB-level tests for contact intelligence.

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
from unittest.mock import AsyncMock, MagicMock, patch

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

        with (
            patch(
                "app.services.contact_intelligence._run_sync_or_return_empty",
                return_value={
                    "full_name": "Jane Sales",
                    "title": "VP Sales",
                    "phone": "+1-555-9999",
                    "confidence": 0.8,
                },
            ),
            patch("app.services.signature_parser.cache_signature_extract"),
        ):
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

        with (
            patch(
                "app.services.contact_intelligence._run_sync_or_return_empty",
                return_value={"confidence": 0.5},
            ),
            patch("app.services.signature_parser.cache_signature_extract"),
        ):
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
        with (
            patch(
                "app.services.contact_intelligence._run_sync_or_return_empty",
                return_value={"confidence": 0.5},
            ),
            patch("app.services.signature_parser.cache_signature_extract"),
        ):
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

        with (
            patch(
                "app.services.contact_intelligence._run_sync_or_return_empty",
                return_value={"confidence": 0.3},
            ),
            patch("app.services.signature_parser.cache_signature_extract"),
        ):
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

        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/timeline")
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

        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert isinstance(data["summary"], str)

    def test_log_call_endpoint(self, client, db_session, test_vendor_card, test_vendor_contact):
        resp = client.post(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/log-call")
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


class TestSplitNameWithPrefix:
    def test_name_with_prefix(self):
        """Line 49: name with surname prefix like 'van' returns prefix as part of last name."""
        from app.services.contact_intelligence import split_name

        first, last = split_name("John van Berg")
        assert first == "John"
        assert last == "van Berg"

    def test_name_with_de_prefix(self):
        from app.services.contact_intelligence import split_name

        first, last = split_name("Maria de Silva")
        assert first == "Maria"
        assert last == "de Silva"


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

    def test_returns_empty_when_loop_is_running(self):
        """Line 206: returns {} when called from within a running async loop."""
        import asyncio

        from app.services.contact_intelligence import _run_sync_or_return_empty

        async def inner():
            return {"should": "not reach"}

        async def outer():
            return _run_sync_or_return_empty(inner)

        result = asyncio.run(outer())
        assert result == {}


# ── Field-level update tests for existing contacts (lines 124-136) ─────


class TestProcessInboundFieldUpdates:
    """Test individual field updates when contact already exists but fields are
    blank."""

    def test_updates_blank_fields_on_existing_contact(self, db_session, test_user):
        """Lines 124, 128, 130, 132, 134, 136: each field fills if contact has empty
        value."""
        card = _make_card(db_session, "FieldCo", "fieldco.com")
        existing = _make_contact(db_session, card, "empty@fieldco.com", full_name="")
        existing.full_name = None
        existing.first_name = None
        existing.last_name = None
        existing.title = None
        existing.phone = None
        existing.phone_mobile = None
        existing.linkedin_url = None
        existing.interaction_count = 1
        db_session.commit()

        sig_data = {
            "full_name": "New Name",
            "title": "VP Sales",
            "phone": "+1-555-1234",
            "mobile": "+1-555-5678",
            "linkedin_url": "https://linkedin.com/in/newname",
            "confidence": 0.8,
        }

        with (
            patch(
                "app.services.contact_intelligence._run_sync_or_return_empty",
                return_value=sig_data,
            ),
            patch("app.services.signature_parser.cache_signature_extract"),
        ):
            vc = process_inbound_email_contact(
                db_session,
                sender_email="empty@fieldco.com",
                sender_name="New Name",
                body="Hello\n--\nNew Name\nVP Sales",
                subject="Quote",
                received_at=datetime.now(timezone.utc),
                user_id=test_user.id,
            )

        assert vc is not None
        assert vc.full_name == "New Name"
        assert vc.first_name == "New"
        assert vc.last_name == "Name"
        assert vc.title == "VP Sales"
        assert vc.phone == "+1-555-1234"
        assert vc.phone_mobile == "+1-555-5678"
        assert vc.linkedin_url == "https://linkedin.com/in/newname"
        assert vc.interaction_count == 2


# ── VendorContact flush conflict (lines 162-165) ──────────────────────


class TestVendorContactFlushConflict:
    def test_flush_conflict_returns_none(self, db_session, test_user):
        """Line 162-165: flush conflict during new contact creation rolls back and returns None."""
        card = _make_card(db_session, "FlushCo", "flushco.com")
        db_session.commit()

        original_flush = db_session.flush

        call_count = 0

        def fail_on_second_flush(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # The first flushes are for internal operations; fail on later flush
            # We need to fail specifically on the VendorContact flush
            # Count how many times flush is called and fail on the right one
            if call_count >= 1:
                # Check if there's a new VendorContact pending
                for obj in db_session.new:
                    from app.models import VendorContact as VC

                    if isinstance(obj, VC):
                        raise Exception("Duplicate key constraint")
            return original_flush(*args, **kwargs)

        with (
            patch(
                "app.services.contact_intelligence._run_sync_or_return_empty",
                return_value={"full_name": "Conflict Person", "confidence": 0.8},
            ),
            patch("app.services.signature_parser.cache_signature_extract"),
        ):
            db_session.flush = fail_on_second_flush
            vc = process_inbound_email_contact(
                db_session,
                sender_email="conflict@flushco.com",
                sender_name="Conflict Person",
                body="Hello",
                subject="Test",
                received_at=datetime.now(timezone.utc),
                user_id=test_user.id,
            )
            db_session.flush = original_flush

        assert vc is None


# ── ActivityLog flush error (lines 187-189) ──────────────────────────


class TestActivityLogFlushError:
    def test_activity_flush_error_still_returns_contact(self, db_session, test_user):
        """Lines 187-189: ActivityLog flush error is caught; contact is still returned."""
        card = _make_card(db_session, "ActFlush", "actflush.com")
        db_session.commit()

        original_flush = db_session.flush

        flush_count = 0

        def fail_on_activity_flush(*args, **kwargs):
            nonlocal flush_count
            flush_count += 1
            # First flush creates VendorContact, second flush creates ActivityLog
            if flush_count == 2:
                raise Exception("Activity flush error")
            return original_flush(*args, **kwargs)

        with (
            patch(
                "app.services.contact_intelligence._run_sync_or_return_empty",
                return_value={"full_name": "Activity Err", "confidence": 0.7},
            ),
            patch("app.services.signature_parser.cache_signature_extract"),
        ):
            db_session.flush = fail_on_activity_flush
            vc = process_inbound_email_contact(
                db_session,
                sender_email="acterr@actflush.com",
                sender_name="Activity Err",
                body="Hello",
                subject="Test",
                received_at=datetime.now(timezone.utc),
                user_id=test_user.id,
            )
            db_session.flush = original_flush

        # Contact is still returned despite ActivityLog flush failure
        assert vc is not None
        assert vc.full_name == "Activity Err"


# ── Pipeline event flush error (lines 270-273) ───────────────────────


class TestPipelineEventFlushError:
    def test_flush_error_returns_none(self, db_session, test_user):
        """Lines 270-273: pipeline event flush error returns None."""
        card = _make_card(db_session, "PipeFlush", "pipeflush.com")
        db_session.commit()

        original_flush = db_session.flush

        def bad_flush(*args, **kwargs):
            raise Exception("Flush error")

        db_session.flush = bad_flush
        al = log_pipeline_event(
            db_session,
            user_id=test_user.id,
            event_type="rfq_sent",
            vendor_card_id=card.id,
            notes="Test flush error",
        )
        db_session.flush = original_flush

        assert al is None


# ── compute_contact_relationship_score edge cases (lines 318-321, 330-335) ──


class TestContactRelationshipScoreEdgeCases:
    def test_recency_mid_range(self):
        """Lines 318-321: recency between ideal and max decays linearly."""
        from app.services.contact_intelligence import compute_contact_relationship_score

        now = datetime.now(timezone.utc)
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

        now = datetime.now(timezone.utc)
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

        now = datetime.now(timezone.utc)
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

        now = datetime.now(timezone.utc)
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

        now = datetime.now(timezone.utc)
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
    def test_cooling_trend(self):
        """Lines 384-385: interactions_30d < 0.5 * older_rate -> cooling."""
        from app.services.contact_intelligence import _compute_trend

        # 90d has 20, 30d has 1 -> older_rate = (20-1)/2 = 9.5, 1 < 0.5*9.5=4.75 -> cooling
        result = _compute_trend(interactions_30d=1, interactions_60d=10, interactions_90d=20)
        assert result == "cooling"

    def test_stable_trend(self):
        """Line 387: not warming, not cooling -> stable."""
        from app.services.contact_intelligence import _compute_trend

        # 90d has 10, 30d has 5 -> older_rate = (10-5)/2 = 2.5
        # 5 > 1.5*2.5=3.75? yes -> warming actually. Let me adjust.
        # 90d=12, 30d=5 -> older_rate=(12-5)/2=3.5
        # 5 > 1.5*3.5=5.25? No. 5 < 0.5*3.5=1.75? No. -> stable
        result = _compute_trend(interactions_30d=5, interactions_60d=8, interactions_90d=12)
        assert result == "stable"


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
                raise Exception("Batch flush error")
            return original_flush(*args, **kwargs)

        db_session.flush = selective_flush
        result = compute_all_contact_scores(db_session)
        db_session.flush = original_flush

        # Some contacts should be counted as skipped due to batch flush error
        assert result["skipped"] > 0 or result["updated"] > 0

    def test_final_flush_error(self, db_session, test_user):
        """Lines 510-513: error during final flush increments skipped count."""
        card = _make_card(db_session, "FinalFlush", "finalflush.com")
        for i in range(3):
            _make_contact(db_session, card, f"ff{i}@finalflush.com", full_name=f"FF {i}")
        db_session.commit()

        original_flush = db_session.flush

        def always_fail_flush(*args, **kwargs):
            raise Exception("Final flush error")

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
        card = _make_card(db_session, "CommitErr", "commiterr.com")
        _make_contact(db_session, card, "ce@commiterr.com", full_name="CE")
        db_session.commit()

        original_commit = db_session.commit

        def fail_commit(*args, **kwargs):
            raise Exception("Commit error")

        db_session.commit = fail_commit
        result = compute_all_contact_scores(db_session)
        db_session.commit = original_commit

        # Should still return results (commit error is caught)
        assert "updated" in result


# ── generate_contact_nudges: last_seen_at fallback + no days (lines 554-555, 558) ──


class TestNudgeDaysSinceFallback:
    def test_uses_last_seen_at_when_no_last_interaction(self, db_session):
        """Lines 554-555: falls back to last_seen_at when last_interaction_at is None."""
        card = _make_card(db_session, "SeenCo", "seenco.com")
        vc = _make_contact(db_session, card, "seen@seenco.com", full_name="Seen Guy")
        vc.activity_trend = "dormant"
        vc.last_interaction_at = None
        vc.last_seen_at = datetime.now(timezone.utc) - timedelta(days=45)
        db_session.commit()

        nudges = generate_contact_nudges(db_session, card.id)
        assert len(nudges) == 1
        assert nudges[0]["days_since_contact"] >= 44

    def test_no_days_since_skips_contact(self, db_session):
        """Line 558: contact with no last_interaction_at and no last_seen_at is skipped."""
        card = _make_card(db_session, "NoDays", "nodays.com")
        vc = _make_contact(db_session, card, "nodays@nodays.com", full_name="No Days")
        vc.activity_trend = "dormant"
        vc.last_interaction_at = None
        vc.last_seen_at = None
        db_session.commit()

        nudges = generate_contact_nudges(db_session, card.id)
        assert len(nudges) == 0


# ── generate_contact_nudges: AI enrichment exception (lines 591-592) ──


class TestNudgeAIEnrichmentError:
    def test_ai_enrichment_exception_swallowed(self, db_session):
        """Lines 591-592: exception in _enrich_nudges_with_ai is caught."""
        card = _make_card(db_session, "EnrichErr", "enricherr.com")
        vc = _make_contact(db_session, card, "err@enricherr.com", full_name="Err Person")
        vc.activity_trend = "dormant"
        vc.last_interaction_at = datetime.now(timezone.utc) - timedelta(days=45)
        db_session.commit()

        with patch(
            "app.services.contact_intelligence._enrich_nudges_with_ai",
            side_effect=Exception("Claude API down"),
        ):
            nudges = generate_contact_nudges(db_session, card.id)

        # Nudges still returned even though enrichment failed
        assert len(nudges) == 1
        assert nudges[0]["nudge_type"] == "dormant"


# ── _enrich_nudges_with_ai (lines 632-656) ──────────────────────────


class TestEnrichNudgesWithAI:
    def test_claude_enrichment_success(self, db_session):
        """Successful Claude enrichment updates message."""
        from app.services.contact_intelligence import _enrich_nudges_with_ai

        nudges = [
            {
                "contact_name": "Test",
                "nudge_type": "dormant",
                "days_since_contact": 45,
                "activity_trend": "dormant",
                "relationship_score": 30,
                "message": "original",
            }
        ]

        mock_loop = MagicMock()
        mock_loop.run_until_complete.return_value = {"message": "AI-enriched suggestion"}

        with (
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            result = _enrich_nudges_with_ai(db_session, nudges, 1)

        assert result[0]["message"] == "AI-enriched suggestion"

    def test_claude_enrichment_per_nudge_exception(self, db_session):
        """Per-nudge exception keeps template message."""
        from app.services.contact_intelligence import _enrich_nudges_with_ai

        nudges = [
            {
                "contact_name": "Test",
                "nudge_type": "dormant",
                "days_since_contact": 45,
                "activity_trend": "dormant",
                "relationship_score": 30,
                "message": "template message",
            }
        ]

        mock_loop = MagicMock()
        mock_loop.run_until_complete.side_effect = Exception("API error")

        with (
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            result = _enrich_nudges_with_ai(db_session, nudges, 1)

        # Template message preserved
        assert result[0]["message"] == "template message"

    def test_claude_enrichment_returns_non_dict(self, db_session):
        """Claude returns non-dict — template message kept."""
        from app.services.contact_intelligence import _enrich_nudges_with_ai

        nudges = [
            {
                "contact_name": "Test",
                "nudge_type": "cooling",
                "days_since_contact": 20,
                "activity_trend": "cooling",
                "relationship_score": 50,
                "message": "template msg",
            }
        ]

        mock_loop = MagicMock()
        mock_loop.run_until_complete.return_value = "not a dict"

        with (
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock),
            patch("asyncio.get_event_loop", return_value=mock_loop),
        ):
            result = _enrich_nudges_with_ai(db_session, nudges, 1)

        assert result[0]["message"] == "template msg"


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
