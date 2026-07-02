"""PERF-2 — get_matches_for_user must not N+1 on ProactiveMatch.requirement, and the do-
not-offer suppression must still work after scoping that lookup to the match set.

Regression (2026-07-02 production-polish audit): the per-match loop reads
m.requirement.target_qty but requirement was not eager-loaded → one lazy SELECT per
match; and the ProactiveDoNotOffer table (which only grows) was loaded in full every call.

Called by: pytest
Depends on: app.services.proactive_service.get_matches_for_user.
"""

from decimal import Decimal

from sqlalchemy import event

from app.models import User
from app.models.crm import Company, CustomerSite
from app.models.intelligence import MaterialCard, ProactiveDoNotOffer, ProactiveMatch
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.services.proactive_service import get_matches_for_user


def _seed_match(db, owner, company, site, *, mpn: str) -> ProactiveMatch:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn)
    db.add(card)
    db.flush()
    req = Requisition(name=f"R-{mpn}", customer_site_id=site.id, status="archived", created_by=owner.id)
    db.add(req)
    db.flush()
    requirement = Requirement(
        requisition_id=req.id, primary_mpn=mpn, normalized_mpn=mpn.lower(), material_card_id=card.id, target_qty=1000
    )
    db.add(requirement)
    db.flush()
    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        material_card_id=card.id,
        vendor_name="Arrow",
        mpn=mpn,
        unit_price=Decimal("0.42"),
        qty_available=5000,
        status="active",
    )
    db.add(offer)
    db.flush()
    match = ProactiveMatch(
        offer_id=offer.id,
        requirement_id=requirement.id,
        requisition_id=req.id,
        customer_site_id=site.id,
        salesperson_id=owner.id,
        mpn=mpn,
        material_card_id=card.id,
        company_id=company.id,
        match_score=85,
        margin_pct=23.0,
        our_cost=0.42,
        status="new",
    )
    db.add(match)
    db.commit()
    return match


def _base(db) -> tuple[User, Company, CustomerSite]:
    owner = User(email="rep@trioscs.com", name="Rep", role="sales")
    db.add(owner)
    db.flush()
    company = Company(name="Acme")
    db.add(company)
    db.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()
    return owner, company, site


class _QueryCounter:
    def __init__(self, db):
        self.engine = db.get_bind()
        self.count = 0

    def _on_exec(self, *a, **k):
        self.count += 1

    def __enter__(self):
        event.listen(self.engine, "after_cursor_execute", self._on_exec)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "after_cursor_execute", self._on_exec)


def test_matches_query_count_independent_of_match_count(db_session):
    """Query count must not grow with the number of matches (no requirement N+1)."""
    owner, company, site = _base(db_session)
    _seed_match(db_session, owner, company, site, mpn="LM358N")

    with _QueryCounter(db_session) as c1:
        get_matches_for_user(db_session, owner.id, status="new")
    one = c1.count

    for mpn in ("SN74LS", "NE555P", "TL072CP"):
        _seed_match(db_session, owner, company, site, mpn=mpn)

    with _QueryCounter(db_session) as c4:
        get_matches_for_user(db_session, owner.id, status="new")
    four = c4.count

    # 4 matches must cost the same number of queries as 1 — a lazy requirement load
    # would add one SELECT per extra match (would be one+3).
    assert four == one, f"N+1 on requirement: 1 match={one} queries, 4 matches={four}"


def test_do_not_offer_still_suppresses_after_scoping(db_session):
    """The scoped do-not-offer query must still suppress a matched (mpn, company)."""
    owner, company, site = _base(db_session)
    _seed_match(db_session, owner, company, site, mpn="LM358N")
    _seed_match(db_session, owner, company, site, mpn="NE555P")

    db_session.add(ProactiveDoNotOffer(mpn="LM358N", company_id=company.id, created_by_id=owner.id))
    db_session.commit()

    result = get_matches_for_user(db_session, owner.id, status="new")
    shown = {m["mpn"] for g in result["groups"] for m in g["matches"]}
    assert "LM358N" not in shown, "do-not-offer match was not suppressed"
    assert "NE555P" in shown
