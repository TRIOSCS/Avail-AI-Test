"""tests/test_remediation_p2c.py — QC 2026-08-10 P2 (resell dead-ends, D4/D4b).

- D4b: withdraw_line is the missing WITHDRAWN writer — a line that won't sell can
  be taken off the table, letting a partially-sold list finally resolve.
- D4: close_bid_out_list gives a BID_OUT list (e.g. after a rejected customer bid)
  a terminal exit; before, it stranded with no way to close.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session, test_user).
"""

from decimal import Decimal

import pytest

from tests.conftest import engine  # noqa: F401


def _owner(db, email="resell-owner@trioscs.com", role="trader"):
    from app.models import User

    u = User(email=email, name="Owner", role=role, azure_id="az-" + email[:8], is_active=True)
    db.add(u)
    db.flush()
    return u


def _list_with_line(db, owner):
    from app.models import Company
    from app.models.excess import ExcessLineItem
    from app.services.excess_service import create_excess_list

    company = Company(name="ExcessCo", is_active=True)
    db.add(company)
    db.flush()
    el = create_excess_list(db, title="Resolve me", company_id=company.id, owner_id=owner.id)
    line = ExcessLineItem(excess_list_id=el.id, part_number="GRM188R", quantity=500, asking_price=Decimal("1.00"))
    db.add(line)
    db.commit()
    return el, line


def test_owner_withdraws_a_line(db_session):
    from app.constants import ExcessLineItemStatus
    from app.services.excess_service import withdraw_line

    owner = _owner(db_session)
    el, line = _list_with_line(db_session, owner)
    withdraw_line(db_session, line.id, owner)
    db_session.refresh(line)
    assert line.status == ExcessLineItemStatus.WITHDRAWN  # the writer that never existed


def test_manager_can_withdraw_a_line(db_session):
    from app.constants import ExcessLineItemStatus
    from app.services.excess_service import withdraw_line

    owner = _owner(db_session)
    mgr = _owner(db_session, email="resell-mgr@trioscs.com", role="manager")
    el, line = _list_with_line(db_session, owner)
    withdraw_line(db_session, line.id, mgr)  # owner + manager/admin, per the owner's rule
    db_session.refresh(line)
    assert line.status == ExcessLineItemStatus.WITHDRAWN


def test_non_owner_non_manager_cannot_withdraw(db_session):
    from fastapi import HTTPException

    from app.services.excess_service import withdraw_line

    owner = _owner(db_session)
    rando = _owner(db_session, email="rando-trader@trioscs.com", role="trader")
    el, line = _list_with_line(db_session, owner)
    with pytest.raises(HTTPException) as exc:
        withdraw_line(db_session, line.id, rando)
    assert exc.value.status_code == 403


def test_close_bid_out_list_terminal(db_session):
    from app.constants import ExcessListStatus
    from app.services.excess_service import close_bid_out_list

    owner = _owner(db_session)
    el, _line = _list_with_line(db_session, owner)
    el.status = ExcessListStatus.BID_OUT.value  # simulate bids sent, customer rejected
    db_session.commit()
    close_bid_out_list(db_session, el.id, owner)
    db_session.refresh(el)
    assert el.status == ExcessListStatus.CLOSED.value  # terminal exit, no longer stranded


def test_cannot_close_non_bid_out_list_from_here(db_session):
    from fastapi import HTTPException

    from app.constants import ExcessListStatus
    from app.services.excess_service import close_bid_out_list

    owner = _owner(db_session)
    el, _line = _list_with_line(db_session, owner)
    el.status = ExcessListStatus.OPEN.value
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        close_bid_out_list(db_session, el.id, owner)
    assert exc.value.status_code == 409  # open/collecting close through the posting-window path
