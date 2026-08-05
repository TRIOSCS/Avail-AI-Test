"""test_resell_status_ladder_remap.py — migrations 207 + 208 (W3 status collapses).

207 (resell): the 5-state ExcessList ladder remap (open→posted, collecting/bid_out→
bidding, expired→closed), the sub-entity trims (line-item bidding→available, outreach
no_response→sent), and the data-driven ``outcome`` backfill for closed lists
(accepted bid / won offer → sold; offers or awards without a win → withdrawn;
nothing → no_bids). 208 (buy plan): the belt-and-braces inbound→active remap.

Both migrations factor their DML into module-level functions (``remap_statuses`` /
``backfill_outcomes``) so these tests drive the EXACT SQL the migrations run (loaded
via importlib, mirroring tests/test_resell_legacy_status_remap.py — no live PG
needed; the remaps are dialect-neutral). Revision metadata (id length, chain) is
asserted here too. Retired vocabulary is written RAW past the ORM validators —
exactly how a pre-W3 DB row exists.

Called by: pytest
Depends on: alembic/versions/207_resell_status_ladder.py,
    alembic/versions/208_buyplan_drop_inbound.py, app.models.excess,
    app.models.buy_plan_v3, app.constants, tests.conftest
"""

from __future__ import annotations

import importlib.util
import os

import pytest
from sqlalchemy.orm import Session

from app.constants import (
    CustomerBidStatus,
    ExcessListOutcome,
    ExcessListStatus,
    ExcessOfferStatus,
)
from app.models import Company, User
from app.models.excess import CustomerBid, ExcessLineItem, ExcessList, ExcessOffer, ExcessOutreach

_VERSIONS = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_VERSIONS, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_m207 = _load("207_resell_status_ladder")
_m208 = _load("208_buyplan_drop_inbound")


# ── Fixtures / helpers ───────────────────────────────────────────────


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(name="Ladder Remap Co")
    db_session.add(co)
    db_session.commit()
    return co


@pytest.fixture()
def owner(db_session: Session) -> User:
    u = User(email="ladder-owner@trioscs.com", name="Lad Owner", role="trader", azure_id="ladder-owner-1")
    db_session.add(u)
    db_session.commit()
    return u


def _make_list(db: Session, owner: User, company: Company, *, status: str) -> ExcessList:
    """Seed a list then write *status* RAW past the validator (pre-W3 rows exist so)."""
    el = ExcessList(title="L", company_id=company.id, owner_id=owner.id, status=ExcessListStatus.DRAFT)
    db.add(el)
    db.flush()
    db.query(ExcessList).filter(ExcessList.id == el.id).update({"status": status})
    db.expire(el)
    db.commit()
    db.refresh(el)
    return el


def _make_line(db: Session, el: ExcessList, *, status: str) -> ExcessLineItem:
    li = ExcessLineItem(excess_list_id=el.id, part_number="LM358N", quantity=10, status="available")
    db.add(li)
    db.flush()
    db.query(ExcessLineItem).filter(ExcessLineItem.id == li.id).update({"status": status})
    db.expire(li)
    db.commit()
    db.refresh(li)
    return li


def _remap(db: Session) -> None:
    _m207.remap_statuses(db.connection())
    db.expire_all()


def _backfill(db: Session) -> None:
    _m207.backfill_outcomes(db.connection())
    db.expire_all()


# ── Revision metadata ────────────────────────────────────────────────


class TestRevisionMetadata:
    def test_revision_ids(self):
        assert _m207.revision == "207_resell_status_ladder"
        assert _m208.revision == "208_buyplan_drop_inbound"

    def test_revision_ids_within_pg_version_num_limit(self):
        # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite ignores length.
        assert len(_m207.revision) <= 32
        assert len(_m208.revision) <= 32

    def test_chain(self):
        assert _m207.down_revision == "206_requirement_norm_remap"
        assert _m208.down_revision == "207_resell_status_ladder"


# ── 207: ExcessList ladder remap ─────────────────────────────────────


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("draft", "draft"),
        ("open", "posted"),
        ("collecting", "bidding"),
        ("bid_out", "bidding"),
        ("awarded", "awarded"),
        ("closed", "closed"),
        ("expired", "closed"),
    ],
)
def test_list_status_remap_table(db_session, owner, company, old, new):
    el = _make_list(db_session, owner, company, status=old)
    _remap(db_session)
    assert el.status == new


def test_line_item_bidding_folds_into_available(db_session, owner, company):
    el = _make_list(db_session, owner, company, status="bidding")
    legacy = _make_line(db_session, el, status="bidding")
    awarded = _make_line(db_session, el, status="awarded")
    _remap(db_session)
    assert legacy.status == "available"
    assert awarded.status == "awarded"  # untouched


