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

from datetime import UTC, datetime

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
        created_at=datetime.now(UTC),
    )
    v2 = VendorCard(
        normalized_name=b.lower(),
        display_name=b,
        emails=[],
        phones=[],
        sighting_count=2,
        created_at=datetime.now(UTC),
    )
    db.add_all([v1, v2])
    db.commit()
    return v1, v2


def _companies(db, a="Globex Corp", b="Globex Corporation"):
    c1 = Company(name=a, is_active=True, created_at=datetime.now(UTC))
    c2 = Company(name=b, is_active=True, created_at=datetime.now(UTC))
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
            created_at=datetime.now(UTC),
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

    def test_vendor_delete_both_cascades_notnull_children(self, admin_client, db_session, test_user):
        """REGRESSION (CRITICAL): the four NOT-NULL, ondelete=CASCADE children of a
        vendor card — VendorContact, VendorReview, VendorMetricsSnapshot,
        BuyerVendorStats — must NOT be NULLed (that raised a NotNullViolation on
        Postgres, breaking delete-both for every real vendor).

        They cascade-delete WITH the card. Insert one of each before delete-both and
        assert it succeeds and each child is gone (not orphaned).
        """
        from datetime import date

        from app.models import BuyerVendorStats, VendorContact, VendorMetricsSnapshot, VendorReview

        v1, v2 = _vendors(db_session, "Casc A", "Casc A Inc")
        contact = VendorContact(vendor_card_id=v1.id, full_name="Jane Buyer", source="manual")
        review = VendorReview(vendor_card_id=v1.id, user_id=test_user.id, rating=4, comment="ok")
        snap = VendorMetricsSnapshot(vendor_card_id=v1.id, snapshot_date=date.today(), composite_score=80.0)
        stats = BuyerVendorStats(vendor_card_id=v1.id, user_id=test_user.id, rfqs_sent=3)
        db_session.add_all([contact, review, snap, stats])
        db_session.commit()
        cid, rid, sid, bid = contact.id, review.id, snap.id, stats.id

        resp = admin_client.post("/v2/partials/admin/vendor-delete-both", data={"id_a": str(v1.id), "id_b": str(v2.id)})
        # The whole point: this is a 200, not a 500/NotNullViolation.
        assert resp.status_code == 200, resp.text[:1500]
        db_session.expire_all()

        # Both cards gone.
        assert db_session.get(VendorCard, v1.id) is None
        assert db_session.get(VendorCard, v2.id) is None
        # All four NOT-NULL children cascade-deleted — gone, NOT orphaned with a stale FK.
        assert db_session.get(VendorContact, cid) is None
        assert db_session.get(VendorReview, rid) is None
        assert db_session.get(VendorMetricsSnapshot, sid) is None
        assert db_session.get(BuyerVendorStats, bid) is None


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

    def test_bulk_dismiss_persists_canonical_rows(self, admin_client, db_session, admin_user):
        """Dismiss now persists: the keeper-first token (keeper id may be the HIGHER
        id) is canonicalized to (min, max) and attributed to the session user; the
        vendor records themselves are untouched."""
        from app.models import DedupDecision

        v1, v2 = _vendors(db_session, "Dis A", "Dis A Inc")
        lo, hi = sorted((v1.id, v2.id))
        token = f"{hi}-{lo}"  # deliberately NOT sorted — simulates keeper-first
        resp = admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "dismiss", "pairs": token})
        assert resp.status_code == 200
        # Records untouched (dismiss is never destructive).
        assert db_session.get(VendorCard, v1.id) is not None
        assert db_session.get(VendorCard, v2.id) is not None
        # Decision persisted canonically, attributed to the admin.
        row = db_session.query(DedupDecision).one()
        assert (row.entity_type, row.id_a, row.id_b) == ("vendor", lo, hi)
        assert row.decided_by_id == admin_user.id

    def test_bulk_dismiss_idempotent_across_orders(self, admin_client, db_session):
        """Dismissing the same pair in both token orders stores exactly ONE row."""
        from app.models import DedupDecision

        v1, v2 = _vendors(db_session, "Dis B", "Dis B Inc")
        for token in (f"{v1.id}-{v2.id}", f"{v2.id}-{v1.id}"):
            resp = admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "dismiss", "pairs": token})
            assert resp.status_code == 200
        assert db_session.query(DedupDecision).count() == 1

    def test_bulk_dismiss_company_persists(self, admin_client, db_session):
        from app.models import DedupDecision

        c1, c2 = _companies(db_session, "DisCo", "DisCo Inc")
        token = f"{c1.id}-{c2.id}"
        resp = admin_client.post("/v2/partials/admin/company-bulk", data={"action": "dismiss", "pairs": token})
        assert resp.status_code == 200
        row = db_session.query(DedupDecision).one()
        assert (row.entity_type, row.id_a, row.id_b) == ("company", min(c1.id, c2.id), max(c1.id, c2.id))

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

    def test_bulk_partial_failure_surfaces_in_toast(self, admin_client, db_session):
        """REGRESSION (HIGH): a partial failure must NOT show a green success toast —
        the failing pair token appears in the message and the toast kind is 'error'."""
        import json

        v1, v2 = _vendors(db_session, "ToastFail A", "ToastFail A Inc")
        good = f"{v1.id}-{v2.id}"
        bad = "99991-99992"
        resp = admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "merge", "pairs": f"{good},{bad}"})
        assert resp.status_code == 200, resp.text[:1500]
        trigger = json.loads(resp.headers["HX-Trigger"])
        toast = trigger["showToast"]
        # The failing pair token is named in the message, not reduced to a bare count.
        assert "99991-99992" in toast["message"]
        # Any failure → not green success.
        assert toast["type"] != "success"
        assert toast["type"] == "error"


