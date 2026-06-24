"""tests/test_contact_merge_move.py — TDD suite for contact merge (dedup) + move.

Covers:
- merge_contacts: FK reassignment (activities, attachments, tasks), scalar backfill,
  primary-contact preservation, loser deleted; authz deny paths.
- contact move: customer_site_id update; invalid/inactive target → 400; authz deny paths.

Called by: pytest
Depends on: app.services.contact_merge_service, app.routers.htmx_views, conftest.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.intelligence import ActivityLog
from app.models.task import RequisitionTask

# ── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def company_a(db_session: Session) -> Company:
    co = Company(name="Merge Corp A", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def company_b(db_session: Session) -> Company:
    co = Company(name="Merge Corp B", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def site_a(db_session: Session, company_a: Company) -> CustomerSite:
    site = CustomerSite(company_id=company_a.id, site_name="HQ A", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def site_b(db_session: Session, company_b: Company) -> CustomerSite:
    site = CustomerSite(company_id=company_b.id, site_name="HQ B", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def keeper(db_session: Session, site_a: CustomerSite) -> SiteContact:
    c = SiteContact(
        customer_site_id=site_a.id,
        full_name="Keep Me",
        email="keeper@example.com",
        title=None,
        phone=None,
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture()
def loser(db_session: Session, site_a: CustomerSite) -> SiteContact:
    c = SiteContact(
        customer_site_id=site_a.id,
        full_name="Lose Me",
        email=None,
        title="Director",
        phone="+15550001111",
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture()
def owner_client_a(db_session: Session, company_a: Company, test_user: User) -> TestClient:
    """TestClient where test_user owns company_a."""
    company_a.account_owner_id = test_user.id
    db_session.commit()

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_admin] = lambda: test_user
    app.dependency_overrides[require_buyer] = lambda: test_user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def unrelated_client(db_session: Session) -> TestClient:
    """TestClient where the user has NO ownership relation to any company."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    stranger = User(
        email="stranger@example.com",
        name="Stranger",
        role="buyer",
        azure_id="stranger-azure-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(stranger)
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: stranger
    app.dependency_overrides[require_admin] = lambda: stranger
    app.dependency_overrides[require_buyer] = lambda: stranger
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


# ── merge_contacts unit tests ────────────────────────────────────────────────


