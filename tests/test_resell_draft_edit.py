"""test_resell_draft_edit.py — the draft-edit set (Phase 4 Task 2, finding #14 / D4).

Before a list is posted it is a private working draft; the owner must be able to correct
it in place — edit/delete a line, edit the list header, or delete the whole draft — instead
of the module's old dead-end (post-then-locked with no undo). All four mutations are
DRAFT-ONLY and owner-only (409 once posted, 403 for a non-owner, 404 across lists), and a
draft carries no offers/mirror so they are side-effect-free except ``total_line_items``.

Covers the four services (delete_line / update_line / update_excess_list /
delete_excess_list), their routes, the re-validated ``quantity > 0`` on the edit path
(the model ``@validates`` would 500 otherwise), and the honest 409 copy.

Called by: pytest
Depends on: app.services.excess_service, app.routers.resell, tests.conftest
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.models.intelligence import MaterialCard
from app.services import excess_service
from app.services.excess_service import create_excess_list, import_line_items

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def owner(db_session: Session) -> User:
    u = User(email="de-owner@trioscs.com", name="Del Owner", role="trader", azure_id="de-owner-1")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def outsider(db_session: Session) -> User:
    u = User(email="de-out@trioscs.com", name="Ora Outsider", role="trader", azure_id="de-out-1")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(name="Draft-Edit Seller Co")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def _draft_with_lines(db: Session, owner: User, company: Company, parts=("LM358N", "MAX232")) -> ExcessList:
    el = create_excess_list(db, title="Draft to edit", company_id=company.id, owner_id=owner.id)
    import_line_items(db, el.id, [{"part_number": p, "quantity": "100"} for p in parts])
    db.refresh(el)
    return el


def _lines(db: Session, el: ExcessList) -> list[ExcessLineItem]:
    return db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()


# ═══════════════════════════════════════════════════════════════════════
#  delete_line
# ═══════════════════════════════════════════════════════════════════════


class TestDeleteLine:
    def test_deletes_line_and_decrements_counter(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        assert el.total_line_items == 2
        line = _lines(db_session, el)[0]

        excess_service.delete_line(db_session, el.id, line.id, owner)

        remaining = _lines(db_session, el)
        assert len(remaining) == 1
        assert line.id not in {li.id for li in remaining}
        db_session.refresh(el)
        assert el.total_line_items == 1

    def test_non_owner_403(self, db_session, owner, outsider, company):
        el = _draft_with_lines(db_session, owner, company)
        line = _lines(db_session, el)[0]
        with pytest.raises(HTTPException) as exc:
            excess_service.delete_line(db_session, el.id, line.id, outsider)
        assert exc.value.status_code == 403
        assert len(_lines(db_session, el)) == 2  # untouched

    def test_posted_list_409(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        el.status = ExcessListStatus.COLLECTING
        db_session.commit()
        line = _lines(db_session, el)[0]
        with pytest.raises(HTTPException) as exc:
            excess_service.delete_line(db_session, el.id, line.id, owner)
        assert exc.value.status_code == 409
        assert len(_lines(db_session, el)) == 2

    def test_cross_list_line_404(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        other = _draft_with_lines(db_session, owner, company, parts=("NE555P",))
        stray = _lines(db_session, other)[0]
        with pytest.raises(HTTPException) as exc:
            excess_service.delete_line(db_session, el.id, stray.id, owner)
        assert exc.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  update_line
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateLine:
    def test_updates_fields(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        line = _lines(db_session, el)[0]

        excess_service.update_line(
            db_session,
            el.id,
            line.id,
            owner,
            part_number="LM358N",
            quantity=250,
            manufacturer="Texas Instruments",
            condition="Used",
            date_code="2024+",
            asking_price=Decimal("1.75"),
        )

        db_session.refresh(line)
        assert line.quantity == 250
        assert line.manufacturer == "Texas Instruments"
        assert line.condition == "Used"
        assert line.date_code == "2024+"
        assert line.asking_price == Decimal("1.75")

    def test_zero_quantity_rejected_400_not_500(self, db_session, owner, company):
        """Re-validate quantity > 0 in the service — the model @validates would 500."""
        el = _draft_with_lines(db_session, owner, company)
        line = _lines(db_session, el)[0]
        with pytest.raises(HTTPException) as exc:
            excess_service.update_line(db_session, el.id, line.id, owner, part_number="LM358N", quantity=0)
        assert exc.value.status_code == 400
        db_session.refresh(line)
        assert line.quantity == 100  # unchanged

    def test_mpn_change_reresolves_material_card(self, db_session, owner, company):
        """Editing the part number re-resolves the MaterialCard link (drops the stale
        one)."""
        # Seed the real target card so the new MPN resolves to a concrete row.
        db_session.add(MaterialCard(normalized_mpn="max232", display_mpn="MAX232"))
        db_session.commit()
        el = _draft_with_lines(db_session, owner, company, parts=("LM358N",))
        line = _lines(db_session, el)[0]
        original_card_id = line.material_card_id

        excess_service.update_line(db_session, el.id, line.id, owner, part_number="MAX232", quantity=100)

        db_session.refresh(line)
        assert line.part_number == "MAX232"
        assert line.material_card_id != original_card_id
        card = db_session.get(MaterialCard, line.material_card_id)
        assert card is not None and card.normalized_mpn == "max232"

    def test_non_owner_403(self, db_session, owner, outsider, company):
        el = _draft_with_lines(db_session, owner, company)
        line = _lines(db_session, el)[0]
        with pytest.raises(HTTPException) as exc:
            excess_service.update_line(db_session, el.id, line.id, outsider, part_number="LM358N", quantity=5)
        assert exc.value.status_code == 403

    def test_posted_list_409(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        el.status = ExcessListStatus.COLLECTING
        db_session.commit()
        line = _lines(db_session, el)[0]
        with pytest.raises(HTTPException) as exc:
            excess_service.update_line(db_session, el.id, line.id, owner, part_number="LM358N", quantity=5)
        assert exc.value.status_code == 409

    def test_cross_list_line_404(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        other = _draft_with_lines(db_session, owner, company, parts=("NE555P",))
        stray = _lines(db_session, other)[0]
        with pytest.raises(HTTPException) as exc:
            excess_service.update_line(db_session, el.id, stray.id, owner, part_number="X", quantity=5)
        assert exc.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  update_excess_list
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateExcessList:
    def test_updates_title_notes_company(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        other_co = Company(name="Reassigned Customer")
        db_session.add(other_co)
        db_session.commit()

        excess_service.update_excess_list(
            db_session, el.id, owner, title="Renamed draft", notes="new notes", company_id=other_co.id
        )

        db_session.refresh(el)
        assert el.title == "Renamed draft"
        assert el.notes == "new notes"
        assert el.company_id == other_co.id

    def test_bad_company_404(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        with pytest.raises(HTTPException) as exc:
            excess_service.update_excess_list(db_session, el.id, owner, title="X", notes=None, company_id=999999)
        assert exc.value.status_code == 404
        db_session.refresh(el)
        assert el.company_id == company.id  # unchanged

    def test_non_owner_403(self, db_session, owner, outsider, company):
        el = _draft_with_lines(db_session, owner, company)
        with pytest.raises(HTTPException) as exc:
            excess_service.update_excess_list(db_session, el.id, outsider, title="X", notes=None, company_id=company.id)
        assert exc.value.status_code == 403

    def test_posted_list_409(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        el.status = ExcessListStatus.COLLECTING
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            excess_service.update_excess_list(db_session, el.id, owner, title="X", notes=None, company_id=company.id)
        assert exc.value.status_code == 409


# ═══════════════════════════════════════════════════════════════════════
#  delete_excess_list
# ═══════════════════════════════════════════════════════════════════════


class TestDeleteExcessList:
    def test_deletes_list_and_cascades_lines(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        list_id = el.id

        excess_service.delete_excess_list(db_session, list_id, owner)

        assert db_session.get(ExcessList, list_id) is None
        assert db_session.query(ExcessLineItem).filter_by(excess_list_id=list_id).count() == 0

    def test_non_owner_403(self, db_session, owner, outsider, company):
        el = _draft_with_lines(db_session, owner, company)
        with pytest.raises(HTTPException) as exc:
            excess_service.delete_excess_list(db_session, el.id, outsider)
        assert exc.value.status_code == 403
        assert db_session.get(ExcessList, el.id) is not None

    def test_posted_list_409(self, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        el.status = ExcessListStatus.COLLECTING
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            excess_service.delete_excess_list(db_session, el.id, owner)
        assert exc.value.status_code == 409
        assert db_session.get(ExcessList, el.id) is not None


# ═══════════════════════════════════════════════════════════════════════
#  Routes (thin — HTTP wiring over the guarded services)
# ═══════════════════════════════════════════════════════════════════════


def _as_owner(client, owner: User):
    from app.dependencies import require_user

    client.app.dependency_overrides[require_user] = lambda: owner
    return lambda: client.app.dependency_overrides.pop(require_user, None)


class TestDraftEditRoutes:
    def test_delete_line_route_200_and_rerenders_detail(self, client, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        line = _lines(db_session, el)[0]
        restore = _as_owner(client, owner)
        try:
            resp = client.delete(f"/api/resell/{el.id}/lines/{line.id}")
        finally:
            restore()
        assert resp.status_code == 200
        assert "data-resell-detail-root" in resp.text  # the detail panel re-rendered
        assert db_session.get(ExcessLineItem, line.id) is None

    def test_update_line_route_200(self, client, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        line = _lines(db_session, el)[0]
        restore = _as_owner(client, owner)
        try:
            resp = client.patch(
                f"/api/resell/{el.id}/lines/{line.id}",
                data={"part_number": "LM358N", "quantity": "500", "condition": "New"},
            )
        finally:
            restore()
        assert resp.status_code == 200
        db_session.refresh(line)
        assert line.quantity == 500

    def test_update_line_route_zero_quantity_400(self, client, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        line = _lines(db_session, el)[0]
        restore = _as_owner(client, owner)
        try:
            resp = client.patch(
                f"/api/resell/{el.id}/lines/{line.id}",
                data={"part_number": "LM358N", "quantity": "0"},
            )
        finally:
            restore()
        assert resp.status_code == 400

    def test_update_list_route_200(self, client, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        restore = _as_owner(client, owner)
        try:
            resp = client.patch(
                f"/api/resell/{el.id}",
                data={"title": "Edited title", "company_id": str(company.id), "notes": "n"},
            )
        finally:
            restore()
        assert resp.status_code == 200
        db_session.refresh(el)
        assert el.title == "Edited title"

    def test_delete_list_route_200_refreshes_left_and_toasts(self, client, db_session, owner, company):
        el = _draft_with_lines(db_session, owner, company)
        list_id = el.id
        restore = _as_owner(client, owner)
        try:
            resp = client.delete(f"/api/resell/{list_id}")
        finally:
            restore()
        assert resp.status_code == 200
        assert db_session.get(ExcessList, list_id) is None
        # The response resets the detail pane (OOB) and fires a toast.
        assert 'id="split-right-resell"' in resp.text
        assert "showToast" in resp.headers.get("HX-Trigger", "")

    def test_non_owner_delete_line_403(self, client, db_session, owner, company):
        """The default client user (a buyer, not the owner) cannot delete a draft
        line."""
        el = _draft_with_lines(db_session, owner, company)
        line = _lines(db_session, el)[0]
        resp = client.delete(f"/api/resell/{el.id}/lines/{line.id}")
        assert resp.status_code == 403
        assert db_session.get(ExcessLineItem, line.id) is not None


class TestHonest409Copy:
    def test_add_line_on_posted_uses_honest_copy(self, client, db_session, owner, company):
        """The false "revise as a new version" copy is replaced with actionable
        guidance."""
        el = _draft_with_lines(db_session, owner, company)
        el.status = ExcessListStatus.COLLECTING
        db_session.commit()
        restore = _as_owner(client, owner)
        try:
            resp = client.post(f"/api/resell/{el.id}/lines", data={"part_number": "X", "quantity": "1"})
        finally:
            restore()
        assert resp.status_code == 409
        detail = resp.json()["error"]
        assert "revise as a new version" not in detail
        assert "Close this list and create a new one" in detail
