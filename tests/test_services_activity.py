"""
test_services_activity.py — Tests for activity_service.

Tests email/phone matching, activity logging, dedup, and query helpers.
Uses in-memory SQLite via conftest fixtures.

Called by: pytest
Depends on: app/services/activity_service.py, conftest.py
"""

from datetime import datetime, timedelta, timezone

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    SiteContact,
    VendorCard,
    VendorContact,
)
from app.services.activity_service import (
    days_since_last_activity,
    get_company_activities,
    get_site_contact_notes,
    get_user_activities,
    get_vendor_activities,
    log_call_activity,
    log_email_activity,
    log_site_contact_note,
    match_email_to_entity,
    match_phone_to_entity,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_company(db, name="Acme Electronics", domain="acme.com"):
    co = Company(
        name=name,
        domain=domain,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.flush()
    return co


def _make_site(db, company_id, email="john@acme.com", phone="+15551234567"):
    site = CustomerSite(
        company_id=company_id,
        site_name="HQ",
        is_active=True,
        contact_email=email,
        contact_phone=phone,
        created_at=datetime.now(timezone.utc),
    )
    db.add(site)
    db.flush()
    return site


def _make_vendor_card(db, name="Arrow Electronics", domain="arrow.com"):
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        domain=domain,
        is_blacklisted=False,
        sighting_count=10,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _make_vendor_contact(db, vendor_card_id, email="sales@arrow.com", phone="+15559876543"):
    vc = VendorContact(
        vendor_card_id=vendor_card_id,
        email=email,
        phone=phone,
        full_name="Sales Rep",
        source="manual",
    )
    db.add(vc)
    db.flush()
    return vc


# ── Email matching ──────────────────────────────────────────────────


class TestMatchEmailToEntity:
    def test_match_customer_site_exact(self, db_session, test_user):
        co = _make_company(db_session)
        _make_site(db_session, co.id, email="john@acme.com")
        db_session.commit()

        result = match_email_to_entity("john@acme.com", db_session)
        assert result is not None
        assert result["type"] == "company"
        assert result["id"] == co.id

    def test_match_vendor_contact(self, db_session):
        card = _make_vendor_card(db_session)
        _make_vendor_contact(db_session, card.id, email="vendor@arrow.com")
        db_session.commit()

        result = match_email_to_entity("vendor@arrow.com", db_session)
        assert result is not None
        assert result["type"] == "vendor"
        assert result["id"] == card.id

    def test_match_company_by_domain(self, db_session):
        co = _make_company(db_session, domain="specialcorp.com")
        db_session.commit()

        result = match_email_to_entity("anyone@specialcorp.com", db_session)
        assert result is not None
        assert result["type"] == "company"
        assert result["id"] == co.id

    def test_match_vendor_by_domain(self, db_session):
        _make_vendor_card(db_session, domain="vendorco.com")
        db_session.commit()

        result = match_email_to_entity("someone@vendorco.com", db_session)
        assert result is not None
        assert result["type"] == "vendor"

    def test_generic_domain_skipped(self, db_session):
        """gmail.com, yahoo.com, etc. should not match."""
        result = match_email_to_entity("someone@gmail.com", db_session)
        assert result is None

    def test_empty_email(self, db_session):
        assert match_email_to_entity("", db_session) is None
        assert match_email_to_entity(None, db_session) is None

    def test_case_insensitive(self, db_session, test_user):
        co = _make_company(db_session)
        _make_site(db_session, co.id, email="John@Acme.COM")
        db_session.commit()

        result = match_email_to_entity("john@acme.com", db_session)
        assert result is not None


# ── Phone matching ──────────────────────────────────────────────────


class TestMatchPhoneToEntity:
    def test_match_customer_site_phone(self, db_session, test_user):
        co = _make_company(db_session)
        _make_site(db_session, co.id, phone="+1-555-123-4567")
        db_session.commit()

        result = match_phone_to_entity("5551234567", db_session)
        assert result is not None
        assert result["type"] == "company"

    def test_match_vendor_contact_phone(self, db_session):
        card = _make_vendor_card(db_session)
        _make_vendor_contact(db_session, card.id, phone="+1-555-987-6543")
        db_session.commit()

        result = match_phone_to_entity("15559876543", db_session)
        assert result is not None
        assert result["type"] == "vendor"

    def test_short_phone_rejected(self, db_session):
        assert match_phone_to_entity("12345", db_session) is None

    def test_empty_phone(self, db_session):
        assert match_phone_to_entity("", db_session) is None
        assert match_phone_to_entity(None, db_session) is None


# ── Email activity logging ──────────────────────────────────────────


class TestLogEmailActivity:
    def test_logs_email_with_company_match(self, db_session, test_user):
        co = _make_company(db_session)
        _make_site(db_session, co.id, email="contact@acme.com")
        db_session.commit()

        record = log_email_activity(
            user_id=test_user.id,
            direction="sent",
            email_addr="contact@acme.com",
            subject="RFQ LM317T",
            external_id="msg-001",
            contact_name="John",
            db=db_session,
        )
        assert record is not None
        assert record.activity_type == "email_sent"
        assert record.company_id == co.id
        assert record.external_id == "msg-001"

    def test_dedup_by_external_id(self, db_session, test_user):
        co = _make_company(db_session)
        _make_site(db_session, co.id, email="contact@acme.com")
        db_session.commit()

        # First log succeeds
        r1 = log_email_activity(
            test_user.id,
            "sent",
            "contact@acme.com",
            "Sub",
            "dup-id",
            "J",
            db_session,
        )
        db_session.commit()
        assert r1 is not None

        # Second log with same external_id is deduped
        r2 = log_email_activity(
            test_user.id,
            "sent",
            "contact@acme.com",
            "Sub",
            "dup-id",
            "J",
            db_session,
        )
        assert r2 is None

    def test_received_direction(self, db_session, test_user):
        co = _make_company(db_session)
        _make_site(db_session, co.id, email="vendor@acme.com")
        db_session.commit()

        record = log_email_activity(
            test_user.id,
            "received",
            "vendor@acme.com",
            "RE: RFQ",
            "msg-002",
            "V",
            db_session,
        )
        assert record is not None
        assert record.activity_type == "email_received"

    def test_no_match_logs_unmatched(self, db_session, test_user):
        """Unmatched emails are still logged (for admin review queue)."""
        record = log_email_activity(
            test_user.id,
            "sent",
            "nobody@unknown.com",
            "Hi",
            None,
            "X",
            db_session,
        )
        assert record is not None
        assert record.company_id is None
        assert record.vendor_card_id is None


# ── Call activity logging ───────────────────────────────────────────


class TestLogCallActivity:
    def test_logs_outbound_call(self, db_session, test_user):
        co = _make_company(db_session)
        _make_site(db_session, co.id, phone="+15551112222")
        db_session.commit()

        record = log_call_activity(
            test_user.id,
            "outbound",
            "5551112222",
            300,
            "call-001",
            "Jane",
            db_session,
        )
        assert record is not None
        assert record.activity_type == "call_outbound"
        assert record.duration_seconds == 300

    def test_dedup_call(self, db_session, test_user):
        co = _make_company(db_session)
        _make_site(db_session, co.id, phone="+15551112222")
        db_session.commit()

        r1 = log_call_activity(
            test_user.id,
            "outbound",
            "5551112222",
            60,
            "call-dup",
            "J",
            db_session,
        )
        db_session.commit()
        r2 = log_call_activity(
            test_user.id,
            "outbound",
            "5551112222",
            60,
            "call-dup",
            "J",
            db_session,
        )
        assert r1 is not None
        assert r2 is None


# ── Query helpers ───────────────────────────────────────────────────


class TestQueryHelpers:
    def _log_activity(self, db, user_id, company_id, vendor_card_id=None):
        a = ActivityLog(
            user_id=user_id,
            activity_type="email_sent",
            channel="email",
            company_id=company_id,
            vendor_card_id=vendor_card_id,
            contact_email="test@test.com",
            created_at=datetime.now(timezone.utc),
        )
        db.add(a)
        db.flush()
        return a

    def test_get_company_activities(self, db_session, test_user):
        co = _make_company(db_session)
        db_session.commit()
        self._log_activity(db_session, test_user.id, co.id)
        self._log_activity(db_session, test_user.id, co.id)
        db_session.commit()

        activities = get_company_activities(co.id, db_session)
        assert len(activities) == 2

    def test_get_vendor_activities(self, db_session, test_user):
        card = _make_vendor_card(db_session)
        db_session.commit()
        self._log_activity(db_session, test_user.id, None, card.id)
        db_session.commit()

        activities = get_vendor_activities(card.id, db_session)
        assert len(activities) == 1

    def test_get_user_activities(self, db_session, test_user):
        co = _make_company(db_session)
        db_session.commit()
        self._log_activity(db_session, test_user.id, co.id)
        db_session.commit()

        activities = get_user_activities(test_user.id, db_session)
        assert len(activities) == 1

    def test_days_since_last_activity(self, db_session, test_user):
        co = _make_company(db_session)
        db_session.commit()
        a = ActivityLog(
            user_id=test_user.id,
            activity_type="email_sent",
            channel="email",
            company_id=co.id,
            contact_email="x@x.com",
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        db_session.add(a)
        db_session.commit()

        days = days_since_last_activity(co.id, db_session)
        assert days is not None
        assert 4 <= days <= 6  # allow small drift

    def test_days_since_no_activity(self, db_session):
        co = _make_company(db_session)
        db_session.commit()
        assert days_since_last_activity(co.id, db_session) is None

    def test_limit_parameter(self, db_session, test_user):
        co = _make_company(db_session)
        db_session.commit()
        for _ in range(10):
            self._log_activity(db_session, test_user.id, co.id)
        db_session.commit()

        activities = get_company_activities(co.id, db_session, limit=3)
        assert len(activities) == 3


# ═══════════════════════════════════════════════════════════════════════
#  days_since_last_vendor_activity
# ═══════════════════════════════════════════════════════════════════════


class TestDaysSinceLastVendorActivity:
    def test_with_recent_activity(self, db_session, test_user, test_vendor_card):
        from app.services.activity_service import days_since_last_vendor_activity

        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="email_sent",
            channel="email",
            vendor_card_id=test_vendor_card.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        db_session.add(activity)
        db_session.commit()

        days = days_since_last_vendor_activity(test_vendor_card.id, db_session)
        assert days is not None
        assert 2 <= days <= 4

    def test_no_activity(self, db_session, test_vendor_card):
        from app.services.activity_service import days_since_last_vendor_activity

        days = days_since_last_vendor_activity(test_vendor_card.id, db_session)
        assert days is None


# ═══════════════════════════════════════════════════════════════════════
#  _update_vendor_contact_stats and _increment_vendor_contact
# ═══════════════════════════════════════════════════════════════════════


class TestVendorContactStats:
    def test_update_vendor_contact_stats_with_contact(self, db_session, test_vendor_contact):
        from app.services.activity_service import _update_vendor_contact_stats

        initial_count = test_vendor_contact.interaction_count or 0
        match = {
            "type": "vendor",
            "id": test_vendor_contact.vendor_card_id,
            "vendor_contact_id": test_vendor_contact.id,
        }
        _update_vendor_contact_stats(match, db_session)
        db_session.flush()
        db_session.refresh(test_vendor_contact)
        assert test_vendor_contact.interaction_count == initial_count + 1
        assert test_vendor_contact.last_interaction_at is not None

    def test_update_vendor_contact_stats_no_contact_id(self, db_session):
        from app.services.activity_service import _update_vendor_contact_stats

        # No vendor_contact_id in match -> should not raise
        match = {"type": "vendor", "id": 1}
        _update_vendor_contact_stats(match, db_session)

    def test_increment_vendor_contact(self, db_session, test_vendor_contact):
        from app.services.activity_service import _increment_vendor_contact

        initial = test_vendor_contact.interaction_count or 0
        _increment_vendor_contact(test_vendor_contact.id, db_session)
        db_session.flush()
        db_session.refresh(test_vendor_contact)
        assert test_vendor_contact.interaction_count == initial + 1


# ═══════════════════════════════════════════════════════════════════════
#  Vendor-specific manual logging
# ═══════════════════════════════════════════════════════════════════════


class TestVendorManualLogging:
    def test_log_vendor_call(self, db_session, test_user, test_vendor_card, test_vendor_contact):
        from app.services.activity_service import log_vendor_call

        record = log_vendor_call(
            user_id=test_user.id,
            vendor_card_id=test_vendor_card.id,
            vendor_contact_id=test_vendor_contact.id,
            direction="outbound",
            phone="+1-555-0100",
            duration_seconds=120,
            contact_name="John Sales",
            notes="Discussed pricing",
            db=db_session,
        )
        assert record is not None
        assert record.activity_type == "call_outbound"
        assert record.vendor_card_id == test_vendor_card.id

    def test_log_vendor_note(self, db_session, test_user, test_vendor_card, test_vendor_contact):
        from app.services.activity_service import log_vendor_note

        record = log_vendor_note(
            user_id=test_user.id,
            vendor_card_id=test_vendor_card.id,
            vendor_contact_id=test_vendor_contact.id,
            notes="Meeting scheduled",
            contact_name="John Sales",
            db=db_session,
        )
        assert record is not None
        assert record.activity_type == "note"

    def test_log_vendor_call_no_contact(self, db_session, test_user, test_vendor_card):
        from app.services.activity_service import log_vendor_call

        record = log_vendor_call(
            user_id=test_user.id,
            vendor_card_id=test_vendor_card.id,
            vendor_contact_id=None,
            direction="inbound",
            phone="+1-555-0100",
            duration_seconds=60,
            contact_name="Unknown",
            notes=None,
            db=db_session,
        )
        assert record is not None

    def test_log_vendor_note_no_contact(self, db_session, test_user, test_vendor_card):
        from app.services.activity_service import log_vendor_note

        record = log_vendor_note(
            user_id=test_user.id,
            vendor_card_id=test_vendor_card.id,
            vendor_contact_id=None,
            notes="General note",
            contact_name=None,
            db=db_session,
        )
        assert record is not None


# ═══════════════════════════════════════════════════════════════════════
#  Site contact note logging
# ═══════════════════════════════════════════════════════════════════════


class TestSiteContactNoteLogging:
    def test_log_site_contact_note(self, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        contact = SiteContact(customer_site_id=site.id, full_name="Note Test Contact")
        db_session.add(contact)
        db_session.commit()

        record = log_site_contact_note(
            user_id=test_user.id,
            site_contact_id=contact.id,
            customer_site_id=site.id,
            company_id=co.id,
            notes="Discussed pricing",
            db=db_session,
        )
        assert record is not None
        assert record.activity_type == "note"
        assert record.channel == "manual"
        assert record.site_contact_id == contact.id
        assert record.company_id == co.id
        assert record.customer_site_id == site.id
        assert record.notes == "Discussed pricing"
        assert record.contact_name == "Note Test Contact"

    def test_get_site_contact_notes_only_for_contact(self, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        c1 = SiteContact(customer_site_id=site.id, full_name="Contact A")
        c2 = SiteContact(customer_site_id=site.id, full_name="Contact B")
        db_session.add_all([c1, c2])
        db_session.commit()

        log_site_contact_note(test_user.id, c1.id, site.id, co.id, "Note for A", db_session)
        log_site_contact_note(test_user.id, c2.id, site.id, co.id, "Note for B", db_session)
        db_session.commit()

        notes_a = get_site_contact_notes(c1.id, db_session)
        assert len(notes_a) == 1
        assert notes_a[0].notes == "Note for A"

        notes_b = get_site_contact_notes(c2.id, db_session)
        assert len(notes_b) == 1
        assert notes_b[0].notes == "Note for B"

    def test_get_site_contact_notes_ordered(self, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        contact = SiteContact(customer_site_id=site.id, full_name="Order Test")
        db_session.add(contact)
        db_session.commit()

        log_site_contact_note(test_user.id, contact.id, site.id, co.id, "First", db_session)
        log_site_contact_note(test_user.id, contact.id, site.id, co.id, "Second", db_session)
        db_session.commit()

        notes = get_site_contact_notes(contact.id, db_session)
        assert len(notes) == 2
        # Most recent first
        assert notes[0].notes == "Second"
        assert notes[1].notes == "First"

    def test_get_site_contact_notes_empty(self, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        contact = SiteContact(customer_site_id=site.id, full_name="Empty Notes")
        db_session.add(contact)
        db_session.commit()

        notes = get_site_contact_notes(contact.id, db_session)
        assert notes == []
