"""IDOR regression tests for the Phase-5 account-authz gates.

A logged-in user who does NOT own/manage an account must not be able to attribute
activity to it (bumping its staleness/cadence clocks).

Covers:
  - POST /api/activity/call-initiated          → cross-account link is DROPPED (still logs the
                                                  user's own action, but not attributed)
  - POST /api/activity/outreach-initiated      → cross-account link DROPPED ("company" in dropped_links)
  - GET /api/enrich/company/{id}/status        → 403 for a non-owner (company-scoped read; carries
                                                  the guarantee formerly probed on the retired
                                                  /api/companies/{id}/activities/call|note routes)

Called by: pytest
Depends on: conftest fixtures — client is authenticated as test_user; sales_user is a
            DIFFERENT user used as the "other rep" who owns the foreign account.
"""

from datetime import UTC, datetime, timedelta

from app.models import ActivityLog
from app.models.crm import Company, CustomerSite


def _foreign_account(db_session, owner_id):
    """A company+site owned by *owner_id* (not the authenticated client)."""
    stale = datetime.now(UTC) - timedelta(days=40)
    co = Company(name="Other Rep Co", is_active=True, account_owner_id=owner_id, last_activity_at=stale)
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="Their HQ", is_active=True)
    db_session.add(site)
    db_session.commit()
    return co, site


def _not_recently_bumped(co):
    """True if last_activity_at was NOT advanced to ~now (i.e. the clock was
    untouched)."""
    return co.last_activity_at.replace(tzinfo=UTC) < datetime.now(UTC) - timedelta(days=1)


class TestActivityAuthzIDOR:
    def test_call_initiated_drops_unowned_company_link(self, client, db_session, sales_user):
        co, site = _foreign_account(db_session, sales_user.id)
        resp = client.post(
            "/api/activity/call-initiated",
            json={"phone_number": "4155551234", "company_id": co.id, "customer_site_id": site.id},
        )
        assert resp.status_code == 201  # the user's own action is still logged…
        record = db_session.get(ActivityLog, resp.json()["id"])
        assert record.company_id is None  # …but NOT attributed to the other rep's account
        assert record.customer_site_id is None
        db_session.refresh(co)
        assert _not_recently_bumped(co)  # staleness clock untouched

    def test_outreach_initiated_drops_unowned_company_link(self, client, db_session, sales_user):
        co, site = _foreign_account(db_session, sales_user.id)
        resp = client.post(
            "/api/activity/outreach-initiated",
            json={
                "channel": "phone",
                "contact_value": "+14155551234",
                "company_id": co.id,
                "customer_site_id": site.id,
            },
        )
        assert resp.status_code == 201
        assert "company" in resp.json()["dropped_links"]
        db_session.refresh(co)
        assert _not_recently_bumped(co)

    def test_company_scoped_read_denied_for_non_owner(self, client, db_session, sales_user):
        # Re-pointed from the retired POST /api/companies/{id}/activities/call|note probes:
        # the account-authz gate on company-scoped endpoints must 403 a non-owner.
        co, _site = _foreign_account(db_session, sales_user.id)
        resp = client.get(f"/api/enrich/company/{co.id}/status")
        assert resp.status_code == 403

    def test_company_scoped_read_allowed_for_owner(self, client, db_session, test_user):
        # Positive control: the owner is not blocked by the gate (no enrich run in
        # flight → empty 286 stop-polling response, not a 403).
        co, _site = _foreign_account(db_session, test_user.id)
        resp = client.get(f"/api/enrich/company/{co.id}/status")
        assert resp.status_code != 403
