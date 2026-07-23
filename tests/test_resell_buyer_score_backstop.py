"""test_resell_buyer_score_backstop.py — nightly BuyerScore reconcile job (#17 core).

Covers Phase-5 Task 4: the nightly backstop that recomputes every buyer's scorecard so a
BuyerScore row can never silently drift from truth when an on-win / on-send hook is missed.

- ``recompute_all_buyer_scores`` reconciles a stale BuyerScore to ground truth and returns
  the walked-card count (RESELL-TEST-4 drift).
- ``_job_recompute_buyer_scores`` mirrors the expiry job: success path plus the
  SQLAlchemyError and generic-Exception branches roll back and never crash the scheduler.
- ``register_resell_jobs`` registers the 3rd cron job at a distinct minute.

Called by: pytest
Depends on: app.jobs.resell_jobs, app.services.buyer_affinity_service, app.models, conftest
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import sqlalchemy.exc
from sqlalchemy.orm import Session

from app.constants import ExcessOfferStatus, OfferLineMatchStatus
from app.models import Company, User, VendorCard
from app.models.excess import BuyerScore, ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine
from app.models.intelligence import MaterialCard
from app.services import buyer_affinity_service as svc
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


# ── Helpers ──────────────────────────────────────────────────────────


def _buyer(db: Session, name: str) -> VendorCard:
    vc = VendorCard(normalized_name=name.lower(), display_name=name, emails=[f"sales@{name.lower()}.com"])
    db.add(vc)
    db.flush()
    return vc


def _list_with_cap_line(db: Session) -> tuple[ExcessList, ExcessLineItem, User]:
    owner = User(email="bs-owner@trioscs.com", name="BS Owner", role="trader", azure_id="bs-owner-1")
    company = Company(name="BS Seller")
    db.add_all([owner, company])
    db.flush()
    el = ExcessList(company_id=company.id, owner_id=owner.id, title="BS Excess")
    db.add(el)
    db.flush()
    mc = MaterialCard(normalized_mpn="grm188r", display_mpn="GRM188R", category="capacitors")
    db.add(mc)
    db.flush()
    line = ExcessLineItem(
        excess_list_id=el.id,
        part_number="GRM188R",
        quantity=1000,
        material_card_id=mc.id,
        asking_price=Decimal("1.00"),
    )
    db.add(line)
    db.flush()
    return el, line, owner


def _won_offer(db: Session, el: ExcessList, line: ExcessLineItem, buyer: VendorCard, owner: User, price: str) -> None:
    offer = ExcessOffer(
        excess_list_id=el.id,
        submitted_by=owner.id,
        offerer_vendor_card_id=buyer.id,
        scope="per_line",
        status=ExcessOfferStatus.WON,
    )
    db.add(offer)
    db.flush()
    db.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw=line.part_number,
            quantity=10,
            unit_price=Decimal(price),
            match_status=OfferLineMatchStatus.MATCHED,
        )
    )
    db.flush()


# ── (a) RESELL-TEST-4: the backstop reconciles a stale score ─────────


def test_backstop_reconciles_stale_buyer_score(db_session: Session):
    """A BuyerScore staled by a missed on-win hook is reconciled to truth by the nightly
    backstop, which returns the count of walked buyer cards."""
    el, line, owner = _list_with_cap_line(db_session)
    buyer = _buyer(db_session, "DriftBuyer")
    _won_offer(db_session, el, line, buyer, owner, "0.80")
    db_session.commit()

    # First compute — the score reflects one won offer.
    svc.recompute_buyer_score(db_session, buyer.id)
    db_session.commit()
    score = db_session.query(BuyerScore).filter_by(vendor_card_id=buyer.id).one()
    assert score.offers_received == 1
    assert score.wins == 1

    # Stale it: a SECOND won offer lands but the on-win hook is missed (score not updated).
    _won_offer(db_session, el, line, buyer, owner, "0.90")
    db_session.commit()
    assert db_session.query(BuyerScore).filter_by(vendor_card_id=buyer.id).one().wins == 1  # still stale

    walked = svc.recompute_all_buyer_scores(db_session)

    assert walked >= 1
    db_session.refresh(score)
    assert score.offers_received == 2  # reconciled to truth
    assert score.wins == 2


def test_backstop_returns_zero_with_no_buyers(db_session: Session):
    """No offers/outreach anywhere → the backstop walks zero cards and returns 0."""
    assert svc.recompute_all_buyer_scores(db_session) == 0


def test_backstop_one_poisoned_buyer_does_not_strand_the_others(db_session: Session):
    """Finding B44: recompute_buyer_score has no per-buyer error isolation — one buyer
    raising must roll back ONLY that buyer and let the batch continue reconciling the
    rest, mirroring excess_service.expire_overdue_lists's per-list isolation."""
    el, line, owner = _list_with_cap_line(db_session)
    good_buyer_a = _buyer(db_session, "GoodBuyerA")
    poisoned_buyer = _buyer(db_session, "PoisonedBuyer")
    good_buyer_b = _buyer(db_session, "GoodBuyerB")
    _won_offer(db_session, el, line, good_buyer_a, owner, "0.80")
    _won_offer(db_session, el, line, poisoned_buyer, owner, "0.85")
    _won_offer(db_session, el, line, good_buyer_b, owner, "0.90")
    db_session.commit()

    real_recompute = svc.recompute_buyer_score

    def _boom_on_poisoned(db, vendor_card_id):
        if vendor_card_id == poisoned_buyer.id:
            raise RuntimeError("simulated per-buyer failure")
        return real_recompute(db, vendor_card_id)

    with patch("app.services.buyer_affinity_service.recompute_buyer_score", side_effect=_boom_on_poisoned):
        walked = svc.recompute_all_buyer_scores(db_session)

    assert walked == 2  # the two good buyers, NOT the poisoned one
    db_session.expire_all()
    assert db_session.query(BuyerScore).filter_by(vendor_card_id=good_buyer_a.id).one().wins == 1
    assert db_session.query(BuyerScore).filter_by(vendor_card_id=good_buyer_b.id).one().wins == 1
    assert db_session.query(BuyerScore).filter_by(vendor_card_id=poisoned_buyer.id).first() is None