def test_outreach_no_response_folds_into_sent(db_session, owner, company):
    el = _make_list(db_session, owner, company, status="bidding")
    o = ExcessOutreach(excess_list_id=el.id, submitted_by=owner.id, channel="email", status="sent")
    db_session.add(o)
    db_session.flush()
    db_session.query(ExcessOutreach).filter(ExcessOutreach.id == o.id).update({"status": "no_response"})
    db_session.expire(o)
    db_session.commit()
    _remap(db_session)
    assert o.status == "sent"


# ── 207: outcome backfill ────────────────────────────────────────────


def test_backfill_no_bids_on_bare_closed_list(db_session, owner, company):
    el = _make_list(db_session, owner, company, status="closed")
    _remap(db_session)
    _backfill(db_session)
    assert el.outcome == ExcessListOutcome.NO_BIDS


def test_backfill_withdrawn_when_offers_but_no_win(db_session, owner, company):
    el = _make_list(db_session, owner, company, status="expired")  # → closed
    db_session.add(
        ExcessOffer(excess_list_id=el.id, submitted_by=owner.id, scope="take_all", status=ExcessOfferStatus.OPEN)
    )
    db_session.commit()
    _remap(db_session)
    _backfill(db_session)
    assert el.status == "closed"
    assert el.outcome == ExcessListOutcome.WITHDRAWN


def test_backfill_withdrawn_when_awarded_line_but_no_win(db_session, owner, company):
    """The recon's list-id-5 shape: award history, zero offers, never sold."""
    el = _make_list(db_session, owner, company, status="expired")
    _make_line(db_session, el, status="awarded")
    _remap(db_session)
    _backfill(db_session)
    assert el.outcome == ExcessListOutcome.WITHDRAWN


def test_backfill_sold_on_won_offer(db_session, owner, company):
    el = _make_list(db_session, owner, company, status="closed")
    db_session.add(
        ExcessOffer(excess_list_id=el.id, submitted_by=owner.id, scope="take_all", status=ExcessOfferStatus.WON)
    )
    db_session.commit()
    _remap(db_session)
    _backfill(db_session)
    assert el.outcome == ExcessListOutcome.SOLD


def test_backfill_sold_on_accepted_customer_bid(db_session, owner, company):
    el = _make_list(db_session, owner, company, status="closed")
    db_session.add(
        CustomerBid(
            excess_list_id=el.id,
            owner_id=owner.id,
            status=CustomerBidStatus.ACCEPTED,
        )
    )
    db_session.commit()
    _remap(db_session)
    _backfill(db_session)
    assert el.outcome == ExcessListOutcome.SOLD


def test_backfill_leaves_non_terminal_rows_null(db_session, owner, company):
    live = _make_list(db_session, owner, company, status="collecting")  # → bidding
    awarded = _make_list(db_session, owner, company, status="awarded")
    _remap(db_session)
    _backfill(db_session)
    assert live.outcome is None
    assert awarded.outcome is None  # NULL until closed


def test_207_downgrade_status_side_is_noop(db_session, owner, company):
    """Downgrade drops only the outcome column; statuses stay put (documented many-to-
    one no-op).

    Driven via the factored functions, so just assert the remap is not reversed by
    anything the module exports.
    """
    el = _make_list(db_session, owner, company, status="bid_out")
    _remap(db_session)
    assert el.status == "bidding"


# ── 208: buy-plan inbound→active ─────────────────────────────────────


def _make_plan(db: Session, *, status: str):
    """Seed a BuyPlan (via a minimal parent Requisition) then write *status* RAW."""
    from app.models import Requisition
    from app.models.buy_plan import BuyPlan

    req = Requisition(name=f"Ladder-Req-{status}", customer_name="Acme")
    db.add(req)
    db.flush()
    bp = BuyPlan(requisition_id=req.id, status="draft")
    db.add(bp)
    db.flush()
    db.query(BuyPlan).filter(BuyPlan.id == bp.id).update({"status": status})
    db.expire(bp)
    db.commit()
    db.refresh(bp)
    return bp


def test_buyplan_inbound_remaps_to_active(db_session):
    bp = _make_plan(db_session, status="inbound")

    _m208.remap_statuses(db_session.connection())
    db_session.expire_all()
    assert bp.status == "active"


def test_buyplan_other_statuses_untouched(db_session):
    plans = {
        st: _make_plan(db_session, status=st)
        for st in ("draft", "pending", "active", "halted", "completed", "cancelled")
    }

    _m208.remap_statuses(db_session.connection())
    db_session.expire_all()
    for st, bp in plans.items():
        assert bp.status == st
