"""test_phase2_crm_enrich_sp4.py — Phase 2.3 + 2.6.

2.3 — the customer contact-enrichment path is no longer a permanent no-op: it invokes the
      live multi-provider contact-discovery waterfall (the same one the async "Find
      contacts" button uses) and PERSISTS the discovered contacts, preserving graceful
      degraded-provider handling.

2.6 — the SP4 manual "Park in prospecting" action: a rep parks their own account (or a
      manager parks anyone's) back into the prospecting pool. The account leaves the CRM
      (owner cleared), the pooled ProspectAccount carries the sales-park provenance
      (discovery_source="sales_park", parked_by_id), and non-authorized users get a 403.

Called by: pytest
Depends on: conftest fixtures (db_session, test_user, sales_user, manager_user, admin_user,
            client), app.services.customer_enrichment_service.enrich_customer_account,
            app.services.prospect_reclamation.park_company_in_prospecting.
"""

from unittest.mock import AsyncMock

from app.constants import ProspectAccountStatus
from app.dependencies import require_user
from app.main import app
from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.prospect_account import ProspectAccount
from app.services.customer_enrichment_service import enrich_customer_account
from app.services.prospect_reclamation import park_company_in_prospecting

# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_company(db, *, owner_id=None, name="Enrich Co", domain="enrichco.com") -> Company:
    co = Company(name=name, domain=domain, is_active=True, account_owner_id=owner_id)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_site(db, company, *, site_name="HQ", site_type="hq") -> CustomerSite:
    site = CustomerSite(company_id=company.id, site_name=site_name, site_type=site_type, is_active=True)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _act_as(user: User) -> None:
    """Point require_user at *user* for this test (client fixture pops it in
    teardown)."""
    app.dependency_overrides[require_user] = lambda: user


def _mock_discovery(monkeypatch, contacts, errored):
    """Patch the live discovery waterfall at its SOURCE module (per CLAUDE.md)."""
    mock = AsyncMock(return_value=(contacts, errored))
    monkeypatch.setattr("app.enrichment_service.find_suggested_contacts_with_errors", mock)
    return mock


# ── 2.3 — auto-enrich now invokes the live contact-discovery path ─────────────


class TestAutoEnrichRewire:
    async def test_invokes_live_discovery_and_persists(self, db_session, monkeypatch):
        """The stub is gone: the live waterfall is called AND contacts are persisted."""
        co = _make_company(db_session)
        site = _make_site(db_session, co)
        mock = _mock_discovery(
            monkeypatch,
            [
                {
                    "full_name": "Dana Buyer",
                    "email": "dana@enrichco.com",
                    "title": "Purchasing Manager",
                    "phone": "+1-555-0101",
                    "linkedin_url": "https://linkedin.com/in/dana",
                    "source": "clay",
                    "verified": True,
                }
            ],
            [],
        )

        result = await enrich_customer_account(co.id, db_session, force=True)

        # The live path was actually invoked with the company's domain — NOT a silent no-op.
        assert mock.await_count == 1
        assert mock.await_args.args[0] == "enrichco.com"
        assert result["status"] != "no_providers"
        assert result["contacts_added"] == 1
        assert result["contacts_verified"] == 1
        assert result["sources_used"] == ["clay"]

        contacts = db_session.query(SiteContact).filter_by(customer_site_id=site.id).all()
        assert len(contacts) == 1
        c = contacts[0]
        assert c.email == "dana@enrichco.com"
        assert c.enrichment_source == "clay"
        assert c.email_verified is True
        assert c.contact_role == "buyer"

    async def test_degraded_providers_no_crash(self, db_session, monkeypatch):
        """All providers down → no crash, degraded status, providers surfaced, 0
        added."""
        co = _make_company(db_session, domain="degraded.com")
        _make_site(db_session, co)
        mock = _mock_discovery(monkeypatch, [], ["clay", "lusha"])

        result = await enrich_customer_account(co.id, db_session, force=True)

        assert mock.await_count == 1
        assert result["contacts_added"] == 0
        assert result["status"] == "degraded"
        assert result["errored_providers"] == ["clay", "lusha"]

    async def test_dedups_existing_contact(self, db_session, monkeypatch):
        """A discovered contact whose email is already on the site is not duplicated."""
        co = _make_company(db_session, domain="dedup.com")
        site = _make_site(db_session, co)
        db_session.add(
            SiteContact(customer_site_id=site.id, full_name="Existing", email="dupe@dedup.com", is_active=True)
        )
        db_session.commit()
        _mock_discovery(
            monkeypatch,
            [
                {"full_name": "Existing", "email": "dupe@dedup.com", "source": "clay", "verified": False},
                {"full_name": "New Person", "email": "new@dedup.com", "source": "hunter", "verified": True},
            ],
            [],
        )

        result = await enrich_customer_account(co.id, db_session, force=True)

        assert result["contacts_added"] == 1
        emails = {c.email for c in db_session.query(SiteContact).filter_by(customer_site_id=site.id).all()}
        assert emails == {"dupe@dedup.com", "new@dedup.com"}