# ── (b) nightly job wrapper — success + both rollback branches ───────


async def test_job_recomputes_buyer_scores(db_session: Session):
    """The job runs recompute_all_buyer_scores against a fresh (patched) session."""
    el, line, owner = _list_with_cap_line(db_session)
    buyer = _buyer(db_session, "JobBuyer")
    _won_offer(db_session, el, line, buyer, owner, "0.80")
    db_session.commit()
    buyer_id = buyer.id

    from app.jobs.resell_jobs import _job_recompute_buyer_scores

    with patch("app.database.SessionLocal", return_value=db_session):
        await _job_recompute_buyer_scores()

    # The commit is visible on the shared test connection; a BuyerScore now exists.
    assert db_session.query(BuyerScore).filter_by(vendor_card_id=buyer_id).count() == 1


async def test_job_sqlalchemy_error_rolls_back(db_session: Session):
    """A SQLAlchemyError from the service is caught, rolled back, and never crashes the
    scheduler."""
    from app.jobs.resell_jobs import _job_recompute_buyer_scores

    with (
        patch.object(db_session, "rollback") as mock_rollback,
        patch("app.database.SessionLocal", return_value=db_session),
        patch(
            "app.services.buyer_affinity_service.recompute_all_buyer_scores",
            side_effect=sqlalchemy.exc.SQLAlchemyError("boom"),
        ),
    ):
        await _job_recompute_buyer_scores()  # must NOT raise

    mock_rollback.assert_called_once()


async def test_job_generic_exception_rolls_back(db_session: Session):
    """A non-DB Exception from the service is caught, rolled back, and never crashes the
    scheduler."""
    from app.jobs.resell_jobs import _job_recompute_buyer_scores

    with (
        patch.object(db_session, "rollback") as mock_rollback,
        patch("app.database.SessionLocal", return_value=db_session),
        patch(
            "app.services.buyer_affinity_service.recompute_all_buyer_scores",
            side_effect=RuntimeError("kaboom"),
        ),
    ):
        await _job_recompute_buyer_scores()  # must NOT raise

    mock_rollback.assert_called_once()


def test_register_resell_jobs_adds_buyer_score_job():
    """register_resell_jobs registers the recompute_buyer_scores cron job at a distinct
    minute so the three nightly jobs do not collide."""
    from app.jobs.resell_jobs import register_resell_jobs

    scheduler = MagicMock()
    register_resell_jobs(scheduler, settings=None)
    ids = {c.kwargs.get("id") for c in scheduler.add_job.call_args_list}
    assert "recompute_buyer_scores" in ids
    # All three nightly jobs registered.
    assert {"expire_resell_lists", "sweep_stale_sending_outreach", "recompute_buyer_scores"} <= ids
