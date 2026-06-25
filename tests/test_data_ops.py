# tests/test_data_ops.py — Data Ops settings tab: honest scan-error state,
# merge-direction disambiguation, and post-merge list refresh.
#
# What it covers:
#   - A forced vendor/company dedup scan failure renders a DISTINCT error block,
#     not the reassuring "No duplicate ... found" clean empty state.
#   - The vendor dedup rows surface a "suggested keep" hint (parity with company rows).
#   - A successful merge re-renders the surrounding Data Ops list (stale pairs drop).
#   - The Company Duplicates empty state says "companies", not "customers".
#
# Called by: pytest. Depends on: conftest fixtures (db_session, admin_user) +
# the data-ops route in app/routers/htmx_views.py.
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, VendorCard


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User):
    """TestClient authenticated as an admin user (data-ops is admin-gated)."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    def _user():
        return admin_user

    async def _token():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[require_admin] = _user
    app.dependency_overrides[require_buyer] = _user
    app.dependency_overrides[require_fresh_token] = _token

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


def _vendor(db: Session, normalized: str, display: str, sightings: int) -> VendorCard:
    card = VendorCard(
        normalized_name=normalized,
        display_name=display,
        emails=[],
        phones=[],
        sighting_count=sightings,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


class TestScanErrorState:
    def test_vendor_scan_error_shows_error_not_empty(self, admin_client, db_session, monkeypatch):
        """A raised vendor dedup scan renders an error state, NOT the clean empty
        text."""

        def _boom(*args, **kwargs):
            raise RuntimeError("scan exploded")

        monkeypatch.setattr("app.vendor_utils.find_vendor_dedup_candidates", _boom)

        resp = admin_client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 200
        html = resp.text
        low = html.lower()
        assert "couldn't" in low or "error" in low
        assert "No duplicate vendors found" not in html
        # The reassuring clean-state copy must NOT appear for vendors.
        assert "No duplicate vendors found at the current threshold" not in html

    def test_company_scan_error_shows_error_not_empty(self, admin_client, db_session, monkeypatch):
        """A raised company dedup scan renders an error state, NOT the clean empty
        text."""

        def _boom(*args, **kwargs):
            raise RuntimeError("scan exploded")

        monkeypatch.setattr("app.company_utils.find_company_dedup_candidates", _boom)

        resp = admin_client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 200
        html = resp.text
        low = html.lower()
        assert "couldn't" in low or "error" in low
        assert "No duplicate companies found at the current threshold" not in html

    def test_clean_dataset_shows_empty_state_not_error(self, admin_client, db_session):
        """With no duplicates and no failure, the reassuring clean empty state shows."""
        resp = admin_client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 200
        html = resp.text
        assert "No duplicate vendors found at the current threshold" in html
        assert "No duplicate companies found at the current threshold" in html
        # Clean state must NOT masquerade as an error.
        assert "couldn't" not in html.lower()


class TestMergeDisambiguation:
    def test_vendor_rows_show_suggested_keep_hint(self, admin_client, db_session):
        """Vendor dedup rows surface a 'suggested keep' hint, like company rows."""
        _vendor(db_session, "arrow electronics", "Arrow Electronics", 100)
        _vendor(db_session, "arrow electronic", "Arrow Electronic", 5)
        db_session.commit()

        resp = admin_client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 200
        html = resp.text
        assert "suggested keep" in html.lower()
        # The actual vendor names must render (the row is not blank/undefined).
        assert "Arrow Electronics" in html

    def test_company_empty_state_says_companies_not_customers(self, admin_client, db_session):
        """The Company Duplicates empty state uses 'companies', matching the card
        title."""
        resp = admin_client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 200
        html = resp.text
        assert "No duplicate companies found at the current threshold" in html
        assert "No duplicate customers found" not in html


class TestPostMergeRefresh:
    def test_vendor_merge_rerenders_list(self, admin_client, db_session, monkeypatch):
        """A successful vendor merge re-renders the Data Ops list (the cards
        reappear)."""
        v1 = _vendor(db_session, "v1_merge", "V1", 10)
        v2 = _vendor(db_session, "v2_merge", "V2", 1)
        db_session.commit()

        monkeypatch.setattr(
            "app.services.vendor_merge_service.merge_vendor_cards",
            lambda keep_id, remove_id, db: {"ok": True, "kept": keep_id, "removed": remove_id, "reassigned": 3},
        )

        resp = admin_client.post(
            "/v2/partials/admin/vendor-merge",
            data={"keep_id": str(v1.id), "remove_id": str(v2.id)},
        )
        assert resp.status_code == 200
        # Re-rendered full data-ops partial → the section headers come back.
        assert "Vendor Duplicates" in resp.text
        assert "Company Duplicates" in resp.text