# ── 2.6 — SP4 manual "Park in prospecting" ────────────────────────────────────


class TestManualParkService:
    def test_park_stamps_sales_park_provenance(self, db_session, test_user):
        co = _make_company(db_session, owner_id=test_user.id, domain="parksvc.com")

        result = park_company_in_prospecting(co.id, test_user.id, db_session)

        db_session.refresh(co)
        assert co.account_owner_id is None  # left the owner's CRM
        pa = db_session.query(ProspectAccount).filter_by(company_id=co.id).first()
        assert pa is not None
        assert pa.discovery_source == "sales_park"
        assert pa.parked_by_id == test_user.id
        assert pa.status == ProspectAccountStatus.SUGGESTED  # in the pool
        # Manual park sets NO reclaim cooldown (immediately claimable, per Idea O).
        assert pa.reclaim_blocked_until is None
        assert result["discovery_source"] == "sales_park"
        assert result["parked_by_id"] == test_user.id


class TestParkEndpoint:
    def test_owner_parks_and_pools(self, client, db_session, test_user):
        co = _make_company(db_session, owner_id=test_user.id, domain="ownerpark.com")
        _act_as(test_user)

        resp = client.post(f"/v2/partials/customers/{co.id}/send-to-prospecting")

        assert resp.status_code == 200
        # The former owner relinquished access → redirected back to the customers list.
        assert resp.headers.get("HX-Redirect") == "/v2/customers"
        db_session.refresh(co)
        assert co.account_owner_id is None
        pa = db_session.query(ProspectAccount).filter_by(company_id=co.id).first()
        assert pa.discovery_source == "sales_park"
        assert pa.parked_by_id == test_user.id
        # Appears in the prospecting pool as a SUGGESTED account.
        pool = db_session.query(ProspectAccount).filter_by(status=ProspectAccountStatus.SUGGESTED).all()
        assert any(p.company_id == co.id for p in pool)

    def test_manager_parks_other_owner_account(self, client, db_session, test_user, manager_user, admin_user):
        co = _make_company(db_session, owner_id=admin_user.id, domain="mgrpark.com")
        _act_as(manager_user)

        resp = client.post(f"/v2/partials/customers/{co.id}/send-to-prospecting")

        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.account_owner_id is None
        pa = db_session.query(ProspectAccount).filter_by(company_id=co.id).first()
        assert pa.discovery_source == "sales_park"
        assert pa.parked_by_id == manager_user.id

    def test_non_authorized_403(self, client, db_session, test_user, admin_user, sales_user):
        co = _make_company(db_session, owner_id=admin_user.id, domain="denypark.com")
        _act_as(sales_user)

        resp = client.post(f"/v2/partials/customers/{co.id}/send-to-prospecting")

        assert resp.status_code == 403
        db_session.refresh(co)
        assert co.account_owner_id == admin_user.id  # unchanged
        assert db_session.query(ProspectAccount).filter_by(company_id=co.id).count() == 0
