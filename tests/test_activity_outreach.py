"""Tests for POST /api/activity/outreach-initiated (CDM click-to-contact logging).

Covers: all four channels (phone/email/teams/wechat), last_activity_at bumps on
company and site, validation errors, and the log_outreach_initiated service.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import ActivityLog
from app.models.crm import Company, CustomerSite, SiteContact


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    """Clear in-memory rate limiter between tests."""
    from app.routers.activity import _call_log

    _call_log.clear()
    yield
    _call_log.clear()


@pytest.fixture
def cdm_company(db_session):
    """Company + site + contact wired together for outreach tests."""
    company = Company(
        name="Outreach Test Co",
        is_active=True,
        last_activity_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Pat Buyer",
        title="Purchasing Manager",
        email="pat@outreachtest.com",
        phone="+14155551234",
        wechat_id="pat_wechat",
    )
    db_session.add(contact)
    db_session.commit()
    return {"company": company, "site": site, "contact": contact}


class TestOutreachInitiated:
    def _post(self, client, cdm, channel, value, **extra):
        payload = {
            "channel": channel,
            "contact_value": value,
            "company_id": cdm["company"].id,
            "customer_site_id": cdm["site"].id,
            "site_contact_id": cdm["contact"].id,
            "contact_name": cdm["contact"].full_name,
            "origin": "cdm_workspace",
            **extra,
        }
        return client.post("/api/activity/outreach-initiated", json=payload)

    def test_phone_outreach(self, client, db_session, cdm_company):
        resp = self._post(client, cdm_company, "phone", "(415) 555-1234")
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.activity_type == "call_logged"
        assert record.channel == "phone"
        assert record.event_type == "call"
        assert record.direction == "outbound"
        assert record.is_meaningful is True
        assert record.auto_logged is True
        assert record.contact_phone == "+14155551234"
        assert record.company_id == cdm_company["company"].id
        assert record.site_contact_id == cdm_company["contact"].id
        assert "Call to Pat Buyer" in record.subject

    def test_email_outreach(self, client, db_session, cdm_company):
        resp = self._post(client, cdm_company, "email", "pat@outreachtest.com")
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.activity_type == "email_sent"
        assert record.channel == "email"
        assert record.event_type == "email"
        assert record.contact_email == "pat@outreachtest.com"
        assert "Email to Pat Buyer" in record.subject

    def test_teams_outreach(self, client, db_session, cdm_company):
        resp = self._post(client, cdm_company, "teams", "pat@outreachtest.com")
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.activity_type == "teams_message"
        assert record.channel == "teams"
        assert record.event_type == "message"
        assert record.contact_email == "pat@outreachtest.com"

    def test_wechat_outreach(self, client, db_session, cdm_company):
        resp = self._post(client, cdm_company, "wechat", "pat_wechat")
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.activity_type == "wechat_message"
        assert record.channel == "wechat"
        assert record.event_type == "message"
        # WeChat handle goes to notes — contact_name keeps the person's name
        assert record.contact_name == "Pat Buyer"
        assert "pat_wechat" in record.notes

    def test_bumps_company_and_site_last_activity(self, client, db_session, cdm_company):
        before = datetime.now(timezone.utc) - timedelta(minutes=1)
        resp = self._post(client, cdm_company, "phone", "+14155551234")
        assert resp.status_code == 201

        db_session.expire_all()
        company = db_session.get(Company, cdm_company["company"].id)
        site = db_session.get(CustomerSite, cdm_company["site"].id)
        assert company.last_activity_at is not None
        assert company.last_activity_at.replace(tzinfo=timezone.utc) > before
        assert site.last_activity_at is not None
        assert site.last_activity_at.replace(tzinfo=timezone.utc) > before

    def test_invalid_phone_returns_400(self, client, cdm_company):
        resp = self._post(client, cdm_company, "phone", "call pat")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_invalid_email_returns_400(self, client, cdm_company):
        resp = self._post(client, cdm_company, "email", "not-an-email")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_invalid_channel_returns_422(self, client, cdm_company):
        resp = self._post(client, cdm_company, "fax", "+14155551234")
        assert resp.status_code == 422

    def test_minimal_payload_without_entities(self, client, db_session):
        resp = client.post(
            "/api/activity/outreach-initiated",
            json={"channel": "phone", "contact_value": "4155551234"},
        )
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.company_id is None
        assert "Call to +14155551234" in record.subject

    def test_rate_limit_returns_429(self, client, cdm_company):
        from app.routers import activity as activity_router

        user_buckets = activity_router._call_log
        # Outreach has its own (higher) budget, separate from call-initiated
        for _ in range(activity_router._OUTREACH_RATE_LIMIT):
            resp = self._post(client, cdm_company, "phone", "+14155551234")
            assert resp.status_code == 201
        resp = self._post(client, cdm_company, "phone", "+14155551234")
        assert resp.status_code == 429
        assert user_buckets  # sanity: limiter actually tracked the user

    def test_rate_limit_bucket_separate_from_call_initiated(self, client, cdm_company):
        """Exhausting the click-to-call budget must not block outreach logging."""
        from app.routers import activity as activity_router

        for _ in range(activity_router._RATE_LIMIT):
            resp = client.post("/api/activity/call-initiated", json={"phone_number": "4155551234"})
            assert resp.status_code == 201
        resp = client.post("/api/activity/call-initiated", json={"phone_number": "4155551234"})
        assert resp.status_code == 429

        # Outreach still works — separate bucket
        resp = self._post(client, cdm_company, "email", "pat@outreachtest.com")
        assert resp.status_code == 201

    def test_double_click_dedup_returns_same_record(self, client, db_session, cdm_company):
        """Re-clicking the same contact link within the window must not duplicate."""
        from app.models import ActivityLog as AL

        first = self._post(client, cdm_company, "phone", "+14155551234")
        second = self._post(client, cdm_company, "phone", "+14155551234")
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        count = (
            db_session.query(AL)
            .filter(AL.activity_type == "call_logged", AL.company_id == cdm_company["company"].id)
            .count()
        )
        assert count == 1

    def test_dedup_stops_after_window_expires(self, client, db_session, cdm_company):
        """The same click AFTER the dedup window is a NEW touch, not a dup.

        Guards the dedup-miss direction: an over-widened window (unit typo,
        timezone bug, inverted comparison) would silently drop every repeat
        touch forever while the dedup-hit tests stay green.
        """
        from app.services.activity_service import OUTREACH_DEDUP_SECONDS

        first = self._post(client, cdm_company, "phone", "+14155551234")
        assert first.status_code == 201
        record = db_session.get(ActivityLog, first.json()["id"])
        record.created_at = datetime.now(timezone.utc) - timedelta(seconds=OUTREACH_DEDUP_SECONDS + 60)
        db_session.commit()

        second = self._post(client, cdm_company, "phone", "+14155551234")
        assert second.status_code == 201
        assert second.json()["id"] != first.json()["id"]
        count = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.activity_type == "call_logged", ActivityLog.company_id == cdm_company["company"].id)
            .count()
        )
        assert count == 2

    def test_dedup_does_not_collapse_distinct_contacts(self, client, db_session, cdm_company):
        """Back-to-back calls to two DIFFERENT contacts at one company are two touches.

        Worst case exercised: same display name AND same phone (shared
        switchboard) — only site_contact_id distinguishes them, so this pins
        the entity ids (not the display subject) as the dedup identity.
        """
        twin = SiteContact(
            customer_site_id=cdm_company["site"].id,
            full_name="Pat Buyer",
            phone="+14155551234",
        )
        db_session.add(twin)
        db_session.commit()

        first = self._post(client, cdm_company, "phone", "+14155551234")
        second = self._post(client, cdm_company, "phone", "+14155551234", site_contact_id=twin.id)
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] != second.json()["id"]

    def test_nonexistent_entity_ids_dropped_not_500(self, client, db_session):
        """Stale DOM ids (deleted company/site/contact) must not FK-crash the log."""
        resp = client.post(
            "/api/activity/outreach-initiated",
            json={
                "channel": "phone",
                "contact_value": "4155551234",
                "company_id": 999999,
                "customer_site_id": 999999,
                "site_contact_id": 999999,
            },
        )
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.company_id is None
        assert record.customer_site_id is None
        assert record.site_contact_id is None
        # The degradation is surfaced to the client, not silently swallowed.
        assert sorted(resp.json()["dropped_links"]) == ["company", "contact", "site"]

    def test_valid_entity_ids_report_no_dropped_links(self, client, cdm_company):
        """Fully-linked logs report an empty dropped_links (frontend shows success)."""
        resp = self._post(client, cdm_company, "phone", "+14155551234")
        assert resp.status_code == 201
        assert resp.json()["dropped_links"] == []

    def test_site_from_other_company_link_dropped(self, client, db_session, cdm_company):
        """A site that doesn't belong to the company must not be linked or bumped."""
        other_co = Company(name="Unrelated Co", is_active=True)
        db_session.add(other_co)
        db_session.flush()
        other_site = CustomerSite(company_id=other_co.id, site_name="Elsewhere", is_active=True)
        db_session.add(other_site)
        db_session.commit()

        resp = self._post(
            client, cdm_company, "phone", "+14155551234", customer_site_id=other_site.id, site_contact_id=None
        )
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.company_id == cdm_company["company"].id
        assert record.customer_site_id is None
        assert "site" in resp.json()["dropped_links"]
        # The unrelated site's staleness data must stay untouched.
        db_session.expire_all()
        assert db_session.get(CustomerSite, other_site.id).last_activity_at is None

    def test_contact_from_other_site_link_dropped(self, client, db_session, cdm_company):
        """A contact that doesn't belong to the claimed site must not be linked."""
        other_site = CustomerSite(company_id=cdm_company["company"].id, site_name="Plant 2", is_active=True)
        db_session.add(other_site)
        db_session.flush()
        other_contact = SiteContact(customer_site_id=other_site.id, full_name="Sam Elsewhere")
        db_session.add(other_contact)
        db_session.commit()

        # Payload claims the HQ site but the contact lives at Plant 2.
        resp = self._post(client, cdm_company, "phone", "+14155551234", site_contact_id=other_contact.id)
        assert resp.status_code == 201
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.site_contact_id is None
        assert record.customer_site_id == cdm_company["site"].id
        assert "contact" in resp.json()["dropped_links"]

    def test_wechat_whitespace_value_returns_400(self, client, cdm_company):
        """An all-whitespace WeChat handle must 400, not log 'WeChat message to '."""
        resp = self._post(client, cdm_company, "wechat", "   ")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_oversized_contact_value_returns_422(self, client, cdm_company):
        """contact_value beyond the String(255) snapshot columns is a 422 at the
        boundary (Postgres would DataError-500; SQLite tests mask that)."""
        resp = self._post(client, cdm_company, "email", "x" * 290 + "@example.com")
        assert resp.status_code == 422

    def test_oversized_phone_returns_400(self, client, cdm_company):
        """A digit string beyond the E.164 15-digit cap is rejected as a phone."""
        resp = self._post(client, cdm_company, "phone", "1" * 60)
        assert resp.status_code == 400

    def test_call_initiated_bumps_last_activity(self, client, db_session, cdm_company):
        """Click-to-call (legacy endpoint) also feeds the staleness sort now."""
        before = datetime.now(timezone.utc) - timedelta(minutes=1)
        resp = client.post(
            "/api/activity/call-initiated",
            json={
                "phone_number": "4155551234",
                "company_id": cdm_company["company"].id,
                "customer_site_id": cdm_company["site"].id,
            },
        )
        assert resp.status_code == 201
        db_session.expire_all()
        company = db_session.get(Company, cdm_company["company"].id)
        assert company.last_activity_at is not None
        assert company.last_activity_at.replace(tzinfo=timezone.utc) > before


class TestLogOutreachService:
    def test_unknown_channel_raises(self, db_session, test_user):
        from app.services.activity_service import log_outreach_initiated

        with pytest.raises(ValueError):
            log_outreach_initiated(
                db_session,
                user_id=test_user.id,
                channel="carrier_pigeon",
                contact_value="coop 7",
            )

    def test_subject_falls_back_to_value(self, db_session, test_user):
        from app.services.activity_service import log_outreach_initiated

        record = log_outreach_initiated(
            db_session,
            user_id=test_user.id,
            channel="email",
            contact_value="someone@example.com",
        )
        assert record.subject == "Email to someone@example.com"
