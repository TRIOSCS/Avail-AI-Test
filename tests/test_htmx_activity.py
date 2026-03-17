"""Tests for HTMX enhanced activity timeline and activity logging (Phase 2C).

Covers activity tab rendering with RFQ contacts, activity logs, and manual logging form.

Called by: pytest
Depends on: conftest (client, db_session, test_user fixtures), app.models
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Requisition, User
from app.models.intelligence import ActivityLog
from app.models.offers import Contact as RfqContact


@pytest.fixture()
def req_with_activity(db_session: Session, test_user: User):
    """Create a requisition with RFQ contacts and activity logs."""
    company = Company(name="Activity Co", created_at=datetime.now(timezone.utc))
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="Activity Test Req",
        status="active",
        created_by=test_user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    # RFQ contacts
    contact1 = RfqContact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Vendor A",
        vendor_name_normalized="vendor a",
        vendor_contact="sales@vendora.com",
        subject="RFQ - LM317T",
        status="sent",
        created_at=datetime.now(timezone.utc),
    )
    contact2 = RfqContact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Vendor B",
        vendor_name_normalized="vendor b",
        vendor_contact="quotes@vendorb.com",
        subject="RFQ - LM317T",
        status="responded",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([contact1, contact2])

    # Activity log
    activity = ActivityLog(
        user_id=test_user.id,
        requisition_id=req.id,
        activity_type="phone_call",
        channel="phone",
        contact_name="John at Vendor A",
        notes="Called about pricing",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(activity)
    db_session.commit()

    return {"req": req, "contacts": [contact1, contact2], "activity": activity}


def test_activity_tab_renders_rfq_contacts(client, req_with_activity):
    """Activity tab shows RFQ contact history."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    assert resp.status_code == 200
    html = resp.text
    assert "Vendor A" in html
    assert "Vendor B" in html
    assert "sales@vendora.com" in html


def test_activity_tab_shows_status_badges(client, req_with_activity):
    """Activity tab shows status badges on RFQ contacts."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    html = resp.text
    assert "Sent" in html
    assert "Responded" in html


def test_activity_tab_renders_activity_logs(client, req_with_activity):
    """Activity tab shows manually logged activities."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    html = resp.text
    assert "Phone Call" in html
    assert "Called about pricing" in html


def test_activity_tab_shows_summary_counts(client, req_with_activity):
    """Activity tab has summary bar with counts."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    html = resp.text
    assert "RFQs Sent" in html
    assert "Responses" in html
    assert "Activities" in html


def test_activity_tab_has_log_form(client, req_with_activity):
    """Activity tab has a log activity form."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    html = resp.text
    assert "Log Activity" in html
    assert f'hx-post="/v2/partials/requisitions/{req.id}/log-activity"' in html
    assert 'name="activity_type"' in html
    assert 'name="notes"' in html


def test_activity_tab_empty_state(client, db_session, test_user):
    """Empty activity tab shows placeholder."""
    company = Company(name="Empty Co", created_at=datetime.now(timezone.utc))
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="Empty Req",
        status="active",
        created_by=test_user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    assert resp.status_code == 200
    assert "No activity recorded" in resp.text


def test_log_activity_creates_record(client, db_session, req_with_activity):
    """POST log-activity creates an ActivityLog and returns refreshed tab."""
    req = req_with_activity["req"]
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/log-activity",
        data={
            "activity_type": "note",
            "vendor_name": "Test Vendor",
            "notes": "Left a voicemail",
        },
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Left a voicemail" in html

    # Verify record was created
    logs = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == req.id,
            ActivityLog.notes == "Left a voicemail",
        )
        .all()
    )
    assert len(logs) == 1
    assert logs[0].activity_type == "note"
    assert logs[0].channel == "note"


def test_log_activity_phone_call(client, db_session, req_with_activity):
    """Logging a phone call sets correct channel."""
    req = req_with_activity["req"]
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/log-activity",
        data={
            "activity_type": "phone_call",
            "vendor_name": "Call Vendor",
            "contact_phone": "+15559876543",
            "notes": "Discussed pricing",
        },
    )
    assert resp.status_code == 200

    log = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.notes == "Discussed pricing",
        )
        .first()
    )
    assert log.channel == "phone"
    assert log.contact_phone == "+15559876543"


def test_log_activity_404_for_missing_req(client):
    """Log activity returns 404 for non-existent requisition."""
    resp = client.post(
        "/v2/partials/requisitions/99999/log-activity",
        data={"activity_type": "note", "notes": "test"},
    )
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  Tests for HTMX activity endpoints (activity.py module)
#  Covers: click-to-call, company/vendor/contact timelines, enrichment
# ═══════════════════════════════════════════════════════════════════════


