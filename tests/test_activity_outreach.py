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
        # Fill the rate limit window for every user id the override may use
        for _ in range(activity_router._RATE_LIMIT):
            resp = self._post(client, cdm_company, "phone", "+14155551234")
            assert resp.status_code == 201
        resp = self._post(client, cdm_company, "phone", "+14155551234")
        assert resp.status_code == 429
        assert user_buckets  # sanity: limiter actually tracked the user


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
