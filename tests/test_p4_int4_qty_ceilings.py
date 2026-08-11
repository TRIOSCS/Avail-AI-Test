"""test_p4_int4_qty_ceilings.py — quantity inputs are bounded to PG INT4.

Every quantity that reaches a PostgreSQL INTEGER column must be rejected with a
clean 400/validation error before the insert, not blow up as an unhandled
psycopg DataError 500. This covers the three intake layers the 2026-08-08 QC
audit flagged as unbounded: the requisition/requirement schema, the buy-plan
line quantity coercer, and the resell add/update-line service paths.

Called by: pytest
Depends on: app.constants.PG_INT4_MAX, app.schemas.requisitions,
    app.services.buyplan_workflow.buyplan_lines, app.services.excess_service
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.constants import PG_INT4_MAX
from app.models import Company, User
from app.services.excess_service import add_line, create_excess_list, update_line
from tests.conftest import engine

_ = engine  # ensure test DB tables exist

OVER = PG_INT4_MAX + 1


# ── Requisition/requirement schema ────────────────────────────────────────


def test_requirement_create_rejects_over_int4_qty():
    from app.schemas.requisitions import RequirementCreate

    with pytest.raises(ValidationError):
        RequirementCreate(primary_mpn="LM317T", manufacturer="TI", target_qty=OVER)
    # boundary value is still accepted
    ok = RequirementCreate(primary_mpn="LM317T", manufacturer="TI", target_qty=PG_INT4_MAX)
    assert ok.target_qty == PG_INT4_MAX


def test_requirement_update_rejects_over_int4_qty():
    from app.schemas.requisitions import RequirementUpdate

    with pytest.raises(ValidationError):
        RequirementUpdate(target_qty=OVER)
    assert RequirementUpdate(target_qty=PG_INT4_MAX).target_qty == PG_INT4_MAX


# ── Buy-plan line quantity coercer ────────────────────────────────────────


def test_buyplan_require_int_quantity_rejects_over_int4():
    from app.services.buyplan_workflow.buyplan_lines import _require_int_quantity

    assert _require_int_quantity(PG_INT4_MAX) == PG_INT4_MAX  # boundary ok
    with pytest.raises(ValueError):
        _require_int_quantity(OVER)
    with pytest.raises(ValueError):
        _require_int_quantity(float(OVER))  # float form also rejected pre-insert


# ── Resell add/update line service paths ──────────────────────────────────


def _draft(db):
    co = Company(name="Seller Corp")
    db.add(co)
    db.commit()
    user = User(email="t@x.com", name="T", role="trader", azure_id="az-t")
    db.add(user)
    db.commit()
    el = create_excess_list(db, title="Excess", company_id=co.id, owner_id=user.id)
    return el, user


def test_resell_add_line_rejects_over_int4_qty(db_session):
    el, user = _draft(db_session)
    with pytest.raises(HTTPException) as exc:
        add_line(db_session, el.id, user, part_number="LM317T", quantity=OVER)
    assert exc.value.status_code == 400


def test_resell_update_line_rejects_over_int4_qty(db_session):
    from app.models.excess import ExcessLineItem

    el, user = _draft(db_session)
    add_line(db_session, el.id, user, part_number="LM317T", quantity=100)
    line = db_session.query(ExcessLineItem).filter(ExcessLineItem.excess_list_id == el.id).first()
    with pytest.raises(HTTPException) as exc:
        update_line(db_session, el.id, line.id, user, part_number="LM317T", quantity=OVER)
    assert exc.value.status_code == 400
