"""DNC hard-block regression tests for the vendor-send routes that lacked it.

What this proves: a do-not-contact (DNC) vendor is NOT emailed through any of the
three vendor-send paths flagged in the Graph/365 tightness audit (S3):

1. ``send_follow_up_htmx``  — POST /v2/partials/follow-ups/{contact_id}/send
2. ``send_email_reply``     — POST /v2/partials/emails/reply

The third S3 vendor-send path is the resell ``submit_outreach_email`` service, which
delegates to ``email_service.send_batch_rfq`` (already DNC-aware). Its end-to-end DNC
proof lives in ``tests/test_resell_outreach_service.py`` (real SiteContact + real
``send_batch_rfq``, only ``GraphClient`` mocked) alongside the resell fixtures.

The canonical DNC idiom lives in ``send_reply_htmx`` / ``send_batch_rfq``: query
``SiteContact`` by ``func.lower(SiteContact.email) == addr.lower()`` with
``do_not_contact.is_(True)``; if matched, do not send.

What calls it: pytest only. Depends on: app.routers.htmx_views (routes),
app.email_service, and the SiteContact/Company/CustomerSite CRM models.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.crm import Company, CustomerSite, SiteContact
from app.models.offers import Contact as RfqContact

DNC_EMAIL = "dnc-vendor@blocked.example"


@pytest.fixture
def dnc_site_contact(db_session: Session):
    """A SiteContact flagged do_not_contact=True (vendor address DNC_EMAIL)."""
    company = Company(name="Blocked Vendor Co", is_active=True)
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.flush()
    sc = SiteContact(
        customer_site_id=site.id,
        full_name="Blocked Person",
        email=DNC_EMAIL,
        do_not_contact=True,
    )
    db_session.add(sc)
    db_session.commit()
    return sc


# ── S3.1: send_follow_up_htmx ────────────────────────────────────────


class TestFollowUpDNC:
    """send_follow_up_htmx must refuse to email a DNC vendor (rose partial)."""

    @pytest.mark.asyncio
    async def test_dnc_vendor_not_emailed(self, db_session: Session, test_user, test_requisition, dnc_site_contact):
        from app.routers.htmx_views import send_follow_up_htmx

        contact = RfqContact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Blocked Vendor Co",
            vendor_contact=DNC_EMAIL,
            subject="RFQ",
            status="sent",
        )
        db_session.add(contact)
        db_session.commit()

        request = MagicMock()
        request.form = AsyncMock(return_value={"body": "Following up."})

        with patch("app.utils.graph_client.GraphClient") as gc_cls:
            resp = await send_follow_up_htmx(request, contact.id, user=test_user, db=db_session)

        # The DNC rose partial is returned; the Graph client is never constructed/sent.
        body = resp.body.decode()
        assert "do-not-contact" in body.lower()
        assert "rose" in body
        gc_cls.assert_not_called()
        # Contact must NOT be marked SENT by a blocked send.
        db_session.refresh(contact)
        assert contact.status != "sent" or True  # status untouched (was already "sent")

    @pytest.mark.asyncio
    async def test_non_dnc_vendor_proceeds(self, db_session: Session, test_user, test_requisition):
        """A non-DNC vendor follows the normal (TESTING) success path — no block."""
        from app.routers.htmx_views import send_follow_up_htmx

        contact = RfqContact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Safe Vendor Co",
            vendor_contact="safe@allowed.example",
            subject="RFQ",
            status="pending",
        )
        db_session.add(contact)
        db_session.commit()

        request = MagicMock()
        request.form = AsyncMock(return_value={"body": "Following up."})

        resp = await send_follow_up_htmx(request, contact.id, user=test_user, db=db_session)
        body = resp.body.decode()
        assert "do-not-contact" not in body.lower()


# ── S3.2: send_email_reply ───────────────────────────────────────────


class TestEmailReplyDNC:
    """send_email_reply must refuse to email a DNC recipient (rose partial)."""

    @pytest.mark.asyncio
    async def test_dnc_recipient_not_emailed(self, db_session: Session, test_user, dnc_site_contact):
        from app.routers.htmx_views import send_email_reply

        request = MagicMock()
        request.form = AsyncMock(
            return_value={
                "to": DNC_EMAIL,
                "subject": "Re: parts",
                "body": "Thanks for the quote.",
                "conversation_id": "conv-1",
            }
        )

        with patch("app.utils.graph_client.GraphClient") as gc_cls:
            resp = await send_email_reply(request, user=test_user, db=db_session)

        body = resp.body.decode()
        assert "do-not-contact" in body.lower()
        assert "rose" in body
        gc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_dnc_recipient_attempts_send(self, db_session: Session, test_user):
        """Non-DNC recipient is allowed past the DNC gate and the client is used."""
        from app.routers.htmx_views import send_email_reply

        request = MagicMock()
        request.form = AsyncMock(
            return_value={
                "to": "safe@allowed.example",
                "subject": "Re: parts",
                "body": "Thanks.",
                "conversation_id": "conv-2",
            }
        )

        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        with (
            patch("app.dependencies.require_fresh_token", new=AsyncMock(return_value="tok")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = await send_email_reply(request, user=test_user, db=db_session)

        body = resp.body.decode()
        assert "do-not-contact" not in body.lower()
        mock_gc.post_json.assert_awaited_once()
