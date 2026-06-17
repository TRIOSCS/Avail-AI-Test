"""Tests for the buy-plan → customer_part_history auto-feed.

Called by: pytest. Depends on: buyplan_workflow, purchase_history_service.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from app.models import Company, CustomerSite, MaterialCard, Offer, Quote, Requirement, Requisition, User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.intelligence import ProactiveMatch
from app.models.purchase_history import CustomerPartHistory

# ── Task 5: retired offer/quote-won CPH hooks ────────────────────────


def test_offer_won_does_not_write_cph():
    """Legacy avail_offer CPH hook is retired — only buy_plan feeds CPH now."""
    import app.routers.crm.offers as offers_mod

    assert not hasattr(offers_mod, "_record_offer_won_history")


def test_quote_won_does_not_write_cph():
    """Legacy avail_quote_won CPH hook is retired — only buy_plan feeds CPH now."""
    import app.routers.crm.quotes as quotes_mod

    assert not hasattr(quotes_mod, "_record_quote_won_history")


def test_buyplan_has_recorded_at_column():
    bp = BuyPlan(quote_id=1, requisition_id=1)
    assert bp.purchase_history_recorded_at is None


# ── Task 2: record_buyplan_purchase_history() ────────────────────────


def _completed_plan(db, *, line_specs, so="SO-1"):
    """line_specs: list of (status, unit_sell, qty, with_card). Returns (plan, company, cards)."""
    owner = User(email="rep@trioscs.com", name="Rep", role="sales", azure_id="rep-cph")
    db.add(owner)
    db.flush()
    company = Company(name="CPH Buyer", is_active=True, account_owner_id=owner.id)
    db.add(company)
    db.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()
    req = Requisition(name="R", customer_site_id=site.id, status="archived", created_by=owner.id)
    db.add(req)
    db.flush()
    quote = Quote(requisition_id=req.id, quote_number=f"Q-{so}")
    db.add(quote)
    db.flush()
    plan = BuyPlan(
        quote_id=quote.id,
        requisition_id=req.id,
        status=BuyPlanStatus.COMPLETED.value,
        so_status=SOVerificationStatus.APPROVED.value,
        sales_order_number=so,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(plan)
    db.flush()
    cards = []
    for status, unit_sell, qty, with_card in line_specs:
        card = MaterialCard(normalized_mpn=f"CPHCARD{len(cards)}", display_mpn=f"CPH-{len(cards)}")
        db.add(card)
        db.flush()
        cards.append(card)
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn=card.display_mpn,
            normalized_mpn=card.normalized_mpn,
            material_card_id=(card.id if with_card else None),
        )
        db.add(requirement)
        db.flush()
        line = BuyPlanLine(
            buy_plan_id=plan.id,
            requirement_id=requirement.id,
            quantity=qty,
            unit_sell=Decimal(str(unit_sell)),
            status=status,
        )
        db.add(line)
    db.commit()
    return plan, company, cards


def test_records_cph_for_verified_lines(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history

    plan, company, cards = _completed_plan(
        db_session,
        line_specs=[(BuyPlanLineStatus.VERIFIED.value, 12.50, 100, True)],
        so="SO-XYZ",
    )
    affected = record_buyplan_purchase_history(db_session, plan)
    db_session.commit()
    assert cards[0].id in affected
    row = (
        db_session.query(CustomerPartHistory)
        .filter_by(company_id=company.id, material_card_id=cards[0].id, source="buy_plan")
        .one()
    )
    assert float(row.avg_unit_price) == 12.50
    assert row.last_quantity == 100
    assert row.source_ref == "SO-XYZ"
    assert plan.purchase_history_recorded_at is not None


def test_skips_cancelled_lines(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history

    plan, company, cards = _completed_plan(
        db_session,
        line_specs=[(BuyPlanLineStatus.CANCELLED.value, 9.0, 10, True)],
    )
    record_buyplan_purchase_history(db_session, plan)
    db_session.commit()
    assert db_session.query(CustomerPartHistory).filter_by(source="buy_plan").count() == 0


def test_idempotent(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history

    plan, company, cards = _completed_plan(db_session, line_specs=[(BuyPlanLineStatus.VERIFIED.value, 10.0, 5, True)])
    record_buyplan_purchase_history(db_session, plan)
    db_session.commit()
    record_buyplan_purchase_history(db_session, plan)
    db_session.commit()
    row = db_session.query(CustomerPartHistory).filter_by(source="buy_plan").one()
    assert row.purchase_count == 1  # not double-counted


def test_unresolvable_line_skipped_others_recorded(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history

    plan, company, cards = _completed_plan(
        db_session,
        line_specs=[
            (BuyPlanLineStatus.VERIFIED.value, 10.0, 5, False),
            (BuyPlanLineStatus.VERIFIED.value, 20.0, 5, True),
        ],
    )
    record_buyplan_purchase_history(db_session, plan)
    db_session.commit()
    rows = db_session.query(CustomerPartHistory).filter_by(source="buy_plan").all()
    assert len(rows) == 1 and rows[0].material_card_id == cards[1].id


# ── Task 3: check_completion hook ────────────────────────────────────


def test_check_completion_records_cph(db_session):
    from app.services.buyplan_workflow import check_completion

    plan, company, cards = _completed_plan(db_session, line_specs=[(BuyPlanLineStatus.VERIFIED.value, 15.0, 50, True)])
    # reset to the pre-completion state check_completion expects
    plan.status = BuyPlanStatus.ACTIVE.value
    plan.completed_at = None
    plan.purchase_history_recorded_at = None
    db_session.commit()
    check_completion(plan.id, db_session)
    db_session.commit()
    assert db_session.get(type(plan), plan.id).status == BuyPlanStatus.COMPLETED.value
    assert db_session.query(CustomerPartHistory).filter_by(company_id=company.id, source="buy_plan").count() == 1


# ── Task 4: immediate proactive re-match on completion ───────────────


def test_refresh_creates_match_when_live_offer_exists(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history

    plan, company, cards = _completed_plan(
        db_session, line_specs=[(BuyPlanLineStatus.VERIFIED.value, 12.50, 100, True)]
    )
    # live vendor stock for the purchased part, below the customer's historical price
    off = Offer(
        requisition_id=plan.requisition_id,
        material_card_id=cards[0].id,
        vendor_name="Avnet",
        mpn=cards[0].display_mpn,
        qty_available=500,
        unit_price=Decimal("8.00"),
        status="active",
    )
    db_session.add(off)
    db_session.commit()
    record_buyplan_purchase_history(db_session, plan)  # refresh=True by default
    db_session.commit()
    assert db_session.query(ProactiveMatch).filter_by(company_id=company.id, material_card_id=cards[0].id).count() == 1


# ── Task 7: backfill command for existing COMPLETED plans ────────────


def test_backfill_records_completed_plans_idempotently(db_session):
    from app.management.backfill_buyplan_cph import backfill

    plan, company, cards = _completed_plan(db_session, line_specs=[(BuyPlanLineStatus.VERIFIED.value, 11.0, 7, True)])
    plan.purchase_history_recorded_at = None
    db_session.commit()
    n1 = backfill(db_session)
    n2 = backfill(db_session)  # idempotent
    assert n1 == 1 and n2 == 0
    assert db_session.query(CustomerPartHistory).filter_by(source="buy_plan").count() == 1
