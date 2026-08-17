"""test_looking_again_clone.py — one-tap Clone-to-Active from a "Looking again" outcome.

The proactive outreach tracking records looking_again but used to create nothing;
clone_line_to_active copies the line's source part into a fresh OPEN requisition via
clone_parts_to_active and stamps produced_requisition_id. Covers: source resolution
via the match's requirement, the (normalized_mpn, company) fallback, no-source and
already-cloned refusals, the ownership gate, and the row-cells rendering states
(button on looking_again, REQ label once produced).

Called by: pytest
Depends on: conftest (db_session, client, test_user), proactive_digest service
"""

from datetime import UTC, datetime

import pytest

from app.constants import SourcingStatus
from app.models import Company, Offer, ProactiveDigest, ProactiveMatch, ProactiveOutreachLine, Requirement, Requisition
from app.services.proactive_digest import clone_line_to_active


def _seed(db, user, *, with_match_requirement=True, mpn="LM317T"):
    co = Company(name="LA Clone Co", is_active=True)
    db.add(co)
    db.flush()
    req = Requisition(name="LA src req", customer_name=co.name, status="open", company_id=co.id, created_by=user.id)
    db.add(req)
    db.flush()
    part = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=mpn.lower(),
        sourcing_status=SourcingStatus.OPEN,
        target_qty=10,
    )
    db.add(part)
    db.flush()
    offer = Offer(requisition_id=req.id, mpn=mpn, vendor_name="Arrow")
    db.add(offer)
    db.flush()
    match = ProactiveMatch(
        offer_id=offer.id,
        requirement_id=part.id if with_match_requirement else None,
        mpn=mpn.lower(),
        company_id=co.id,
        salesperson_id=user.id,
        status="sent",
    )
    db.add(match)
    digest = ProactiveDigest(salesperson_id=user.id, status="sent", sent_at=datetime.now(UTC))
    db.add(digest)
    db.flush()
    line = ProactiveOutreachLine(
        digest_id=digest.id,
        match_id=match.id,
        mpn=mpn,
        company_id=co.id,
        salesperson_id=user.id,
        sent_at=datetime.now(UTC),
        outcome="looking_again",
    )
    db.add(line)
    db.commit()
    return co, part, line


class TestCloneLineToActive:
    def test_clones_via_match_requirement(self, db_session, test_user):
        co, part, line = _seed(db_session, test_user)
        line2, new_req = clone_line_to_active(db_session, line.id, viewer=test_user, can_manage=False)

        assert line2.produced_requisition_id == new_req.id
        assert new_req.status == "open"
        clones = db_session.query(Requirement).filter(Requirement.cloned_from_id == part.id).all()
        assert len(clones) == 1
        assert clones[0].requisition_id == new_req.id

    def test_fallback_resolves_by_mpn_and_company(self, db_session, test_user):
        co, part, line = _seed(db_session, test_user, with_match_requirement=False)
        line2, new_req = clone_line_to_active(db_session, line.id, viewer=test_user, can_manage=False)
        assert db_session.query(Requirement).filter(Requirement.cloned_from_id == part.id).count() == 1
        assert line2.produced_requisition_id == new_req.id

    def test_no_source_part_refused(self, db_session, test_user):
        co, part, line = _seed(db_session, test_user, with_match_requirement=False)
        # Orphan the fallback too: different company on the line.
        other = Company(name="Other Co", is_active=True)
        db_session.add(other)
        db_session.flush()
        line.company_id = other.id
        db_session.commit()
        with pytest.raises(ValueError, match="No source part"):
            clone_line_to_active(db_session, line.id, viewer=test_user, can_manage=False)

    def test_already_cloned_refused(self, db_session, test_user):
        co, part, line = _seed(db_session, test_user)
        clone_line_to_active(db_session, line.id, viewer=test_user, can_manage=False)
        with pytest.raises(ValueError, match="Already cloned"):
            clone_line_to_active(db_session, line.id, viewer=test_user, can_manage=False)

    def test_foreign_line_refused_without_manage(self, db_session, test_user, admin_user):
        co, part, line = _seed(db_session, admin_user)
        with pytest.raises(ValueError, match="Not your"):
            clone_line_to_active(db_session, line.id, viewer=test_user, can_manage=False)


class TestRowRendering:
    def test_route_clones_and_renders_req_label(self, client, db_session, test_user):
        co, part, line = _seed(db_session, test_user)
        resp = client.post(f"/v2/partials/proactive/lines/{line.id}/clone")
        assert resp.status_code == 200
        assert "Cloned" in resp.text and "REQ-" in resp.text
        assert "showToast" in resp.headers.get("HX-Trigger", "")
        db_session.refresh(line)
        assert line.produced_requisition_id is not None

    def test_tracking_rerender_shows_button_for_looking_again(self, client, db_session, test_user):
        co, part, line = _seed(db_session, test_user)
        resp = client.post(f"/v2/partials/proactive/lines/{line.id}/tracking", data={"outcome": "looking_again"})
        assert resp.status_code == 200
        assert "Clone to Active" in resp.text

    def test_no_button_for_other_outcomes(self, client, db_session, test_user):
        co, part, line = _seed(db_session, test_user)
        resp = client.post(f"/v2/partials/proactive/lines/{line.id}/tracking", data={"outcome": "still_looking"})
        assert resp.status_code == 200
        assert "Clone to Active" not in resp.text