# ── PART 5: persisted dedup decisions ───────────────────────────────────────


class TestDedupDecisionModel:
    def test_models_importable_and_persist(self, db_session, admin_user):
        from app.models import DedupDecision, DedupMergeAudit

        db_session.add(DedupDecision(entity_type="vendor", id_a=1, id_b=2, decided_by_id=admin_user.id))
        db_session.add(
            DedupMergeAudit(
                actor_id=admin_user.id,
                entity_type="vendor",
                action="merge",
                kept_id=1,
                kept_name="Keeper",
                removed_id=2,
                removed_name="Loser",
            )
        )
        db_session.commit()
        row = db_session.query(DedupDecision).one()
        assert (row.entity_type, row.id_a, row.id_b) == ("vendor", 1, 2)
        assert row.created_at is not None
        audit = db_session.query(DedupMergeAudit).one()
        assert audit.action == "merge"
        assert audit.created_at is not None

    def test_unique_pair_constraint(self, db_session):
        from sqlalchemy.exc import IntegrityError

        from app.models import DedupDecision

        db_session.add(DedupDecision(entity_type="vendor", id_a=1, id_b=2))
        db_session.commit()
        # Same entity_type + same canonical pair → rejected.
        db_session.add(DedupDecision(entity_type="vendor", id_a=1, id_b=2))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()
        # Same ids under a DIFFERENT entity_type is a different pair → allowed.
        db_session.add(DedupDecision(entity_type="company", id_a=1, id_b=2))
        db_session.commit()
        from app.models import DedupDecision as DD

        assert db_session.query(DD).count() == 2


