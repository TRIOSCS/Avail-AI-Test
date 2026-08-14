"""Part-level hotlist parts seed Proactive matches as standing monitors.

A HOTLIST *part* (Requirement.sourcing_status, migration 210) is the per-part
"customer uses this but doesn't need it now — keep watching" state. The
matcher's part-hotlist pass has NO time window and NO requisition-status
filter (a hotlist part on a won/lost deal is the core case), carries REAL
demand signals (requirement_id, last asked date/qty), scores computed-floored-
at-60, and dedups against the requirement and requisition-hotlist passes.
Flipping a part to hotlist also retro-matches against existing live supply.

Called by: pytest.
Depends on: app.services.proactive_matching, app.services.requirement_status,
            models, conftest db_session fixture.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.constants import RequisitionStatus, SourcingStatus
from app.models import (
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    ProactiveMatch,
    Requirement,
    Requisition,
    User,
)
from app.models.intelligence import ProactiveDoNotOffer
from app.services.proactive_matching import find_matches_for_offer
from app.services.requirement_status import transition_requirement
from tests.conftest import engine  # noqa: F401


def _setup(
    db,
    *,
    mpn="PH-100",
    req_status=RequisitionStatus.OPEN.value,
    part_status=SourcingStatus.HOTLIST.value,
    ask_age_days=10,
    account_owner=True,
):
    """Company + active site + requisition (any status) + hotlist part + live offer."""
    owner = User(
        email=f"owner-{mpn}@trioscs.com",
        name="Account Owner",
        role="sales",
        azure_id=f"owner-{mpn}",
        created_at=datetime.now(UTC),
    )
    db.add(owner)
    db.flush()

    card = MaterialCard(normalized_mpn=mpn.lower().replace("-", ""), display_mpn=mpn, search_count=1)
    db.add(card)
    db.flush()

    co = Company(name=f"Acme {mpn}", is_active=True, account_owner_id=owner.id if account_owner else None)
    db.add(co)
    db.flush()

    site = CustomerSite(company_id=co.id, site_name="Acme HQ", is_active=True)
    db.add(site)
    db.flush()

    req = Requisition(
        name=f"deal {mpn}",
        status=req_status,
        customer_site_id=site.id,
        company_id=co.id,
        created_by=owner.id,
    )
    db.add(req)
    db.flush()

    part = Requirement(
        requisition_id=req.id,
        material_card_id=card.id,
        primary_mpn=mpn,
        target_qty=500,
        sourcing_status=part_status,
        created_at=datetime.now(UTC) - timedelta(days=ask_age_days),
    )
    db.add(part)
    offer = Offer(
        material_card_id=card.id,
        vendor_name="Arrow",
        mpn=mpn,
        unit_price=Decimal("10"),
        qty_available=1000,
        status="active",
    )
    db.add(offer)
    db.commit()
    return {"owner": owner, "company": co, "site": site, "req": req, "part": part, "offer": offer, "card": card}


def test_part_hotlist_seeds_match_with_real_signals(db_session):
    """A hotlist part seeds a match carrying requirement_id + ask history."""
    db = db_session
    d = _setup(db, mpn="PH-SIG")
    matches = find_matches_for_offer(d["offer"].id, db)
    db.commit()

    rows = db.query(ProactiveMatch).filter_by(company_id=d["company"].id).all()
    assert len(rows) == 1
    m = rows[0]
    assert m.match_source == "hotlist"
    assert m.requirement_id == d["part"].id  # part-level: real requirement link
    assert m.last_asked_at is not None
    assert m.last_asked_qty == 500
    assert m.requirement_count == 1
    assert m.match_score >= 60  # computed, floored at the hotlist baseline
    assert m.customer_site_id == d["site"].id
    assert m.salesperson_id == d["owner"].id
    assert any(x.company_id == d["company"].id for x in matches)


def test_part_hotlist_is_a_standing_monitor_beyond_window(db_session):
    """An ask older than the 24-month requirement window still seeds — no window."""
    db = db_session
    d = _setup(db, mpn="PH-OLD", ask_age_days=30 * 30)  # ~30 months
    find_matches_for_offer(d["offer"].id, db)
    db.commit()

    m = db.query(ProactiveMatch).filter_by(company_id=d["company"].id).first()
    assert m is not None
    assert m.match_source == "hotlist"
    assert m.requirement_id == d["part"].id


def test_part_hotlist_on_won_requisition_seeds(db_session):
    """The core case: the deal closed WON but the hotlist part keeps matching."""
    db = db_session
    d = _setup(db, mpn="PH-WON", req_status=RequisitionStatus.WON.value)
    find_matches_for_offer(d["offer"].id, db)
    db.commit()

    m = db.query(ProactiveMatch).filter_by(company_id=d["company"].id).first()
    assert m is not None
    assert m.match_source == "hotlist"


def test_requirement_pass_no_longer_seeds_hotlist_parts(db_session):
    """A company whose ONLY in-window ask is hotlisted gets exactly ONE match, owned by
    the hotlist pass (the requirement pass excludes hotlist parts)."""
    db = db_session
    d = _setup(db, mpn="PH-OWN", ask_age_days=5)  # well inside the window
    find_matches_for_offer(d["offer"].id, db)
    db.commit()

    rows = db.query(ProactiveMatch).filter_by(company_id=d["company"].id).all()
    assert len(rows) == 1
    assert rows[0].match_source == "hotlist"


def test_null_status_part_still_seeds_requirement_pass(db_session):
    """Regression: the hotlist exclusion in the requirement pass is NULL-safe —
    a legacy NULL-status part still seeds an ordinary requirement match."""
    db = db_session
    d = _setup(db, mpn="PH-NULL", part_status=None)
    find_matches_for_offer(d["offer"].id, db)
    db.commit()

    rows = db.query(ProactiveMatch).filter_by(company_id=d["company"].id).all()
    assert len(rows) == 1
    assert rows[0].match_source == "requirement"


def test_cross_pass_dedup_one_match_per_company(db_session):
    """A company with an in-window OPEN ask AND a hotlist part gets ONE match (the
    requirement pass wins the slot)."""
    db = db_session
    d = _setup(db, mpn="PH-DUP")
    # Second, ordinary open ask for the same part by the same company.
    db.add(
        Requirement(
            requisition_id=d["req"].id,
            material_card_id=d["card"].id,
            primary_mpn="PH-DUP",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN.value,
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
    )
    db.commit()

    find_matches_for_offer(d["offer"].id, db)
    db.commit()

    rows = db.query(ProactiveMatch).filter_by(company_id=d["company"].id).all()
    assert len(rows) == 1
    assert rows[0].match_source == "requirement"


def test_scratch_requisitions_excluded(db_session):
    """Hotlist parts on scratch requisitions never seed."""
    db = db_session
    d = _setup(db, mpn="PH-SCR")
    d["req"].is_scratch = True
    db.commit()

    find_matches_for_offer(d["offer"].id, db)
    db.commit()
    assert db.query(ProactiveMatch).filter_by(company_id=d["company"].id).count() == 0


def test_do_not_offer_suppresses(db_session):
    """A DNO row for (mpn, company) blocks the part-hotlist match."""
    db = db_session
    d = _setup(db, mpn="PH-DNO")
    db.add(ProactiveDoNotOffer(mpn="PH-DNO", company_id=d["company"].id, created_by_id=d["owner"].id))
    db.commit()

    find_matches_for_offer(d["offer"].id, db)
    db.commit()
    assert db.query(ProactiveMatch).filter_by(company_id=d["company"].id).count() == 0


def test_salesperson_falls_back_to_requisition_owner(db_session):
    """No account owner on the company → the requisition creator gets the match."""
    db = db_session
    d = _setup(db, mpn="PH-FBK", account_owner=False)
    find_matches_for_offer(d["offer"].id, db)
    db.commit()

    m = db.query(ProactiveMatch).filter_by(company_id=d["company"].id).first()
    assert m is not None
    assert m.salesperson_id == d["owner"].id  # created_by fallback


def test_retro_match_on_hotlist_transition(db_session):
    """Flipping a part to hotlist with EXISTING live supply seeds immediately (no
    waiting for the next new offer)."""
    db = db_session
    d = _setup(db, mpn="PH-RETRO", part_status=SourcingStatus.OPEN.value)

    changed = transition_requirement(d["part"], SourcingStatus.HOTLIST, db, actor=d["owner"])
    assert changed is True
    db.commit()

    m = db.query(ProactiveMatch).filter_by(company_id=d["company"].id).first()
    assert m is not None
    assert m.match_source == "hotlist"
    assert m.requirement_id == d["part"].id


def test_retro_match_noop_without_live_supply(db_session):
    """Flipping to hotlist with no live in-window offer creates nothing and never blocks
    the status change."""
    db = db_session
    d = _setup(db, mpn="PH-QUIET", part_status=SourcingStatus.OPEN.value)
    d["offer"].status = "expired"
    db.commit()

    changed = transition_requirement(d["part"], SourcingStatus.HOTLIST, db, actor=d["owner"])
    assert changed is True
    db.commit()

    assert d["part"].sourcing_status == SourcingStatus.HOTLIST
    assert db.query(ProactiveMatch).filter_by(company_id=d["company"].id).count() == 0