class TestClickToCallLogging:
    """POST /partials/activity/call — click-to-call logging."""

    def test_call_logging_generic(self, client, db_session, test_user):
        """Logging a call without company or vendor uses generic log_call_activity."""
        resp = client.post(
            "/v2/partials/activity/call",
            data={"phone_number": "+15551234567"},
        )
        assert resp.status_code == 200
        assert "Call logged" in resp.text

        record = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.contact_phone == "+15551234567")
            .first()
        )
        assert record is not None
        assert record.channel == "phone"

    def test_call_logging_with_company(self, client, db_session, test_user, test_company):
        """Logging a call with company_id routes to log_company_call."""
        resp = client.post(
            "/v2/partials/activity/call",
            data={"phone_number": "+15559999999", "company_id": str(test_company.id)},
        )
        assert resp.status_code == 200
        assert "Call logged" in resp.text

        record = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.company_id == test_company.id,
                ActivityLog.contact_phone == "+15559999999",
            )
            .first()
        )
        assert record is not None

    def test_call_logging_with_vendor(self, client, db_session, test_user, test_vendor_card):
        """Logging a call with vendor_card_id routes to log_vendor_call."""
        resp = client.post(
            "/v2/partials/activity/call",
            data={
                "phone_number": "+15558888888",
                "vendor_card_id": str(test_vendor_card.id),
            },
        )
        assert resp.status_code == 200
        assert "Call logged" in resp.text

        record = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.vendor_card_id == test_vendor_card.id,
                ActivityLog.contact_phone == "+15558888888",
            )
            .first()
        )
        assert record is not None

    def test_call_logging_with_origin(self, client, db_session, test_user, test_company):
        """Origin param is captured in the notes field."""
        resp = client.post(
            "/v2/partials/activity/call",
            data={
                "phone_number": "+15557777777",
                "company_id": str(test_company.id),
                "origin": "company-detail",
            },
        )
        assert resp.status_code == 200

        record = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.contact_phone == "+15557777777")
            .first()
        )
        assert record is not None
        assert "company-detail" in (record.notes or "")


class TestCompanyActivityTimeline:
    """GET /partials/companies/{company_id}/tab/activity — company timeline."""

    def test_returns_html_table(self, client, db_session, test_user, test_company):
        """Endpoint returns HTML with activity table."""
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="email_sent",
            channel="email",
            company_id=test_company.id,
            contact_name="Jane Vendor",
            subject="Quote request",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        resp = client.get(f"/v2/partials/companies/{test_company.id}/tab/activity")
        assert resp.status_code == 200
        assert "<table" in resp.text
        assert "Jane Vendor" in resp.text
        assert "Quote request" in resp.text

    def test_empty_state(self, client, db_session, test_user, test_company):
        """Empty company shows no-activity message."""
        resp = client.get(f"/v2/partials/companies/{test_company.id}/tab/activity")
        assert resp.status_code == 200
        assert "No activity recorded" in resp.text

    def test_channel_filter(self, client, db_session, test_user, test_company):
        """Channel query param filters activities."""
        for ch in ("email", "phone"):
            db_session.add(
                ActivityLog(
                    user_id=test_user.id,
                    activity_type=f"test_{ch}",
                    channel=ch,
                    company_id=test_company.id,
                    subject=f"Test {ch}",
                    created_at=datetime.now(timezone.utc),
                )
            )
        db_session.commit()

        resp = client.get(
            f"/v2/partials/companies/{test_company.id}/tab/activity?channel=email"
        )
        assert resp.status_code == 200
        assert "Test email" in resp.text
        assert "Test phone" not in resp.text

    def test_404_for_missing_company(self, client):
        """Non-existent company returns 404."""
        resp = client.get("/v2/partials/companies/99999/tab/activity")
        assert resp.status_code == 404

    def test_pagination(self, client, db_session, test_user, test_company):
        """Limit and offset control returned activities."""
        for i in range(5):
            db_session.add(
                ActivityLog(
                    user_id=test_user.id,
                    activity_type="note",
                    channel="manual",
                    company_id=test_company.id,
                    subject=f"Note {i}",
                    created_at=datetime.now(timezone.utc),
                )
            )
        db_session.commit()

        resp = client.get(
            f"/v2/partials/companies/{test_company.id}/tab/activity?limit=2&offset=0"
        )
        assert resp.status_code == 200
        assert "<table" in resp.text


class TestVendorActivityTimeline:
    """GET /partials/vendors/{vendor_id}/activity — vendor timeline."""

    def test_returns_html_table(self, client, db_session, test_user, test_vendor_card):
        """Endpoint returns HTML with activity table for a vendor."""
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="call_outbound",
            channel="phone",
            vendor_card_id=test_vendor_card.id,
            contact_name="Bob Sales",
            subject="Pricing call",
            summary="Called Bob about pricing",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/activity")
        assert resp.status_code == 200
        assert "<table" in resp.text
        assert "Bob Sales" in resp.text

    def test_empty_vendor_timeline(self, client, db_session, test_user, test_vendor_card):
        """Empty vendor shows no-activity message."""
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/activity")
        assert resp.status_code == 200
        assert "No activity recorded" in resp.text

    def test_404_for_missing_vendor(self, client):
        """Non-existent vendor returns 404."""
        resp = client.get("/v2/partials/vendors/99999/activity")
        assert resp.status_code == 404


