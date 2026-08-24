"""test_manufacturer_alias_harvester.py — TDD tests for the manufacturer alias harvester
(survey idea #11).

normalize_brand_name misses (returns verbatim) accumulate silently as manufacturer
variants ("Seagate"/"SEAGATE"/"Seagate Technology"). This nightly harvester:
  - collects distinct manufacturer strings across offers/sightings/requirements that
    miss the canonical map (and aren't already an existing canonical/alias or already
    pending), asks Claude to map each variant to an existing canonical or new/unknown,
    and QUEUES the proposals for human approval (the spec_codes pending pattern);
  - approve appends the variant to the canonical Manufacturer.aliases JSON (never
    rewrites the raw source-reported columns — verifier build note) and busts the
    memoized alias-map cache so it is immediately visible; 'unknown' is reject-only;
  - reject marks the row 'rejected' and KEEPS it, so the variant is not re-classified
    (re-billed) and re-queued every night.

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_manufacturer_alias_harvester.py -v)
Depends on: app.services.manufacturer_alias_harvester, app.models.sourcing, admin router.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@pytest.fixture()
def admin_client(db_session, admin_user):
    """TestClient authed as an admin (require_settings_access satisfied)."""
    from app.database import get_db
    from app.dependencies import require_settings_access, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_settings_access] = lambda: admin_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_settings_access):
            app.dependency_overrides.pop(dep, None)


from app.constants import OfferStatus
from app.models import Requirement, User
from app.models.offers import Offer
from app.models.sourcing import Manufacturer, ManufacturerAliasPending, Requisition


def _canonical(db: Session, name: str, aliases=None) -> Manufacturer:
    m = Manufacturer(canonical_name=name, aliases=aliases or [])
    db.add(m)
    db.commit()
    return m


def _offer_with_mfr(db: Session, user: User, mfr: str) -> Offer:
    req = Requisition(name="MAH", customer_name="Acme", status="open", created_by=user.id)
    db.add(req)
    db.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn="LM317")
    db.add(rq)
    db.flush()
    o = Offer(
        requisition_id=req.id,
        requirement_id=rq.id,
        vendor_name="V",
        vendor_name_normalized="v",
        mpn="LM317",
        normalized_mpn="LM317",
        status=OfferStatus.ACTIVE.value,
        manufacturer=mfr,
    )
    db.add(o)
    db.commit()
    return o


# ── harvest ────────────────────────────────────────────────────────────────────


class TestHarvest:
    async def test_queues_unmatched_variant_mapped_to_existing_canonical(self, db_session, test_user):
        from app.services.manufacturer_alias_harvester import harvest_manufacturer_aliases

        _canonical(db_session, "Seagate Technology")
        _offer_with_mfr(db_session, test_user, "SEAGATE")  # variant, not canonical/alias
        with patch(
            "app.services.manufacturer_alias_harvester.claude_structured",
            new_callable=AsyncMock,
            return_value={"kind": "existing", "canonical": "Seagate Technology", "reason": "case variant"},
        ):
            n = await harvest_manufacturer_aliases(db_session)
        assert n == 1
        p = db_session.query(ManufacturerAliasPending).one()
        assert p.variant == "SEAGATE"
        assert p.proposed_canonical == "Seagate Technology"
        assert p.proposed_kind == "existing"

    async def test_skips_strings_already_canonical_or_aliased(self, db_session, test_user):
        from app.services.manufacturer_alias_harvester import harvest_manufacturer_aliases

        _canonical(db_session, "Seagate Technology", aliases=["SEAGATE"])
        _offer_with_mfr(db_session, test_user, "Seagate Technology")  # already canonical
        _offer_with_mfr(db_session, test_user, "SEAGATE")  # already an alias
        with patch("app.services.manufacturer_alias_harvester.claude_structured", new_callable=AsyncMock) as ai:
            n = await harvest_manufacturer_aliases(db_session)
        assert n == 0
        ai.assert_not_called()  # nothing unmatched → no AI spend

    async def test_does_not_duplicate_existing_pending(self, db_session, test_user):
        from app.services.manufacturer_alias_harvester import harvest_manufacturer_aliases

        _canonical(db_session, "Texas Instruments")
        db_session.add(
            ManufacturerAliasPending(
                variant="TI",
                variant_normalized="ti",
                proposed_canonical="Texas Instruments",
                proposed_kind="existing",
                source="ai",
            )
        )
        db_session.commit()
        _offer_with_mfr(db_session, test_user, "TI")  # already pending
        with patch("app.services.manufacturer_alias_harvester.claude_structured", new_callable=AsyncMock) as ai:
            n = await harvest_manufacturer_aliases(db_session)
        assert n == 0
        ai.assert_not_called()
        assert db_session.query(ManufacturerAliasPending).count() == 1

    async def test_new_unknown_kind_queued_without_canonical(self, db_session, test_user):
        from app.services.manufacturer_alias_harvester import harvest_manufacturer_aliases

        _offer_with_mfr(db_session, test_user, "Zorblax Semi")
        with patch(
            "app.services.manufacturer_alias_harvester.claude_structured",
            new_callable=AsyncMock,
            return_value={"kind": "new", "canonical": None, "reason": "not a known maker"},
        ):
            await harvest_manufacturer_aliases(db_session)
        p = db_session.query(ManufacturerAliasPending).one()
        assert p.proposed_kind == "new"
        assert p.proposed_canonical is None


# ── approve / reject ───────────────────────────────────────────────────────────


class TestApproveReject:
    def test_approve_appends_alias_to_canonical(self, db_session, test_user):
        from app.services.manufacturer_alias_harvester import approve_manufacturer_alias

        m = _canonical(db_session, "Seagate Technology", aliases=["seagate tech"])
        p = ManufacturerAliasPending(
            variant="SEAGATE",
            variant_normalized="seagate",
            proposed_canonical="Seagate Technology",
            proposed_kind="existing",
            source="ai",
        )
        db_session.add(p)
        db_session.commit()
        approve_manufacturer_alias(db_session, p.id, test_user)
        db_session.refresh(m)
        assert "SEAGATE" in m.aliases
        assert "seagate tech" in m.aliases  # existing aliases preserved
        assert db_session.query(ManufacturerAliasPending).count() == 0  # dequeued

    def test_approve_new_kind_creates_canonical(self, db_session, test_user):
        from app.services.manufacturer_alias_harvester import approve_manufacturer_alias

        p = ManufacturerAliasPending(
            variant="Zorblax Semi",
            variant_normalized="zorblax semi",
            proposed_canonical=None,
            proposed_kind="new",
            source="ai",
        )
        db_session.add(p)
        db_session.commit()
        approve_manufacturer_alias(db_session, p.id, test_user)
        m = db_session.query(Manufacturer).filter(Manufacturer.canonical_name == "Zorblax Semi").one()
        assert m.canonical_name == "Zorblax Semi"
        assert db_session.query(ManufacturerAliasPending).count() == 0  # dequeued

    def test_reject_marks_rejected_and_keeps_row(self, db_session, test_user):
        """Reject flips status to 'rejected' but KEEPS the row (the re-harvest
        marker)."""
        from app.services.manufacturer_alias_harvester import reject_manufacturer_alias

        p = ManufacturerAliasPending(
            variant="Junk", variant_normalized="junk", proposed_canonical=None, proposed_kind="unknown", source="ai"
        )
        db_session.add(p)
        db_session.commit()
        pid = p.id
        reject_manufacturer_alias(db_session, pid)
        row = db_session.get(ManufacturerAliasPending, pid)
        assert row is not None  # kept as a marker, not deleted
        assert row.status == "rejected"


# ── admin route ────────────────────────────────────────────────────────────────


def test_admin_pending_list_renders(admin_client, db_session):
    db_session.add(
        ManufacturerAliasPending(
            variant="SEAGATE",
            variant_normalized="seagate",
            proposed_canonical="Seagate Technology",
            proposed_kind="existing",
            source="ai",
        )
    )
    db_session.commit()
    resp = admin_client.get("/v2/partials/admin/manufacturer-aliases", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "SEAGATE" in resp.text
    assert "Seagate Technology" in resp.text


def test_admin_approve_route(admin_client, db_session, test_user):
    _canonical(db_session, "Seagate Technology")
    p = ManufacturerAliasPending(
        variant="SEAGATE",
        variant_normalized="seagate",
        proposed_canonical="Seagate Technology",
        proposed_kind="existing",
        source="ai",
    )
    db_session.add(p)
    db_session.commit()
    pid = p.id
    resp = admin_client.post(f"/v2/partials/admin/manufacturer-aliases/{pid}/approve", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert db_session.query(ManufacturerAliasPending).count() == 0
    m = db_session.query(Manufacturer).filter(Manufacturer.canonical_name == "Seagate Technology").one()
    assert "SEAGATE" in m.aliases


def test_admin_routes_require_settings_access(client, db_session):
    """A non-admin user cannot reach the pending queue or approve."""
    resp = client.get("/v2/partials/admin/manufacturer-aliases", headers={"HX-Request": "true"})
    assert resp.status_code in (403, 401)


# ── Adversarial-review fixes ───────────────────────────────────────────────────


class TestReviewFixes:
    def test_approve_invalidates_brand_cache(self, db_session, test_user):
        """Approve must bust the memoized _load_map cache so a newly-approved alias is
        visible to normalize_brand_name without a process restart (HIGH finding)."""
        from unittest.mock import patch as _patch

        from app.services.manufacturer_alias_harvester import approve_manufacturer_alias

        _canonical(db_session, "Seagate Technology")
        p = ManufacturerAliasPending(
            variant="SEAGATE",
            variant_normalized="seagate",
            proposed_canonical="Seagate Technology",
            proposed_kind="existing",
            source="ai",
        )
        db_session.add(p)
        db_session.commit()
        with _patch("app.services.manufacturer_alias_harvester.invalidate_canonical_map") as inv:
            approve_manufacturer_alias(db_session, p.id, test_user)
        inv.assert_called_once()

    async def test_rejected_variant_not_reharvested(self, db_session, test_user):
        """A rejected variant is not re-sent to Claude / re-queued the next night (HIGH
        finding — rejected rows persist as a marker)."""
        from app.services.manufacturer_alias_harvester import harvest_manufacturer_aliases, reject_manufacturer_alias

        _offer_with_mfr(db_session, test_user, "CDW")  # a distributor the admin will reject
        with patch(
            "app.services.manufacturer_alias_harvester.claude_structured",
            new_callable=AsyncMock,
            return_value={"kind": "unknown", "canonical": None, "reason": "distributor"},
        ):
            await harvest_manufacturer_aliases(db_session)
        p = db_session.query(ManufacturerAliasPending).filter_by(variant="CDW").one()
        reject_manufacturer_alias(db_session, p.id)

        # Next night: same source string still present — must NOT re-bill / re-queue.
        with patch("app.services.manufacturer_alias_harvester.claude_structured", new_callable=AsyncMock) as ai2:
            n = await harvest_manufacturer_aliases(db_session)
        assert n == 0
        ai2.assert_not_called()

    def test_reject_hides_from_admin_queue(self, admin_client, db_session):
        from app.services.manufacturer_alias_harvester import reject_manufacturer_alias

        p = ManufacturerAliasPending(
            variant="CDW", variant_normalized="cdw", proposed_canonical=None, proposed_kind="unknown", source="ai"
        )
        db_session.add(p)
        db_session.commit()
        reject_manufacturer_alias(db_session, p.id)
        resp = admin_client.get("/v2/partials/admin/manufacturer-aliases", headers={"HX-Request": "true"})
        assert "CDW" not in resp.text  # rejected rows are not shown

    def test_approve_unknown_is_refused(self, db_session, test_user):
        """Approving an 'unknown' must NOT mint a canonical from a raw fragment/
        distributor string (MEDIUM finding)."""
        import pytest as _pytest

        from app.services.manufacturer_alias_harvester import approve_manufacturer_alias

        p = ManufacturerAliasPending(
            variant="CDW", variant_normalized="cdw", proposed_canonical=None, proposed_kind="unknown", source="ai"
        )
        db_session.add(p)
        db_session.commit()
        with _pytest.raises(ValueError):
            approve_manufacturer_alias(db_session, p.id, test_user)
        assert db_session.query(Manufacturer).filter(Manufacturer.canonical_name == "CDW").count() == 0

    async def test_case_only_variants_dedup_to_one(self, db_session, test_user):
        """'SEAGATE' and 'seagate' normalize to one — a single AI call / pending row
        (LOW finding)."""
        from app.services.manufacturer_alias_harvester import harvest_manufacturer_aliases

        _canonical(db_session, "Seagate Technology")
        _offer_with_mfr(db_session, test_user, "SEAGATE")
        _offer_with_mfr(db_session, test_user, "seagate")
        with patch(
            "app.services.manufacturer_alias_harvester.claude_structured",
            new_callable=AsyncMock,
            return_value={"kind": "existing", "canonical": "Seagate Technology", "reason": "case"},
        ) as ai:
            await harvest_manufacturer_aliases(db_session)
        assert ai.call_count == 1
        assert db_session.query(ManufacturerAliasPending).count() == 1

    def test_post_routes_require_settings_access(self, client, db_session):
        """Both mutation routes 403 for a non-admin (not just the GET queue)."""
        r1 = client.post("/v2/partials/admin/manufacturer-aliases/1/approve", headers={"HX-Request": "true"})
        r2 = client.post("/v2/partials/admin/manufacturer-aliases/1/reject", headers={"HX-Request": "true"})
        assert r1.status_code in (401, 403)
        assert r2.status_code in (401, 403)
