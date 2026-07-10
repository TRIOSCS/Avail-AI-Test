"""tests/test_crm_disposition.py — Increment 1: account/contact disposition.

TDD spec for the disposition feature ("Increment 1 — Disposition").

Covers:
  * prospect_claim.send_company_to_prospecting (ownership clear + pool dedupe +
    no-domain fallback + owner-or-admin gate)
  * crm_service._needs_call_filter / cdm_overdue_count bucket suppression
    (count==list invariant; NULL == active)
  * crm_service.company_contact_rows priority/archive sort (archived stay,
    sort to bottom; priority first)
  * setter routes (disposition allowlist + owner-or-admin; priority/archive
    IDOR company-scope)

Written FIRST (TDD) — fails until the production code lands.

Called by: pytest
Depends on: app.services.prospect_claim, app.services.crm_service, app.models,
            app.routers.htmx_views (via the TestClient `client` fixture)
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.constants import CompanyDisposition, ProspectAccountStatus
from app.models import Company, CustomerSite, SiteContact, User
from app.models.prospect_account import ProspectAccount
from app.services import crm_service
from app.services.prospect_claim import send_company_to_prospecting

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_company(
    db: Session,
    *,
    name: str = "Disp Co",
    owner_id: int | None = None,
    domain: str | None = None,
    disposition: str | None = None,
    last_outbound_at: datetime | None = None,
) -> Company:
    co = Company(
        name=name,
        is_active=True,
        account_owner_id=owner_id,
        domain=domain,
        disposition=disposition,
        last_outbound_at=last_outbound_at,
    )
    db.add(co)
    db.flush()
    return co


def _make_site(db: Session, company: Company) -> CustomerSite:
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()
    return site


def _make_contact(
    db: Session,
    site: CustomerSite,
    *,
    full_name: str,
    is_primary: bool = False,
    is_priority: bool = False,
    is_archived: bool = False,
) -> SiteContact:
    c = SiteContact(
        customer_site_id=site.id,
        full_name=full_name,
        is_active=True,
        is_primary=is_primary,
        is_priority=is_priority,
        is_archived=is_archived,
    )
    db.add(c)
    db.flush()
    return c


def _make_user(db: Session, *, email: str, role: str = "sales") -> User:
    u = User(email=email, name=email, role=role, azure_id=f"az-{email}")
    db.add(u)
    db.flush()
    return u


# ─────────────────────────────────────────────────────────────────────────────
# TestSendCompanyToProspecting
# ─────────────────────────────────────────────────────────────────────────────


class TestSendCompanyToProspecting:
    def test_clears_ownership_and_sets_cleared_at(self, db_session: Session):
        owner = _make_user(db_session, email="owner1@trioscs.com")
        co = _make_company(db_session, owner_id=owner.id, domain="acme.com")
        db_session.commit()

        send_company_to_prospecting(co.id, owner.id, db_session)

        db_session.refresh(co)
        assert co.account_owner_id is None
        assert co.ownership_cleared_at is not None
        # is_active must NOT be flipped — bucket/send never uses is_active.
        assert co.is_active is True

    def test_creates_suggested_prospect_when_domain_present(self, db_session: Session):
        owner = _make_user(db_session, email="owner2@trioscs.com")
        co = _make_company(db_session, owner_id=owner.id, domain="widgets.com")
        db_session.commit()

        send_company_to_prospecting(co.id, owner.id, db_session)

        pa = db_session.query(ProspectAccount).filter(ProspectAccount.domain == "widgets.com").first()
        assert pa is not None
        assert pa.status == ProspectAccountStatus.SUGGESTED

    def test_dedupes_existing_prospect_by_domain(self, db_session: Session):
        owner = _make_user(db_session, email="owner3@trioscs.com")
        co = _make_company(db_session, owner_id=owner.id, domain="dupe.com")
        existing = ProspectAccount(
            name="Pre-existing",
            domain="dupe.com",
            discovery_source="manual",
            status=ProspectAccountStatus.SUGGESTED,
        )
        db_session.add(existing)
        db_session.commit()

        send_company_to_prospecting(co.id, owner.id, db_session)

        rows = db_session.query(ProspectAccount).filter(ProspectAccount.domain == "dupe.com").all()
        assert len(rows) == 1

    def test_no_domain_clears_ownership_only_no_pool_row(self, db_session: Session):
        owner = _make_user(db_session, email="owner4@trioscs.com")
        co = _make_company(db_session, owner_id=owner.id, domain=None)
        db_session.commit()
        before = db_session.query(ProspectAccount).count()

        send_company_to_prospecting(co.id, owner.id, db_session)

        db_session.refresh(co)
        assert co.account_owner_id is None
        assert co.ownership_cleared_at is not None
        assert db_session.query(ProspectAccount).count() == before

    def test_non_owner_non_admin_raises(self, db_session: Session):
        owner = _make_user(db_session, email="owner5@trioscs.com")
        other = _make_user(db_session, email="other5@trioscs.com")
        co = _make_company(db_session, owner_id=owner.id, domain="x.com")
        db_session.commit()

        with pytest.raises(ValueError):
            send_company_to_prospecting(co.id, other.id, db_session, is_admin=False)

        db_session.refresh(co)
        assert co.account_owner_id == owner.id  # untouched

    def test_admin_can_act_on_another_owners_account(self, db_session: Session):
        owner = _make_user(db_session, email="owner6@trioscs.com")
        admin = _make_user(db_session, email="admin6@trioscs.com", role="admin")
        co = _make_company(db_session, owner_id=owner.id, domain="y.com")
        db_session.commit()

        send_company_to_prospecting(co.id, admin.id, db_session, is_admin=True)

        db_session.refresh(co)
        assert co.account_owner_id is None

    def test_missing_company_raises_lookup(self, db_session: Session):
        owner = _make_user(db_session, email="owner7@trioscs.com")
        db_session.commit()
        with pytest.raises(LookupError):
            send_company_to_prospecting(999999, owner.id, db_session)


# ─────────────────────────────────────────────────────────────────────────────
# TestBucketSuppression — count == list invariant
# ─────────────────────────────────────────────────────────────────────────────


class TestBucketSuppression:
    def _setup(self, db_session: Session):
        owner = _make_user(db_session, email="seller@trioscs.com", role="sales")
        old = NOW - timedelta(days=90)  # past the 30d red line → needs a call
        active_co = _make_company(db_session, name="Active", owner_id=owner.id, disposition=None, last_outbound_at=old)
        null_co = _make_company(db_session, name="NullDisp", owner_id=owner.id, disposition=None, last_outbound_at=old)
        bucket_co = _make_company(
            db_session,
            name="Bucketed",
            owner_id=owner.id,
            disposition=CompanyDisposition.BUCKET,
            last_outbound_at=old,
        )
        db_session.commit()
        return owner, active_co, null_co, bucket_co

    def test_bucketed_excluded_from_needs_call_count_and_list(self, db_session: Session):
        owner, active_co, null_co, bucket_co = self._setup(db_session)

        count = crm_service.cdm_overdue_count(db_session, owner, now=NOW)
        q = crm_service.cdm_company_query(
            db_session,
            owner,
            search="",
            staleness="needs_call",
            account_type="",
            my_only=True,
            sort="oldest",
            now=NOW,
        )
        listed = q.all()
        listed_ids = {c.id for c in listed}

        # Count == list length — the shared-predicate invariant.
        assert count == len(listed)
        # Bucketed is gone; active + NULL-disposition both remain.
        assert bucket_co.id not in listed_ids
        assert active_co.id in listed_ids
        assert null_co.id in listed_ids

    def test_null_disposition_behaves_as_active(self, db_session: Session):
        owner, active_co, null_co, _bucket_co = self._setup(db_session)
        q = crm_service.cdm_company_query(
            db_session,
            owner,
            search="",
            staleness="needs_call",
            account_type="",
            my_only=True,
            sort="oldest",
            now=NOW,
        )
        listed_ids = {c.id for c in q.all()}
        assert null_co.id in listed_ids

    def test_base_query_hides_bucketed_but_facet_reveals(self, db_session: Session):
        owner, active_co, _null_co, bucket_co = self._setup(db_session)

        base = crm_service.cdm_company_query(
            db_session,
            owner,
            search="",
            staleness="",
            account_type="",
            my_only=True,
            sort="oldest",
            now=NOW,
        )
        base_ids = {c.id for c in base.all()}
        assert bucket_co.id not in base_ids
        assert active_co.id in base_ids

        faceted = crm_service.cdm_company_query(
            db_session,
            owner,
            search="",
            staleness="bucket",
            account_type="",
            my_only=True,
            sort="oldest",
            now=NOW,
        )
        faceted_ids = {c.id for c in faceted.all()}
        assert bucket_co.id in faceted_ids


# ─────────────────────────────────────────────────────────────────────────────
# TestContactSort — archived last, priority first, archived NOT filtered out
# ─────────────────────────────────────────────────────────────────────────────


class TestContactSort:
    def test_priority_first_archived_last_all_present(self, db_session: Session):
        co = _make_company(db_session, name="SortCo")
        site = _make_site(db_session, co)
        # plain, priority, archived, primary
        _make_contact(db_session, site, full_name="Plain")
        _make_contact(db_session, site, full_name="Priority", is_priority=True)
        _make_contact(db_session, site, full_name="Archived", is_archived=True)
        _make_contact(db_session, site, full_name="Primary", is_primary=True)
        db_session.commit()

        rows = crm_service.company_contact_rows(db_session, co.id)
        names = [r["contact"].full_name for r in rows if r["contact"] is not None]

        # All four still present — archived must NOT be filtered out.
        assert set(names) == {"Plain", "Priority", "Archived", "Primary"}
        # Priority sorts to the very top; archived to the very bottom.
        assert names[0] == "Priority"
        assert names[-1] == "Archived"

    def test_legacy_rows_dont_crash_sort(self, db_session: Session):
        co = _make_company(db_session, name="LegacyCo")
        # Legacy site-level contact (no SiteContact row) → contact is None row.
        site = CustomerSite(
            company_id=co.id,
            site_name="HQ",
            is_active=True,
            contact_name="Legacy Person",
            contact_email="legacy@x.com",
        )
        db_session.add(site)
        db_session.flush()
        _make_contact(db_session, site, full_name="Real", is_priority=True)
        db_session.commit()

        rows = crm_service.company_contact_rows(db_session, co.id)
        # Must include both the real and the legacy row without raising.
        assert any(r["legacy"] for r in rows)
        assert any(r["contact"] is not None for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
# TestSetterRoutes — disposition allowlist + owner-or-admin; IDOR scope
# ─────────────────────────────────────────────────────────────────────────────


class TestDispositionRoute:
    def test_invalid_disposition_400(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="DispRoute", owner_id=test_user.id)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{co.id}/disposition",
            data={"disposition": "garbage"},
        )
        assert resp.status_code == 400

    def test_valid_disposition_owner_writes(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="DispRoute2", owner_id=test_user.id)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{co.id}/disposition",
            data={"disposition": "bucket", "disposition_reason": "no recent demand"},
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.disposition == CompanyDisposition.BUCKET
        assert co.disposition_reason == "no recent demand"
        assert co.disposition_set_by == test_user.id
        assert co.disposition_set_at is not None

    def test_disposition_reversible_to_active(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="DispRoute3", owner_id=test_user.id, disposition=CompanyDisposition.BUCKET)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{co.id}/disposition",
            data={"disposition": "active"},
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.disposition == CompanyDisposition.ACTIVE

    def test_non_owner_non_admin_forbidden(self, client, db_session: Session, test_user: User):
        other = _make_user(db_session, email="someoneelse@trioscs.com")
        co = _make_company(db_session, name="DispRoute4", owner_id=other.id)
        db_session.commit()
        # client's authed user is test_user (buyer, not admin, not owner).
        resp = client.post(
            f"/v2/partials/customers/{co.id}/disposition",
            data={"disposition": "bucket"},
        )
        assert resp.status_code == 403
        db_session.refresh(co)
        assert co.disposition in (None, CompanyDisposition.ACTIVE)


@pytest.fixture()
def _grant_account_management(test_user: User, db_session: Session) -> None:
    """Promote the buyer ``test_user`` to MANAGER so it can_manage every account.

    The send-to-prospecting route clears the company's ``account_owner_id`` and then
    re-renders the company detail partial, which now gates on ``can_manage_account``.
    Once ownership is cleared the buyer would 404 on that re-render, so promote the actor
    to MANAGER (``can_manage_account`` stays True regardless of ownership) to exercise the
    authorized render path. The clears-ownership assertion is unaffected — the service
    still nulls ``account_owner_id``.
    """
    test_user.role = "manager"
    db_session.commit()


@pytest.mark.usefixtures("_grant_account_management")
class TestSendToProspectingRoute:
    def test_route_clears_ownership(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="SendRoute", owner_id=test_user.id, domain="sendroute.com")
        db_session.commit()
        resp = client.post(f"/v2/partials/customers/{co.id}/send-to-prospecting")
        assert resp.status_code == 200
        db_session.refresh(co)
        assert co.account_owner_id is None


class TestContactToggleRoutes:
    def test_priority_toggle_sets_flag(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="PrioCo", owner_id=test_user.id)
        site = _make_site(db_session, co)
        c = _make_contact(db_session, site, full_name="Pri")
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{co.id}/contacts/{c.id}/priority",
            data={"is_priority": "1"},
        )
        assert resp.status_code == 200
        db_session.refresh(c)
        assert c.is_priority is True

    def test_archive_toggle_sets_flag(self, client, db_session: Session, test_user: User):
        co = _make_company(db_session, name="ArchCo", owner_id=test_user.id)
        site = _make_site(db_session, co)
        c = _make_contact(db_session, site, full_name="Arc")
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{co.id}/contacts/{c.id}/archive",
            data={"is_archived": "1"},
        )
        assert resp.status_code == 200
        db_session.refresh(c)
        assert c.is_archived is True

    def test_priority_idor_wrong_company_404(self, client, db_session: Session, test_user: User):
        co_a = _make_company(db_session, name="A", owner_id=test_user.id)
        co_b = _make_company(db_session, name="B", owner_id=test_user.id)
        site_b = _make_site(db_session, co_b)
        c_b = _make_contact(db_session, site_b, full_name="UnderB")
        db_session.commit()
        # Address contact under company A's path → IDOR scope filter must 404.
        resp = client.post(
            f"/v2/partials/customers/{co_a.id}/contacts/{c_b.id}/priority",
            data={"is_priority": "1"},
        )
        assert resp.status_code == 404

    def test_archive_idor_wrong_company_404(self, client, db_session: Session, test_user: User):
        co_a = _make_company(db_session, name="A2", owner_id=test_user.id)
        co_b = _make_company(db_session, name="B2", owner_id=test_user.id)
        site_b = _make_site(db_session, co_b)
        c_b = _make_contact(db_session, site_b, full_name="UnderB2")
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{co_a.id}/contacts/{c_b.id}/archive",
            data={"is_archived": "1"},
        )
        assert resp.status_code == 404
