"""Tests for the do-not-contact (DNC) flag on SiteContact.

Covers:
- Toggle DNC on/off via POST /v2/partials/customers/{company_id}/contacts/{contact_id}/do-not-contact
- Outreach endpoint refuses to log to DNC contacts (no ActivityLog created, 4xx)
- Non-DNC contacts still work via outreach endpoint
- send_batch_rfq skips DNC contacts and reports them as "skipped" (status="skipped", error includes "do-not-contact")
- Non-DNC contacts in the same batch are still sent
- Contact card renders DNC badge when do_not_contact=True

TDD: tests were written first; implementation follows.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ActivityLog
from app.models.crm import Company, CustomerSite, SiteContact

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def dnc_company(db_session: Session, test_user):
    """Company (owned by test_user) + site + two contacts: one DNC, one not."""
    company = Company(name="DNC Test Co", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.flush()

    contact_ok = SiteContact(
        customer_site_id=site.id,
        full_name="Safe Contact",
        email="safe@dnctest.com",
        phone="+14155550001",
    )
    contact_dnc = SiteContact(
        customer_site_id=site.id,
        full_name="DNC Person",
        email="dnc@dnctest.com",
        phone="+14155550002",
    )
    db_session.add(contact_ok)
    db_session.add(contact_dnc)
    db_session.commit()
    db_session.refresh(contact_ok)
    db_session.refresh(contact_dnc)

    return {
        "company": company,
        "site": site,
        "contact_ok": contact_ok,
        "contact_dnc": contact_dnc,
    }


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    """Clear in-memory rate limiter between tests."""
    from app.routers.activity import _call_log

    _call_log.clear()
    yield
    _call_log.clear()


# ── Toggle DNC ────────────────────────────────────────────────────────


class TestDNCToggle:
    def _toggle_url(self, company_id: int, contact_id: int) -> str:
        return f"/v2/partials/customers/{company_id}/contacts/{contact_id}/do-not-contact"

    def test_set_dnc_persists_true(self, client: TestClient, db_session: Session, dnc_company):
        """POST with dnc=1 sets do_not_contact=True."""
        contact = dnc_company["contact_ok"]
        company = dnc_company["company"]
        assert contact.do_not_contact is False

        resp = client.post(
            self._toggle_url(company.id, contact.id),
            data={"do_not_contact": "1"},
        )
        assert resp.status_code == 200

        db_session.expire(contact)
        db_session.refresh(contact)
        assert contact.do_not_contact is True

    def test_clear_dnc_persists_false(self, client: TestClient, db_session: Session, dnc_company):
        """POST with do_not_contact='' clears the flag back to False."""
        contact = dnc_company["contact_dnc"]
        company = dnc_company["company"]

        # Seed as DNC
        contact.do_not_contact = True
        db_session.commit()

        resp = client.post(
            self._toggle_url(company.id, contact.id),
            data={"do_not_contact": ""},
        )
        assert resp.status_code == 200

        db_session.expire(contact)
        db_session.refresh(contact)
        assert contact.do_not_contact is False

    def test_toggle_unknown_contact_returns_404(self, client: TestClient, dnc_company):
        """Contact that does not exist → 404."""
        company = dnc_company["company"]
        resp = client.post(
            self._toggle_url(company.id, 999999),
            data={"do_not_contact": "1"},
        )
        assert resp.status_code == 404

    def test_toggle_response_is_html(self, client: TestClient, dnc_company):
        """Response is an HTML partial (the re-rendered card fragment)."""
        contact = dnc_company["contact_ok"]
        company = dnc_company["company"]
        resp = client.post(
            self._toggle_url(company.id, contact.id),
            data={"do_not_contact": "1"},
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_dnc_badge_visible_in_response_when_set(self, client: TestClient, dnc_company):
        """When DNC is active, the response HTML contains the DNC badge."""
        contact = dnc_company["contact_ok"]
        company = dnc_company["company"]
        resp = client.post(
            self._toggle_url(company.id, contact.id),
            data={"do_not_contact": "1"},
        )
        assert resp.status_code == 200
        # The DNC badge must be rendered in the partial
        assert "do-not-contact" in resp.text.lower() or "Do Not Contact" in resp.text


# ── Contact card DNC badge ────────────────────────────────────────────


class TestContactCardDNCBadge:
    def test_contact_card_shows_dnc_badge_when_flag_set(self, client: TestClient, db_session: Session, dnc_company):
        """Contact card rendered via the contacts tab shows DNC badge when flag is
        True."""
        contact = dnc_company["contact_dnc"]
        company = dnc_company["company"]

        contact.do_not_contact = True
        db_session.commit()

        resp = client.get(
            f"/v2/partials/customers/{company.id}",
            params={"tab": "contacts"},
        )
        assert resp.status_code == 200
        assert "Do Not Contact" in resp.text or "do-not-contact" in resp.text.lower()

    def test_contact_card_no_dnc_badge_when_flag_clear(self, client: TestClient, db_session: Session, dnc_company):
        """Contact card does NOT show the red DNC badge when flag is False.

        (The DNC toggle button with 'Mark as Do Not Contact' title is always present;
        this test checks that the red active-badge is absent.)
        """
        contact = dnc_company["contact_ok"]
        company = dnc_company["company"]

        assert contact.do_not_contact is False
        # Ensure contact_dnc is also NOT set so no DNC badge appears at all
        dnc_company["contact_dnc"].do_not_contact = False
        db_session.commit()

        resp = client.get(
            f"/v2/partials/customers/{company.id}",
            params={"tab": "contacts"},
        )
        assert resp.status_code == 200
        # The red active badge text only renders when do_not_contact=True.
        # The widget renders as a plain "DNC" button (set-mode) when clear.
        # bg-red-100 is the badge class; its absence confirms no active badge.
        assert "bg-red-100" not in resp.text


# ── Server-side outreach enforcement ─────────────────────────────────


class TestOutreachDNCEnforcement:
    def _post_outreach(self, client: TestClient, dnc_company, contact, channel="email", value=None):
        if value is None:
            value = contact.email or contact.phone
        return client.post(
            "/api/activity/outreach-initiated",
            json={
                "channel": channel,
                "contact_value": value,
                "company_id": dnc_company["company"].id,
                "customer_site_id": dnc_company["site"].id,
                "site_contact_id": contact.id,
                "contact_name": contact.full_name,
                "origin": "cdm_workspace",
            },
        )

    def test_outreach_refused_for_dnc_contact(self, client: TestClient, db_session: Session, dnc_company):
        """Outreach endpoint returns 4xx for a DNC contact; no ActivityLog created."""
        contact = dnc_company["contact_dnc"]
        contact.do_not_contact = True
        db_session.commit()

        before_count = db_session.query(ActivityLog).count()
        resp = self._post_outreach(client, dnc_company, contact)

        # Must refuse — 4xx (422 or 403)
        assert resp.status_code in (403, 422, 400)
        after_count = db_session.query(ActivityLog).count()
        assert after_count == before_count, "No ActivityLog should be created for a DNC contact"

    def test_outreach_allowed_for_non_dnc_contact(self, client: TestClient, db_session: Session, dnc_company):
        """Outreach endpoint succeeds for a non-DNC contact; ActivityLog created."""
        contact = dnc_company["contact_ok"]
        assert contact.do_not_contact is False

        resp = self._post_outreach(client, dnc_company, contact)
        assert resp.status_code == 201
        assert "id" in resp.json()

        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record is not None
        assert record.site_contact_id == contact.id

    def test_outreach_refused_error_message_mentions_dnc(self, client: TestClient, db_session: Session, dnc_company):
        """Error response must mention DNC / do-not-contact so callers know why."""
        contact = dnc_company["contact_dnc"]
        contact.do_not_contact = True
        db_session.commit()

        resp = self._post_outreach(client, dnc_company, contact)
        assert resp.status_code in (403, 422, 400)
        body = resp.text.lower()
        assert "do not contact" in body or "do-not-contact" in body or "dnc" in body


# ── send_batch_rfq DNC enforcement ───────────────────────────────────


class TestSendBatchRfqDNC:
    """send_batch_rfq must skip DNC contacts and report them with status='skipped'."""

    def _make_vendor_group(self, name: str, email: str, parts: list | None = None):
        return {
            "vendor_name": name,
            "vendor_email": email,
            "parts": parts or ["LM317T"],
            "subject": f"RFQ for parts from {name}",
            "body": "Please quote.",
        }

    @pytest.mark.asyncio
    async def test_dnc_contact_skipped_not_emailed(self, db_session: Session, test_user, test_requisition, dnc_company):
        """A vendor group whose email matches a DNC SiteContact must be skipped."""
        from app.email_service import send_batch_rfq

        contact = dnc_company["contact_dnc"]
        contact.do_not_contact = True
        db_session.commit()

        vendor_groups = [
            self._make_vendor_group("DNC Vendor", contact.email),
        ]

        mock_gc = AsyncMock()
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 1
        assert results[0]["status"] == "skipped"
        assert "do-not-contact" in results[0]["error"].lower() or "do not contact" in results[0]["error"].lower()
        # Must NOT have attempted to send
        mock_gc.post_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_dnc_contact_still_sent(self, db_session: Session, test_user, test_requisition, dnc_company):
        """Non-DNC contact in the same batch is still emailed."""
        from app.email_service import send_batch_rfq

        safe_contact = dnc_company["contact_ok"]
        assert safe_contact.do_not_contact is False

        vendor_groups = [
            self._make_vendor_group("Safe Vendor", safe_contact.email),
        ]

        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {
            "value": [
                {
                    "id": "sent-msg-1",
                    "conversationId": "conv-1",
                    "subject": f"RFQ for parts from Safe Vendor [ref:{test_requisition.id}]",
                }
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 1
        assert results[0]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_mixed_batch_dnc_skipped_non_dnc_sent(
        self, db_session: Session, test_user, test_requisition, dnc_company
    ):
        """Mixed batch: DNC contact skipped, non-DNC contact sent."""
        from app.email_service import send_batch_rfq

        safe_contact = dnc_company["contact_ok"]
        dnc_contact = dnc_company["contact_dnc"]
        dnc_contact.do_not_contact = True
        db_session.commit()

        vendor_groups = [
            self._make_vendor_group("Safe Vendor", safe_contact.email),
            self._make_vendor_group("DNC Vendor", dnc_contact.email),
        ]

        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {
            "value": [
                {
                    "id": "sent-msg-1",
                    "conversationId": "conv-1",
                    "subject": f"RFQ for parts from Safe Vendor [ref:{test_requisition.id}]",
                }
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 2
        statuses = {r["vendor_email"]: r["status"] for r in results}
        assert statuses[safe_contact.email] == "sent"
        assert statuses[dnc_contact.email] == "skipped"

        # Only one send attempted (the non-DNC one)
        assert mock_gc.post_json.call_count == 1

    @pytest.mark.asyncio
    async def test_dnc_skipped_result_has_vendor_name(
        self, db_session: Session, test_user, test_requisition, dnc_company
    ):
        """Skipped DNC result includes the vendor_name so the caller can display it."""
        from app.email_service import send_batch_rfq

        dnc_contact = dnc_company["contact_dnc"]
        dnc_contact.do_not_contact = True
        db_session.commit()

        vendor_groups = [
            self._make_vendor_group("DNC Corp", dnc_contact.email),
        ]

        mock_gc = AsyncMock()
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert results[0]["vendor_name"] == "DNC Corp"
        assert results[0]["vendor_email"] == dnc_contact.email
        assert results[0]["status"] == "skipped"