class TestDismissedFiltering:
    def test_dismissed_vendor_pair_absent_from_next_render(self, admin_client, db_session):
        # Names must clear the finder's token_sort_ratio threshold (85) or the pair
        # never surfaces and the test would pass vacuously ("Hidden V" scores 80).
        v1, v2 = _vendors(db_session, "Hidden Components", "Hidden Components Inc")
        admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "dismiss", "pairs": f"{v1.id}-{v2.id}"})
        html = admin_client.get("/v2/partials/settings/data-ops").text
        assert f'data-pair="{v1.id}-{v2.id}"' not in html
        assert f'data-pair="{v2.id}-{v1.id}"' not in html
        assert "No duplicate vendors found at the current threshold." in html

    def test_dismissed_company_pair_absent_from_next_render(self, admin_client, db_session):
        c1, c2 = _companies(db_session, "Hidden Co", "Hidden Co Inc")
        admin_client.post("/v2/partials/admin/company-bulk", data={"action": "dismiss", "pairs": f"{c1.id}-{c2.id}"})
        html = admin_client.get("/v2/partials/settings/data-ops").text
        assert f'data-pair="{c1.id}-{c2.id}"' not in html
        assert f'data-pair="{c2.id}-{c1.id}"' not in html
        assert "No duplicate companies found at the current threshold." in html

    def test_undismissed_pair_still_renders(self, admin_client, db_session):
        """Filtering is per-pair — dismissing one pair must not hide another."""
        v1, v2 = _vendors(db_session, "Keep Components", "Keep Components Inc")
        v3, v4 = _vendors(db_session, "Gone Components", "Gone Components Inc")
        admin_client.post("/v2/partials/admin/vendor-bulk", data={"action": "dismiss", "pairs": f"{v3.id}-{v4.id}"})
        html = admin_client.get("/v2/partials/settings/data-ops").text
        assert f'data-pair="{v1.id}-{v2.id}"' in html
        assert f'data-pair="{v3.id}-{v4.id}"' not in html

    def test_overfetch_keeps_page_populated(self, admin_client, db_session):
        """With 2 dismissed pairs, the finder is asked for 30+2 candidates and the page
        still shows a full 30 rows after post-filtering (no starvation)."""
        from unittest.mock import patch

        from app.models import DedupDecision

        calls: list[int] = []

        def fake_finder(db, threshold=85, limit=50):
            calls.append(limit)
            return [
                {
                    "vendor_a": {"id": 10000 + i, "name": f"Fake {i}", "sightings": 5},
                    "vendor_b": {"id": 20000 + i, "name": f"Fake {i} Inc", "sightings": 2},
                    "score": 90,
                }
                for i in range(limit)
            ]

        db_session.add_all(
            [
                DedupDecision(entity_type="vendor", id_a=10000, id_b=20000),
                DedupDecision(entity_type="vendor", id_a=10001, id_b=20001),
            ]
        )
        db_session.commit()

        with patch("app.vendor_utils.find_vendor_dedup_candidates", side_effect=fake_finder):
            html = admin_client.get("/v2/partials/settings/data-ops").text

        assert calls == [32]  # 30-row page + 2 dismissed = overfetch
        # Company/contact sections are empty (empty DB), so every data-pair row is
        # a vendor row: the page is still FULL after post-filtering.
        assert html.count('data-pair="') == 30
        assert 'data-pair="10000-20000"' not in html
        assert 'data-pair="10001-20001"' not in html


# ── PART 6: per-row Dismiss endpoints ───────────────────────────────────────


class TestPerRowDismiss:
    def test_vendor_dismiss_persists_and_hides(self, admin_client, db_session, admin_user):
        from app.models import DedupDecision

        # Names must score ≥85 on token_sort_ratio so the pair WOULD render
        # absent the dismissal — otherwise the "hidden" asserts pass trivially.
        v1, v2 = _vendors(db_session, "Row Components", "Row Components Inc")
        resp = admin_client.post("/v2/partials/admin/vendor-dismiss", data={"id_a": str(v2.id), "id_b": str(v1.id)})
        assert resp.status_code == 200, resp.text[:1500]
        row = db_session.query(DedupDecision).one()
        assert (row.entity_type, row.id_a, row.id_b) == ("vendor", min(v1.id, v2.id), max(v1.id, v2.id))
        assert row.decided_by_id == admin_user.id
        # The re-rendered pane (the response body) no longer shows the pair.
        assert f'data-pair="{v1.id}-{v2.id}"' not in resp.text
        assert f'data-pair="{v2.id}-{v1.id}"' not in resp.text

    def test_company_dismiss_persists(self, admin_client, db_session):
        from app.models import DedupDecision

        c1, c2 = _companies(db_session, "Row Co", "Row Co Inc")
        resp = admin_client.post("/v2/partials/admin/company-dismiss", data={"id_a": str(c1.id), "id_b": str(c2.id)})
        assert resp.status_code == 200, resp.text[:1500]
        row = db_session.query(DedupDecision).one()
        assert row.entity_type == "company"

    def test_render_has_per_row_dismiss_buttons(self, admin_client, db_session):
        _vendors(db_session)
        _companies(db_session)
        html = admin_client.get("/v2/partials/settings/data-ops").text
        assert "/v2/partials/admin/vendor-dismiss" in html
        assert "/v2/partials/admin/company-dismiss" in html

    def test_dismiss_requires_admin(self, client, db_session):
        resp = client.post("/v2/partials/admin/vendor-dismiss", data={"id_a": "1", "id_b": "2"})
        assert resp.status_code == 403
