"""IDOR regression tests for the Phase-5 account-authz gates.

A logged-in user who does NOT own/manage an account must not be able to attribute
activity to it (bumping its staleness/cadence clocks) or promote a prospect into it.

Covers:
  - POST /api/activity/call-initiated          → cross-account link is DROPPED (still logs the
                                                  user's own action, but not attributed)
  - POST /api/activity/outreach-initiated      → cross-account link DROPPED ("company" in dropped_links)
  - POST /api/companies/{id}/activities/call   → 403
  - POST /api/companies/{id}/activities/note   → 403
  - POST /api/ai/prospect-contacts/{id}/promote → 403 for a site-linked prospect (200 for the owner)

Called by: pytest
Depends on: conftest fixtures — client is authenticated as test_user; sales_user is a
            DIFFERENT user used as the "other rep" who owns the foreign account.
"""

from datetime import UTC, datetime, timedelta

from app.models import ActivityLog
from app.models.crm import Company, CustomerSite
from app.models.enrichment import ProspectContact


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

    def test_log_company_call_denied_for_non_owner(self, client, db_session, sales_user):
        co, _site = _foreign_account(db_session, sales_user.id)
        resp = client.post(
            f"/api/companies/{co.id}/activities/call",
            json={"phone": "+15551234567", "direction": "outbound", "duration_seconds": 30},
        )
        assert resp.status_code == 403

    def test_log_company_note_denied_for_non_owner(self, client, db_session, sales_user):
        co, _site = _foreign_account(db_session, sales_user.id)
        resp = client.post(f"/api/companies/{co.id}/activities/note", json={"notes": "snooping"})
        assert resp.status_code == 403

    def test_promote_site_prospect_denied_for_non_owner(self, client, db_session, sales_user):
        co, site = _foreign_account(db_session, sales_user.id)
        pc = ProspectContact(
            customer_site_id=site.id,
            full_name="Prospect Person",
            email="p@other.com",
            source="web_search",
            confidence="high",
        )
        db_session.add(pc)
        db_session.commit()
        resp = client.post(f"/api/ai/prospect-contacts/{pc.id}/promote")
        assert resp.status_code == 403
        db_session.refresh(pc)
        assert pc.promoted_to_type is None  # not promoted

    def test_promote_site_prospect_allowed_for_owner(self, client, db_session, test_user):
        # Positive control: when the client OWNS the account, promotion succeeds (gate not over-broad).
        co, site = _foreign_account(db_session, test_user.id)
        pc = ProspectContact(
            customer_site_id=site.id,
            full_name="Owned Prospect",
            email="owned@acme.com",
            source="web_search",
            confidence="high",
        )
        db_session.add(pc)
        db_session.commit()
        resp = client.post(f"/api/ai/prospect-contacts/{pc.id}/promote")
        assert resp.status_code == 200
        db_session.refresh(pc)
        assert pc.promoted_to_type == "site_contact"