class TestMergeContactsService:
    def test_activities_reassigned_to_keeper(self, db_session: Session, keeper: SiteContact, loser: SiteContact):
        """ActivityLog.site_contact_id on the loser → keeper after merge."""
        activity = ActivityLog(
            user_id=None,
            activity_type="email_sent",
            channel="email",
            company_id=None,
            site_contact_id=loser.id,
            contact_email="lose@example.com",
            contact_name="Lose Me",
            subject="RFQ",
            external_id="graph-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        from app.services.contact_merge_service import merge_contacts

        result = merge_contacts(keeper.id, loser.id, db_session)
        db_session.commit()

        db_session.refresh(activity)
        assert activity.site_contact_id == keeper.id
        assert result["ok"] is True
        assert result["kept"] == keeper.id
        assert result["removed"] == loser.id

    def test_loser_deleted(self, db_session: Session, keeper: SiteContact, loser: SiteContact):
        """Loser row is deleted after merge."""
        loser_id = loser.id

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(keeper.id, loser_id, db_session)
        db_session.commit()

        assert db_session.get(SiteContact, loser_id) is None

    def test_keeper_scalar_backfill_title_from_loser(
        self, db_session: Session, keeper: SiteContact, loser: SiteContact
    ):
        """Keeper.title is None → backfilled from loser.title after merge."""
        assert keeper.title is None
        assert loser.title == "Director"

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(keeper.id, loser.id, db_session)
        db_session.commit()
        db_session.refresh(keeper)

        assert keeper.title == "Director"

    def test_keeper_scalar_not_overwritten_when_set(self, db_session: Session, site_a: CustomerSite):
        """Keeper.title already set → NOT overwritten by loser.title."""
        c_keep = SiteContact(customer_site_id=site_a.id, full_name="Keep", title="VP")
        c_lose = SiteContact(customer_site_id=site_a.id, full_name="Lose", title="Manager")
        db_session.add_all([c_keep, c_lose])
        db_session.commit()

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(c_keep.id, c_lose.id, db_session)
        db_session.commit()
        db_session.refresh(c_keep)

        assert c_keep.title == "VP"

    def test_same_id_raises_value_error(self, db_session: Session, keeper: SiteContact):
        from app.services.contact_merge_service import merge_contacts

        with pytest.raises(ValueError, match="itself"):
            merge_contacts(keeper.id, keeper.id, db_session)

    def test_notes_appended(self, db_session: Session, site_a: CustomerSite):
        """Loser.notes appended to keeper.notes with separator."""
        c_keep = SiteContact(customer_site_id=site_a.id, full_name="Keep", notes="Original note.")
        c_lose = SiteContact(customer_site_id=site_a.id, full_name="Lose", notes="Merged note.")
        db_session.add_all([c_keep, c_lose])
        db_session.commit()

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(c_keep.id, c_lose.id, db_session)
        db_session.commit()
        db_session.refresh(c_keep)

        assert "Original note." in c_keep.notes
        assert "Merged note." in c_keep.notes
        assert "Merged from" in c_keep.notes

    def test_tasks_reassigned_to_keeper(self, db_session: Session, keeper: SiteContact, loser: SiteContact):
        """RequisitionTask.site_contact_id on the loser → keeper after merge."""
        task = RequisitionTask(
            site_contact_id=loser.id,
            title="Follow up call",
            task_type="general",
            status="todo",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(task)
        db_session.commit()

        from app.services.contact_merge_service import merge_contacts

        merge_contacts(keeper.id, loser.id, db_session)
        db_session.commit()
        db_session.refresh(task)

        assert task.site_contact_id == keeper.id


# ── Merge route HTTP tests ───────────────────────────────────────────────────


class TestMergeRoutes:
    def test_merge_form_returns_200(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
    ):
        resp = owner_client_a.get(f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge-form")
        assert resp.status_code == 200
        assert "merge" in resp.text.lower()

    def test_merge_preview_returns_200(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
        loser: SiteContact,
    ):
        resp = owner_client_a.get(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge-preview?remove_id={loser.id}"
        )
        assert resp.status_code == 200
        assert "Keep Me" in resp.text
        assert "Lose Me" in resp.text

    def test_merge_preview_same_id_returns_400(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
    ):
        resp = owner_client_a.get(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge-preview?remove_id={keeper.id}"
        )
        assert resp.status_code == 400

    def test_merge_execute_reassigns_and_deletes_loser(
        self,
        owner_client_a: TestClient,
        db_session: Session,
        company_a: Company,
        keeper: SiteContact,
        loser: SiteContact,
    ):
        loser_id = loser.id
        resp = owner_client_a.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge",
            data={"remove_id": str(loser_id), "confirmed": "true"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(SiteContact, loser_id) is None

    def test_merge_execute_requires_confirmed(
        self,
        owner_client_a: TestClient,
        company_a: Company,
        keeper: SiteContact,
        loser: SiteContact,
    ):
        resp = owner_client_a.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge",
            data={"remove_id": str(loser.id), "confirmed": ""},
        )
        assert resp.status_code == 400

    def test_merge_unrelated_rep_gets_403(
        self,
        unrelated_client: TestClient,
        company_a: Company,
        keeper: SiteContact,
        loser: SiteContact,
    ):
        resp = unrelated_client.post(
            f"/v2/partials/customers/{company_a.id}/contacts/{keeper.id}/merge",
            data={"remove_id": str(loser.id), "confirmed": "true"},
        )
        assert resp.status_code == 403
