"""test_pm_active_unique_index.py — DB-level one-active-match-per-(mpn, company) pin.

Migration 211 / QC pa-match-no-unique-index: the Python dedup in
_existing_match_company_ids can be defeated by an overlapping scheduler scan +
manual Refresh; ``uq_pm_active_mpn_company`` (partial unique on (mpn, company_id)
WHERE status IN ('new','sent')) is the DB backstop. SQLite honors sqlite_where on
create_all, so the constraint is assertable in the main suite; the scan writers'
existing commit try/rollback turns a race-loser violation into a logged yield.

Called by: pytest
Depends on: conftest (db_session), app.models.intelligence.ProactiveMatch
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import ProactiveMatchStatus
from app.models import Company, Offer, Requisition
from app.models.intelligence import ProactiveMatch


def _mk_offer(db: Session) -> Offer:
    req = Requisition(name="PM-UQ req", customer_name="Acme", status="open")
    db.add(req)
    db.flush()
    offer = Offer(requisition_id=req.id, mpn="LM317T", vendor_name="Arrow")
    db.add(offer)
    db.flush()
    return offer


def _mk_match(db: Session, offer: Offer, company_id, status) -> ProactiveMatch:
    m = ProactiveMatch(
        offer_id=offer.id,
        mpn="lm317t",
        company_id=company_id,
        status=status,
    )
    db.add(m)
    return m


class TestActiveUniqueIndex:
    def test_duplicate_active_same_company_rejected(self, db_session: Session):
        offer = _mk_offer(db_session)
        co = Company(name="PM UQ Co", is_active=True)
        db_session.add(co)
        db_session.flush()

        _mk_match(db_session, offer, co.id, ProactiveMatchStatus.NEW)
        db_session.flush()
        _mk_match(db_session, offer, co.id, ProactiveMatchStatus.SENT)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_terminal_status_row_does_not_block_new_active(self, db_session: Session):
        offer = _mk_offer(db_session)
        co = Company(name="PM UQ Co 2", is_active=True)
        db_session.add(co)
        db_session.flush()

        _mk_match(db_session, offer, co.id, ProactiveMatchStatus.DISMISSED)
        db_session.flush()
        _mk_match(db_session, offer, co.id, ProactiveMatchStatus.NEW)
        db_session.flush()  # no IntegrityError — dismissed row is outside the predicate

        active = db_session.query(ProactiveMatch).filter_by(company_id=co.id, status=ProactiveMatchStatus.NEW).count()
        assert active == 1

    def test_null_company_backorder_rows_unconstrained(self, db_session: Session):
        offer = _mk_offer(db_session)
        _mk_match(db_session, offer, None, ProactiveMatchStatus.NEW)
        _mk_match(db_session, offer, None, ProactiveMatchStatus.NEW)
        db_session.flush()  # NULLs are distinct — back-order rows dedup per owner in Python

        rows = db_session.query(ProactiveMatch).filter(ProactiveMatch.company_id.is_(None)).count()
        assert rows == 2
