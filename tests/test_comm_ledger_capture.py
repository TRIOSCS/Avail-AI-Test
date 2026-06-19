"""Phase 2 — verify the Communication Ledger already captures inbound CUSTOMER email.

The Graph inbox poll logs every non-noise inbound message through the canonical writer
``log_email_activity`` → ``match_email_to_entity``, which attributes the row to a known
customer (CustomerSite email / Company domain) by setting ``company_id``. These tests
prove that attribution end-to-end and that the resulting row flows into the CRM
``InboundCustomerSource`` alert — so no parallel capture path is needed.
"""

from app.constants import Channel, Direction
from app.models.crm import CustomerSite
from app.services.activity_service import log_email_activity
from app.services.alerts.sources.inbound_customer import InboundCustomerSource


def _seed_customer_site(db, company, email: str) -> CustomerSite:
    company.account_type = "Customer"
    site = CustomerSite(company_id=company.id, site_name="HQ", contact_email=email, is_active=True)
    db.add(site)
    db.commit()
    return site


def test_inbound_customer_email_attributed_to_company(db_session, test_user, test_company):
    _seed_customer_site(db_session, test_company, "buyer@acmecorp.test")

    rec = log_email_activity(
        user_id=test_user.id,
        direction="received",
        email_addr="buyer@acmecorp.test",
        subject="Re: pricing on the H100s",
        external_id="msg-cust-1",
        contact_name="Acme Buyer",
        db=db_session,
    )

    assert rec is not None
    assert rec.company_id == test_company.id  # attributed to the customer, not a vendor
    assert rec.vendor_card_id is None
    assert rec.direction == Direction.INBOUND
    assert rec.channel == Channel.EMAIL


def test_captured_inbound_flows_into_crm_alert(db_session, test_user, test_company):
    """A captured inbound customer email lights the CRM alert for the account owner."""
    test_company.account_owner_id = test_user.id
    _seed_customer_site(db_session, test_company, "buyer@acmecorp.test")

    log_email_activity(
        user_id=test_user.id,
        direction="received",
        email_addr="buyer@acmecorp.test",
        subject="Re: pricing",
        external_id="msg-cust-2",
        contact_name="Acme Buyer",
        db=db_session,
    )

    assert InboundCustomerSource().count_for_user(db_session, test_user) == 1
