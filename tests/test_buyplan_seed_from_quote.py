"""test_buyplan_seed_from_quote.py — Chunk B2: the quote→offer link as the buy-plan
default.

`build_buy_plan` now seeds each requirement's line from the offer the salesperson CHOSE on
the quote (`QuoteLine.offer_id`) instead of always re-scoring offers from scratch. The
re-score / auto-split path remains the graceful fallback for a stale/removed quote offer or
for unmet quantity (partial coverage). This mirrors the resell
`CustomerBidLine.selected_offer_id` provenance: the chosen inbound offer drives the plan,
overridable, degrading to the rollup when the choice is gone.

Covers:
- a quote whose lines carry chosen offers → buy-plan lines use THOSE offers, not the
  re-scored "best" pick (incl. a deliberately non-cheapest choice);
- a stale/removed quote offer (inactive or deleted) → falls back to re-score;
- partial coverage (chosen offer can't cover target_qty) → auto-split still fills the gap,
  with the chosen offer leading;
- no QuoteLine rows at all (legacy quote) → unchanged re-score behavior.

Called by: pytest
Depends on: conftest fixtures, app/services/buyplan_builder.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Offer, Quote, QuoteLine, Requirement, Requisition, User, VendorCard
from app.services.buyplan_builder import build_buy_plan

# ── Helpers (local; mirror test_buyplan_builder_extra conventions) ────────


def _make_user(db: Session, email="b2seed@trioscs.com") -> User:
    u = User(email=email, name="B2 Tester", role="buyer", azure_id=f"az-{email}", created_at=datetime.now(timezone.utc))
    db.add(u)
    db.flush()
    return u


def _make_site(db: Session, country=None) -> CustomerSite:
    company = Company(name="B2 Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(company)
    db.flush()
    s = CustomerSite(company_id=company.id, site_name="HQ", country=country, created_at=datetime.now(timezone.utc))
    db.add(s)
    db.flush()
    return s


def _make_requisition(db: Session, user: User, site: CustomerSite) -> Requisition:
    r = Requisition(
        name="REQ-B2", status="won", created_by=user.id, customer_site_id=site.id, created_at=datetime.now(timezone.utc)
    )
    db.add(r)
    db.flush()
    return r


def _make_requirement(db: Session, req: Requisition, mpn="B2-123", qty=100, price=1.0) -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=qty,
        target_price=price,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_vendor(db: Session, name="B2 Vendor") -> VendorCard:
    v = VendorCard(normalized_name=name.lower(), display_name=name, created_at=datetime.now(timezone.utc))
    db.add(v)
    db.flush()
    return v


def _make_offer(
    db: Session,
    req: Requisition,
    requirement: Requirement,
    vendor: VendorCard,
    *,
    qty=200,
    price=0.50,
    status="active",
) -> Offer:
    o = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_card_id=vendor.id,
        vendor_name=vendor.display_name,
        mpn=requirement.primary_mpn,
        qty_available=qty,
        unit_price=price,
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.add(o)
    db.flush()
    return o


def _make_quote(db: Session, req: Requisition, site: CustomerSite, user: User, status="won") -> Quote:
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-B2-001",
        status=status,
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    return q


def _add_quote_line(db: Session, quote: Quote, requirement: Requirement, offer: Offer | None, qty=100) -> QuoteLine:
    ql = QuoteLine(
        quote_id=quote.id,
        offer_id=offer.id if offer else None,
        mpn=requirement.primary_mpn,
        manufacturer=requirement.manufacturer,
        qty=qty,
        cost_price=float(offer.unit_price) if offer else None,
        sell_price=1.0,
        currency="USD",
    )
    db.add(ql)
    db.flush()
    return ql


# ── Seeding from the quote's chosen offer ─────────────────────────────────


class TestSeedFromQuoteOffer:
    def test_uses_chosen_offer_not_rescored_best(self, db_session):
        """The salesperson chose the PRICIER offer; the plan must honor it, not re-pick
        the cheapest."""
        user = _make_user(db_session)
        site = _make_site(db_session)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=100)
        v_cheap = _make_vendor(db_session, "Cheap Co")
        v_chosen = _make_vendor(db_session, "Chosen Co")
        # Cheapest offer the re-score would otherwise pick.
        _make_offer(db_session, req, requirement, v_cheap, qty=200, price=0.40)
        # The offer the salesperson actually quoted (more expensive, fully covers qty).
        chosen = _make_offer(db_session, req, requirement, v_chosen, qty=200, price=0.55)
        quote = _make_quote(db_session, req, site, user)
        _add_quote_line(db_session, quote, requirement, chosen, qty=100)

        plan = build_buy_plan(quote.id, db_session)

        assert len(plan.lines) == 1
        assert plan.lines[0].offer_id == chosen.id
        assert float(plan.lines[0].unit_cost) == 0.55

    def test_falls_back_to_rescore_when_chosen_offer_inactive(self, db_session):
        """A stale/removed quote offer (now inactive) → re-score picks the active
        best."""
        user = _make_user(db_session)
        site = _make_site(db_session)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=100)
        v_active = _make_vendor(db_session, "Still Active Co")
        v_gone = _make_vendor(db_session, "Gone Co")
        active = _make_offer(db_session, req, requirement, v_active, qty=200, price=0.45)
        gone = _make_offer(db_session, req, requirement, v_gone, qty=200, price=0.30, status="sold")
        quote = _make_quote(db_session, req, site, user)
        _add_quote_line(db_session, quote, requirement, gone, qty=100)

        plan = build_buy_plan(quote.id, db_session)

        assert len(plan.lines) == 1
        # Inactive chosen offer ignored → re-score picks the only active offer.
        assert plan.lines[0].offer_id == active.id

    def test_partial_coverage_chosen_offer_leads_then_autosplit(self, db_session):
        """Chosen offer can't cover the full qty → it leads, auto-split fills the
        gap."""
        user = _make_user(db_session)
        site = _make_site(db_session)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=150)
        v_chosen = _make_vendor(db_session, "Chosen Partial Co")
        v_filler = _make_vendor(db_session, "Filler Co")
        chosen = _make_offer(db_session, req, requirement, v_chosen, qty=80, price=0.50)
        filler = _make_offer(db_session, req, requirement, v_filler, qty=200, price=0.42)
        quote = _make_quote(db_session, req, site, user)
        _add_quote_line(db_session, quote, requirement, chosen, qty=150)

        plan = build_buy_plan(quote.id, db_session)

        offer_ids = [ln.offer_id for ln in plan.lines]
        # The chosen offer leads (first line) and the gap is filled.
        assert chosen.id in offer_ids
        assert plan.lines[0].offer_id == chosen.id
        assert filler.id in offer_ids
        assert sum(ln.quantity for ln in plan.lines) >= 150

    def test_legacy_quote_without_lines_rescore_unchanged(self, db_session):
        """A quote with NO QuoteLine rows (legacy) → behaves exactly like the old re-
        score."""
        user = _make_user(db_session)
        site = _make_site(db_session)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=100)
        v = _make_vendor(db_session, "Solo Co")
        offer = _make_offer(db_session, req, requirement, v, qty=200, price=0.40)
        quote = _make_quote(db_session, req, site, user)
        # No quote lines added.

        plan = build_buy_plan(quote.id, db_session)

        assert len(plan.lines) == 1
        assert plan.lines[0].offer_id == offer.id

    def test_quote_line_without_offer_id_rescores(self, db_session):
        """A QuoteLine with a NULL offer_id (manual price) → re-score for that
        requirement."""
        user = _make_user(db_session)
        site = _make_site(db_session)
        req = _make_requisition(db_session, user, site)
        requirement = _make_requirement(db_session, req, qty=100)
        v = _make_vendor(db_session, "Rescore Co")
        offer = _make_offer(db_session, req, requirement, v, qty=200, price=0.40)
        quote = _make_quote(db_session, req, site, user)
        _add_quote_line(db_session, quote, requirement, None, qty=100)

        plan = build_buy_plan(quote.id, db_session)

        assert len(plan.lines) == 1
        assert plan.lines[0].offer_id == offer.id
