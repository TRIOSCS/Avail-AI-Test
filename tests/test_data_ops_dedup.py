"""Data Ops dedup/merge queue — regression + feature tests.

Covers the Settings → Data Ops surface end-to-end:
  - the click→merge path (the reported bug: clicking a dup "just throws errors"),
  - the new Delete-both action,
  - the new multi-select bulk merge / delete / dismiss mass actions,
for both the vendor and company dedup sections.

Called by: pytest. Depends on: app.main (TestClient), real merge/delete services
(no service mocks — the bug lived in the template + swap path, so the tests drive the
real DB so a future regression in either layer is caught).
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import Company, User, VendorCard


@pytest.fixture()
def admin_client(db_session, admin_user: User) -> TestClient:
    """TestClient authenticated as an admin (mirrors test_htmx_views_nightly2)."""
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


def _vendors(db, a="Acme Components", b="Acme Components Inc"):
    """Create two near-duplicate vendor cards (distinct normalized_name — UNIQUE)."""
    v1 = VendorCard(
        normalized_name=a.lower(),
        display_name=a,
        emails=[],
        phones=[],
        sighting_count=5,
        created_at=datetime.now(timezone.utc),
    )
    v2 = VendorCard(
        normalized_name=b.lower(),
        display_name=b,
        emails=[],
        phones=[],
        sighting_count=2,
        created_at=datetime.now(timezone.utc),
    )
    db.add_all([v1, v2])
    db.commit()
    return v1, v2


def _companies(db, a="Globex Corp", b="Globex Corporation"):
    c1 = Company(name=a, is_active=True, created_at=datetime.now(timezone.utc))
    c2 = Company(name=b, is_active=True, created_at=datetime.now(timezone.utc))
    db.add_all([c1, c2])
    db.commit()
    return c1, c2


# ── PART 1: the bug — clicking a dup opens the review and merges, no error ──


class TestRenderNoCruft:
    def test_render_has_working_merge_buttons_and_no_dead_alpine(self, admin_client, db_session):
        """The render must NOT carry the dead `merged`/x-if/x-cloak wrapper that hid the
        merge buttons (the root cause), and MUST carry live hx-post merge buttons."""
        v1, v2 = _vendors(db_session)
        resp = admin_client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 200
        html = resp.text
        # Cruft is gone.
        assert "merged: false" not in html
        assert "x-if" not in html
        # Buttons are live HTMX (not gated behind a never-toggled Alpine flag).
        assert "/v2/partials/admin/vendor-merge" in html
        assert f'"keep_id": {v1.id}' in html or f'"keep_id": {v2.id}' in html

    def test_render_company_section(self, admin_client, db_session):
        _companies(db_session)
        resp = admin_client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 200
        assert "/v2/partials/admin/company-merge" in resp.text


class TestClickMerge:
    def test_vendor_merge_click_succeeds(self, admin_client, db_session):
        """Drive the real click→merge POST; the removed card is gone, kept survives."""
        v1, v2 = _vendors(db_session, "Acme X", "Acme X Inc")
        keep, remove = v1.id, v2.id
        resp = admin_client.post(
            "/v2/partials/admin/vendor-merge", data={"keep_id": str(keep), "remove_id": str(remove)}
        )
        assert resp.status_code == 200, resp.text[:1500]
        assert db_session.get(VendorCard, remove) is None
        assert db_session.get(VendorCard, keep) is not None

    def test_company_merge_click_succeeds(self, admin_client, db_session):
        c1, c2 = _companies(db_session)
        keep, remove = c1.id, c2.id
        resp = admin_client.post(
            "/v2/partials/admin/company-merge", data={"keep_id": str(keep), "remove_id": str(remove)}
        )
        assert resp.status_code == 200, resp.text[:1500]
        assert db_session.get(Company, remove) is None
        assert db_session.get(Company, keep) is not None

    def test_vendor_merge_bad_id_is_toast_not_500(self, admin_client, db_session):
        """A non-existent id must surface as an error toast (200 + HX-Trigger), never a
        500 — the vendor route now catches Exception, matching company-merge."""
        resp = admin_client.post("/v2/partials/admin/vendor-merge", data={"keep_id": "99999", "remove_id": "99998"})
        assert resp.status_code == 200
        assert "showToast" in resp.headers.get("HX-Trigger", "")


# ── PART 2: Delete both ─────────────────────────────────────────────────────


class TestDeleteBoth:
    def test_vendor_delete_both(self, admin_client, db_session):
        v1, v2 = _vendors(db_session, "Junk A", "Junk A Inc")
        a, b = v1.id, v2.id
        resp = admin_client.post("/v2/partials/admin/vendor-delete-both", data={"id_a": str(a), "id_b": str(b)})
        assert resp.status_code == 200, resp.text[:1500]
        assert db_session.get(VendorCard, a) is None
        assert db_session.get(VendorCard, b) is None

    def test_company_delete_both(self, admin_client, db_session):
        c1, c2 = _companies(db_session, "Junk Co", "Junk Co LLC")
        a, b = c1.id, c2.id
        resp = admin_client.post("/v2/partials/admin/company-delete-both", data={"id_a": str(a), "id_b": str(b)})
        assert resp.status_code == 200, resp.text[:1500]
        assert db_session.get(Company, a) is None
        assert db_session.get(Company, b) is None

    def test_vendor_delete_both_detaches_offers(self, admin_client, db_session, test_user):
        """Deleting both vendors must NOT delete dependent offers — their vendor_card_id
        is NULLed so the offer survives unlinked."""
        from app.models import Offer

        v1, v2 = _vendors(db_session, "Det A", "Det A Inc")
        offer = Offer(
            vendor_card_id=v1.id,
            vendor_name="Det A",
            mpn="LM317T",
            qty_available=10,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()
        oid = offer.id
        resp = admin_client.post("/v2/partials/admin/vendor-delete-both", data={"id_a": str(v1.id), "id_b": str(v2.id)})
        assert resp.status_code == 200, resp.text[:1500]
        db_session.expire_all()
        surviving = db_session.get(Offer, oid)
        assert surviving is not None
        assert surviving.vendor_card_id is None


# ── PART 4: multi-select bulk mass actions ──────────────────────────────────


class TestBulkActions:
    def test_render_has_multiselect_scaffold(self, admin_client, db_session):
        _vendors(db_session)
        html = admin_client.get("/v2/partials/settings/data-ops").text
        assert "dedupSelect()" in html
        assert "Select all" in html
        assert "Merge selected" in html
        assert "Delete selected" in html
        assert "Dismiss for now" in html

    def test_bulk_vendor_merge(self, admin_client, db_session):
        v1, v2 = _vendors(db_session, "Bulk A", "Bulk A Inc")
        token = f"{v1.id}-{v2.id}"
        resp = admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "merge", "pairs": token})
        assert resp.status_code == 200, resp.text[:1500]
        assert db_session.get(VendorCard, v2.id) is None
        assert db_session.get(VendorCard, v1.id) is not None

    def test_bulk_vendor_delete(self, admin_client, db_session):
        v1, v2 = _vendors(db_session, "BulkDel A", "BulkDel A Inc")
        token = f"{v1.id}-{v2.id}"
        resp = admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "delete", "pairs": token})
        assert resp.status_code == 200, resp.text[:1500]
        assert db_session.get(VendorCard, v1.id) is None
        assert db_session.get(VendorCard, v2.id) is None

    def test_bulk_company_merge(self, admin_client, db_session):
        c1, c2 = _companies(db_session, "BulkCo", "BulkCo Inc")
        token = f"{c1.id}-{c2.id}"
        resp = admin_client.post("/v2/partials/admin/company-bulk", data={"action": "merge", "pairs": token})
        assert resp.status_code == 200, resp.text[:1500]
        assert db_session.get(Company, c2.id) is None

    def test_bulk_dismiss_is_noop_render(self, admin_client, db_session):
        """Dismiss is view-only — records are untouched, render still 200."""
        v1, v2 = _vendors(db_session, "Dis A", "Dis A Inc")
        token = f"{v1.id}-{v2.id}"
        resp = admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "dismiss", "pairs": token})
        assert resp.status_code == 200
        assert db_session.get(VendorCard, v1.id) is not None
        assert db_session.get(VendorCard, v2.id) is not None

    def test_bulk_invalid_action_rejected(self, admin_client, db_session):
        resp = admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "nuke", "pairs": "1-2"})
        assert resp.status_code == 400

    def test_bulk_partial_failure_tolerated(self, admin_client, db_session):
        """A bad pair token in a batch is counted as failed, not fatal — good pairs
        still process and the response is 200."""
        v1, v2 = _vendors(db_session, "Partial A", "Partial A Inc")
        good = f"{v1.id}-{v2.id}"
        bad = "99991-99992"
        resp = admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "merge", "pairs": f"{good},{bad}"})
        assert resp.status_code == 200, resp.text[:1500]
        assert db_session.get(VendorCard, v2.id) is None  # good pair merged
