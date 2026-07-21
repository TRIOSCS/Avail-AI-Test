"""tests/test_company_segment_tags_idor.py — Cross-tenant IDOR regression guard.

company_segment_tags_partial (GET /v2/partials/customers/{company_id}/segment-tags)
must not leak a company's segment tags to an unrelated buyer. It must gate on
can_manage_account like its POST/DELETE peers, returning 404 for out-of-scope
companies (read convention: indistinguishable from missing).

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx.companies.tags, app.dependencies
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company

# ── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def owned_company(db_session: Session, test_user: User) -> Company:
    """A company owned by test_user (account_owner_id set)."""
    co = Company(
        name="Owned Corp SegTags IDOR",
        is_active=True,
        account_owner_id=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def unrelated_client(db_session: Session) -> TestClient:
    """TestClient authenticated as a user who owns NO companies/sites."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    stranger = User(
        email="stranger_segtags_idor@example.com",
        name="Stranger SegTags IDOR",
        role="buyer",
        azure_id="stranger-azure-segtags-idor",
        created_at=datetime.now(UTC),
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


# ── company_segment_tags_partial ─────────────────────────────────────────────


class TestCompanySegmentTagsPartialIDOR:
    """company_segment_tags_partial must gate on can_manage_account."""

    def test_unrelated_rep_gets_404(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
    ):
        """Unrelated rep reading another company's segment tags must get 404."""
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/segment-tags",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_owner_gets_200(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        owned_company: Company,
    ):
        """Account owner reading their own company's segment tags must get 200."""
        # test_user IS already set as account_owner_id on owned_company via fixture.
        # Seed a segment tag so the chip (denied to the stranger) actually renders.
        from app.services.tagging import assign_segment_tag, get_or_create_segment_tag

        tag = get_or_create_segment_tag("Strategic-OEM", db_session)
        assign_segment_tag(company_id=owned_company.id, tag_id=tag.id, db=db_session)
        db_session.commit()
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/segment-tags",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The owner sees the segment tag the stranger's 404 hid.
        assert "Strategic-OEM" in resp.text