class TestContactTimeline:
    """GET /partials/contacts/{contact_id}/timeline — contact timeline."""

    def test_returns_html_list(self, client, db_session, test_user, test_vendor_contact):
        """Contact timeline returns HTML list items."""
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="call_outbound",
            channel="phone",
            vendor_contact_id=test_vendor_contact.id,
            summary="Called John Sales",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        resp = client.get(f"/v2/partials/contacts/{test_vendor_contact.id}/timeline")
        assert resp.status_code == 200
        assert "<ul" in resp.text
        assert "Called John Sales" in resp.text

    def test_empty_contact_timeline(self, client, db_session, test_user):
        """Contact with no activities shows empty message."""
        resp = client.get("/v2/partials/contacts/99999/timeline")
        assert resp.status_code == 200
        assert "No activity recorded" in resp.text


class TestCompanyEnrichment:
    """POST /partials/companies/{company_id}/enrich — company enrichment."""

    def test_enrich_success(self, client, db_session, test_user, test_company, monkeypatch):
        """Successful enrichment returns a result card."""
        mock_result = {
            "entity_type": "company",
            "entity_id": test_company.id,
            "identifier": "acme-electronics.com",
            "sources_fired": 3,
            "sources_used": ["apollo", "clearbit"],
            "applied": {"industry": "Semiconductors", "employee_count": "500"},
            "rejected": {"revenue": {"reason": "low confidence"}},
        }

        async def _mock_enrich(entity_type, entity_id, db):
            return mock_result

        monkeypatch.setattr(
            "app.routers.htmx.activity.enrich_on_demand",
            _mock_enrich,
            raising=False,
        )
        # Also patch the import inside the function
        monkeypatch.setattr(
            "app.services.enrichment_orchestrator.enrich_on_demand",
            _mock_enrich,
        )

        resp = client.post(f"/v2/partials/companies/{test_company.id}/enrich")
        assert resp.status_code == 200
        html = resp.text
        assert "Enrichment complete" in html
        assert "2 of 3 sources returned data" in html
        assert "industry" in html
        assert "Semiconductors" in html
        assert "revenue" in html
        assert "low confidence" in html

    def test_enrich_failure(self, client, db_session, test_user, test_company, monkeypatch):
        """Enrichment failure returns error card."""

        async def _mock_fail(entity_type, entity_id, db):
            raise RuntimeError("Apollo API key not configured")

        monkeypatch.setattr(
            "app.services.enrichment_orchestrator.enrich_on_demand",
            _mock_fail,
        )

        resp = client.post(f"/v2/partials/companies/{test_company.id}/enrich")
        assert resp.status_code == 500
        assert "Enrichment failed" in resp.text
        assert "Apollo API key not configured" in resp.text

    def test_enrich_404_for_missing_company(self, client):
        """Non-existent company returns 404."""
        resp = client.post("/v2/partials/companies/99999/enrich")
        assert resp.status_code == 404


class TestVendorEnrichment:
    """POST /partials/vendors/{vendor_id}/enrich — vendor enrichment."""

    def test_enrich_success(self, client, db_session, test_user, test_vendor_card, monkeypatch):
        """Successful vendor enrichment returns a result card."""
        mock_result = {
            "entity_type": "vendor",
            "entity_id": test_vendor_card.id,
            "identifier": "arrow.com",
            "sources_fired": 2,
            "sources_used": ["apollo"],
            "applied": {"website": "https://arrow.com"},
            "rejected": {},
        }

        async def _mock_enrich(entity_type, entity_id, db):
            return mock_result

        monkeypatch.setattr(
            "app.services.enrichment_orchestrator.enrich_on_demand",
            _mock_enrich,
        )

        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/enrich")
        assert resp.status_code == 200
        html = resp.text
        assert "Enrichment complete" in html
        assert "1 of 2 sources returned data" in html
        assert "website" in html

    def test_enrich_failure(self, client, db_session, test_user, test_vendor_card, monkeypatch):
        """Vendor enrichment failure returns error card."""

        async def _mock_fail(entity_type, entity_id, db):
            raise RuntimeError("Service unavailable")

        monkeypatch.setattr(
            "app.services.enrichment_orchestrator.enrich_on_demand",
            _mock_fail,
        )

        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/enrich")
        assert resp.status_code == 500
        assert "Enrichment failed" in resp.text

    def test_enrich_404_for_missing_vendor(self, client):
        """Non-existent vendor returns 404."""
        resp = client.post("/v2/partials/vendors/99999/enrich")
        assert resp.status_code == 404
