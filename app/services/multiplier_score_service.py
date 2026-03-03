"""Multiplier Score Service — Competitive points for monthly bonus determination.

Buyers earn points from offer pipeline progression (non-stacking: each offer
earns ONLY its highest achieved tier) plus bonus points from RFQs and stock
list uploads.

Sales earn points from quote progression, proactive offer conversion, and
new account creation.

Bonus winners require minimum Avail Score thresholds:
  1st: $500 — Avail Score >=60, >=10 offers (buyer) or >=20 activities (sales)
  2nd: $250 — Avail Score >=50, same minimums

Called by: scheduler.py (daily), routers/performance.py (on-demand)
Depends on: models (Offer, Quote, BuyPlan, Contact, StockListHash, etc.)
"""

from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..models import (
    BuyPlan,
    Company,
    Contact,
    Offer,
    ProactiveOffer,
    Quote,
    StockListHash,
    User,
)
from ..models.performance import AvailScoreSnapshot, MultiplierScoreSnapshot

# ── Point values ─────────────────────────────────────────────────────
# Buyer offer pipeline (non-stacking — highest tier only)
PTS_OFFER_BASE = 1
PTS_OFFER_QUOTED = 3
PTS_OFFER_BUYPLAN = 5
PTS_OFFER_PO = 8

# Buyer bonus (additive, not from offers)
PTS_RFQ_SENT = 0.25
PTS_STOCK_LIST = 2

# Sales scoring
PTS_QUOTE_SENT = 2
PTS_QUOTE_WON = 8
PTS_PROACTIVE_SENT = 1
PTS_PROACTIVE_CONVERTED = 4
PTS_NEW_ACCOUNT = 3

# Bonus thresholds
BONUS_1ST = 500.0
BONUS_2ND = 250.0
BONUS_3RD = 100.0
QUALIFY_SCORE_1ST = 60
QUALIFY_SCORE_2ND = 50
QUALIFY_SCORE_3RD = 40
MIN_OFFERS_BUYER = 10
MIN_ACTIVITIES_SALES = 20


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════


def _month_range(month: date):
    """Return (start_dt, end_dt) as aware datetimes for the given month."""
    from app.services.scoring_helpers import month_range

    return month_range(month)


def _load_quoted_offer_ids(db: Session) -> set[int]:
    """Return set of offer IDs that appear in any sent/won/lost quote line_items."""
    ids = set()
    for (items,) in db.query(Quote.line_items).filter(Quote.status.in_(["sent", "won", "lost"])).limit(10000).all():
        for item in items or []:
            oid = item.get("offer_id")
            if oid:
                ids.add(oid)
    return ids


def _load_buyplan_offer_ids(db: Session) -> tuple[set[int], set[int]]:
    """Return (bp_offer_ids, po_confirmed_offer_ids) from buy plan line_items."""
    bp_ids = set()
    po_ids = set()
    for bp_status, items in db.query(BuyPlan.status, BuyPlan.line_items).limit(10000).all():
        for item in items or []:
            oid = item.get("offer_id")
            if oid:
                bp_ids.add(oid)
                if bp_status in ("po_confirmed", "complete"):
                    po_ids.add(oid)
    return bp_ids, po_ids


# ══════════════════════════════════════════════════════════════════════
#  BUYER MULTIPLIER
# ══════════════════════════════════════════════════════════════════════


def compute_buyer_multiplier(
    db: Session,
    user_id: int,
    month: date,
    *,
    quoted_ids: set[int] | None = None,
    bp_ids: set[int] | None = None,
    po_ids: set[int] | None = None,
) -> dict:
    """Compute multiplier points for a buyer in a given month.

    Non-stacking: each offer earns ONLY its highest achieved tier.
    Plus bonus points from RFQs sent and stock list uploads.

    Pre-loaded lookup sets can be passed to avoid re-querying per user.
    """
    start_dt, end_dt = _month_range(month)

    # Load global lookups if not provided
    if quoted_ids is None:
        quoted_ids = _load_quoted_offer_ids(db)
    if bp_ids is None or po_ids is None:
        bp_ids, po_ids = _load_buyplan_offer_ids(db)

    # User's offers this month
    user_offers = (
        db.query(Offer)
        .filter(
            Offer.entered_by_id == user_id,
            Offer.created_at >= start_dt,
            Offer.created_at < end_dt,
        )
        .all()
    )

    # Grace period: offers from last 7 days of previous month that advanced
    grace_start = start_dt - timedelta(days=7)
    grace_offers = (
        db.query(Offer)
        .filter(
            Offer.entered_by_id == user_id,
            Offer.created_at >= grace_start,
            Offer.created_at < start_dt,
        )
        .all()
    )
    grace_advanced = [o for o in grace_offers if o.id in quoted_ids or o.id in bp_ids]
    all_offers = user_offers + grace_advanced

    # Classify each offer into its HIGHEST tier (non-stacking)
    count_po = 0
    count_bp = 0
    count_quoted = 0
    count_base = 0

    for o in all_offers:
        if o.id in po_ids:
            count_po += 1
        elif o.id in bp_ids:
            count_bp += 1
        elif o.id in quoted_ids:
            count_quoted += 1
        else:
            count_base += 1

    pts_base = count_base * PTS_OFFER_BASE
    pts_quoted = count_quoted * PTS_OFFER_QUOTED
    pts_bp = count_bp * PTS_OFFER_BUYPLAN
    pts_po = count_po * PTS_OFFER_PO
    offer_points = pts_base + pts_quoted + pts_bp + pts_po

    # Bonus: RFQs sent this month
    rfqs_sent = (
        db.query(sqlfunc.count(Contact.id))
        .filter(
            Contact.user_id == user_id,
            Contact.contact_type == "email",
            Contact.created_at >= start_dt,
            Contact.created_at < end_dt,
        )
        .scalar()
    ) or 0
    pts_rfqs = rfqs_sent * PTS_RFQ_SENT

    # Bonus: Stock lists uploaded this month
    stock_lists = (
        db.query(sqlfunc.count(StockListHash.id))
        .filter(
            StockListHash.user_id == user_id,
            StockListHash.first_seen_at >= start_dt,
            StockListHash.first_seen_at < end_dt,
        )
        .scalar()
    ) or 0
    pts_stock = stock_lists * PTS_STOCK_LIST

    bonus_points = pts_rfqs + pts_stock
    total_points = offer_points + bonus_points

    logger.debug(
        "Buyer multiplier user=%d: %d offers, %.1f offer_pts, %.1f bonus_pts",
        user_id,
        len(all_offers),
        offer_points,
        bonus_points,
    )

    return {
        "user_id": user_id,
        "role_type": "buyer",
        "offer_points": offer_points,
        "bonus_points": bonus_points,
        "total_points": total_points,
        # Buyer breakdown columns
        "offers_total": len(all_offers),
        "offers_base_count": count_base,
        "offers_base_pts": pts_base,
        "offers_quoted_count": count_quoted,
        "offers_quoted_pts": pts_quoted,
        "offers_bp_count": count_bp,
        "offers_bp_pts": pts_bp,
        "offers_po_count": count_po,
        "offers_po_pts": pts_po,
        "rfqs_sent_count": rfqs_sent,
        "rfqs_sent_pts": pts_rfqs,
        "stock_lists_count": stock_lists,
        "stock_lists_pts": pts_stock,
    }


# ══════════════════════════════════════════════════════════════════════
#  SALES MULTIPLIER
# ══════════════════════════════════════════════════════════════════════


def compute_sales_multiplier(db: Session, user_id: int, month: date) -> dict:
    """Compute multiplier points for a salesperson in a given month.

    Quote progression is non-stacking (won replaces sent).
    Proactive conversion is non-stacking (converted replaces sent).
    New account points are additive.
    """
    start_dt, end_dt = _month_range(month)

    # ── Quotes: non-stacking (won replaces sent) ──
    quotes_sent = (
        db.query(sqlfunc.count(Quote.id))
        .filter(
            Quote.created_by_id == user_id,
            Quote.sent_at >= start_dt,
            Quote.sent_at < end_dt,
            Quote.status.in_(["sent", "won", "lost"]),
        )
        .scalar()
    ) or 0

    quotes_won = (
        db.query(sqlfunc.count(Quote.id))
        .filter(
            Quote.created_by_id == user_id,
            Quote.result == "won",
            Quote.result_at >= start_dt,
            Quote.result_at < end_dt,
        )
        .scalar()
    ) or 0

    count_quote_won = quotes_won
    count_quote_sent_only = max(0, quotes_sent - quotes_won)
    pts_quote_won = count_quote_won * PTS_QUOTE_WON
    pts_quote_sent = count_quote_sent_only * PTS_QUOTE_SENT

    # ── Proactive: non-stacking (converted replaces sent) ──
    proactive_sent = (
        db.query(sqlfunc.count(ProactiveOffer.id))
        .filter(
            ProactiveOffer.salesperson_id == user_id,
            ProactiveOffer.sent_at >= start_dt,
            ProactiveOffer.sent_at < end_dt,
        )
        .scalar()
    ) or 0

    proactive_converted = (
        db.query(sqlfunc.count(ProactiveOffer.id))
        .filter(
            ProactiveOffer.salesperson_id == user_id,
            ProactiveOffer.status == "converted",
            ProactiveOffer.converted_at >= start_dt,
            ProactiveOffer.converted_at < end_dt,
        )
        .scalar()
    ) or 0

    count_proactive_converted = proactive_converted
    count_proactive_sent_only = max(0, proactive_sent - proactive_converted)
    pts_proactive_converted = count_proactive_converted * PTS_PROACTIVE_CONVERTED
    pts_proactive_sent = count_proactive_sent_only * PTS_PROACTIVE_SENT

    # ── New accounts: additive ──
    new_accounts = (
        db.query(sqlfunc.count(Company.id))
        .filter(
            Company.account_owner_id == user_id,
            Company.created_at >= start_dt,
            Company.created_at < end_dt,
        )
        .scalar()
    ) or 0
    pts_accounts = new_accounts * PTS_NEW_ACCOUNT

    offer_points = pts_quote_won + pts_quote_sent + pts_proactive_converted + pts_proactive_sent
    bonus_points = pts_accounts
    total_points = offer_points + bonus_points

    logger.debug("Sales multiplier user=%d: %.1f offer_pts, %.1f bonus_pts", user_id, offer_points, bonus_points)

    return {
        "user_id": user_id,
        "role_type": "sales",
        "offer_points": offer_points,
        "bonus_points": bonus_points,
        "total_points": total_points,
        # Sales breakdown columns
        "quotes_sent_count": count_quote_sent_only,
        "quotes_sent_pts": pts_quote_sent,
        "quotes_won_count": count_quote_won,
        "quotes_won_pts": pts_quote_won,
        "proactive_sent_count": count_proactive_sent_only,
        "proactive_sent_pts": pts_proactive_sent,
        "proactive_converted_count": count_proactive_converted,
        "proactive_converted_pts": pts_proactive_converted,
        "new_accounts_count": new_accounts,
        "new_accounts_pts": pts_accounts,
    }


# ══════════════════════════════════════════════════════════════════════
#  BATCH COMPUTE + RANK + BONUS
# ══════════════════════════════════════════════════════════════════════


def compute_all_multiplier_scores(db: Session, month: date | None = None) -> dict:
    """Compute multiplier scores for all users, rank, and assign bonuses."""
    month = (month or date.today()).replace(day=1)

    _human = [User.is_active.is_(True), ~User.email.like("%@availai.local")]

    buyers = db.query(User).filter(User.role.in_(["buyer", "trader"]), *_human).all()
    sales = db.query(User).filter(User.role.in_(["sales", "manager"]), *_human).all()
    multi_role = db.query(User).filter(User.role == "trader", *_human).all()

    # Pre-load offer status lookups once for all buyers
    quoted_ids = _load_quoted_offer_ids(db)
    bp_ids, po_ids = _load_buyplan_offer_ids(db)

    buyer_results = []
    for user in buyers:
        try:
            result = compute_buyer_multiplier(
                db,
                user.id,
                month,
                quoted_ids=quoted_ids,
                bp_ids=bp_ids,
                po_ids=po_ids,
            )
            result["user_name"] = user.name
            buyer_results.append(result)
        except Exception as e:
            logger.error("Multiplier error for buyer %s: %s", user.id, e)

    sales_results = []
    for user in sales + multi_role:
        try:
            result = compute_sales_multiplier(db, user.id, month)
            result["user_name"] = user.name
            sales_results.append(result)
        except Exception as e:
            logger.error("Multiplier error for sales %s: %s", user.id, e)

    # Attach Avail Scores and determine bonuses
    _attach_avail_scores_and_rank(db, buyer_results, month, "buyer")
    _attach_avail_scores_and_rank(db, sales_results, month, "sales")

    # Persist
    saved = 0
    for results in [buyer_results, sales_results]:
        for r in results:
            saved += _upsert_multiplier(db, r, month)

    db.commit()
    logger.info("Multiplier scores: %d buyers, %d sales, %d saved", len(buyer_results), len(sales_results), saved)
    return {"buyers": len(buyer_results), "sales": len(sales_results), "saved": saved}


def _attach_avail_scores_and_rank(db: Session, results: list[dict], month: date, role_type: str):
    """Load Avail Scores, determine qualification, rank, and assign bonuses."""
    month_start = month.replace(day=1)

    avail_map = {}
    for snap in (
        db.query(AvailScoreSnapshot)
        .filter(
            AvailScoreSnapshot.month == month_start,
            AvailScoreSnapshot.role_type == role_type,
        )
        .all()
    ):
        avail_map[snap.user_id] = snap.total_score or 0

    for r in results:
        uid = r["user_id"]
        r["avail_score"] = avail_map.get(uid, 0)

        if role_type == "buyer":
            r["qualified"] = r["avail_score"] >= QUALIFY_SCORE_2ND and r.get("offers_total", 0) >= MIN_OFFERS_BUYER
        else:
            r["qualified"] = r["avail_score"] >= QUALIFY_SCORE_2ND

    # Rank by total_points desc, tiebreak by avail_score
    results.sort(key=lambda r: (r["total_points"], r["avail_score"]), reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    # Assign bonuses
    for r in results:
        r["bonus_amount"] = 0.0

    qualified = [r for r in results if r["qualified"]]
    if len(qualified) >= 1 and qualified[0]["avail_score"] >= QUALIFY_SCORE_1ST:
        qualified[0]["bonus_amount"] = BONUS_1ST
    if len(qualified) >= 2 and qualified[1]["avail_score"] >= QUALIFY_SCORE_2ND:
        qualified[1]["bonus_amount"] = BONUS_2ND
    if len(qualified) >= 3 and qualified[2]["avail_score"] >= QUALIFY_SCORE_3RD:
        qualified[2]["bonus_amount"] = BONUS_3RD


def _upsert_multiplier(db: Session, result: dict, month: date) -> int:
    """Upsert a MultiplierScoreSnapshot row. Returns 1 if saved."""
    month_start = month.replace(day=1)
    existing = (
        db.query(MultiplierScoreSnapshot)
        .filter(
            MultiplierScoreSnapshot.user_id == result["user_id"],
            MultiplierScoreSnapshot.month == month_start,
            MultiplierScoreSnapshot.role_type == result["role_type"],
        )
        .first()
    )

    snap = existing or MultiplierScoreSnapshot(
        user_id=result["user_id"],
        month=month_start,
        role_type=result["role_type"],
    )
    if not existing:
        db.add(snap)

    # Totals
    snap.offer_points = result["offer_points"]
    snap.bonus_points = result["bonus_points"]
    snap.total_points = result["total_points"]

    # Buyer breakdown (NULL for sales rows)
    for col in (
        "offers_total",
        "offers_base_count",
        "offers_base_pts",
        "offers_quoted_count",
        "offers_quoted_pts",
        "offers_bp_count",
        "offers_bp_pts",
        "offers_po_count",
        "offers_po_pts",
        "rfqs_sent_count",
        "rfqs_sent_pts",
        "stock_lists_count",
        "stock_lists_pts",
    ):
        if col in result:
            setattr(snap, col, result[col])

    # Sales breakdown (NULL for buyer rows)
    for col in (
        "quotes_sent_count",
        "quotes_sent_pts",
        "quotes_won_count",
        "quotes_won_pts",
        "proactive_sent_count",
        "proactive_sent_pts",
        "proactive_converted_count",
        "proactive_converted_pts",
        "new_accounts_count",
        "new_accounts_pts",
    ):
        if col in result:
            setattr(snap, col, result[col])

    snap.rank = result.get("rank")
    snap.avail_score = result.get("avail_score", 0)
    snap.qualified = result.get("qualified", False)
    snap.bonus_amount = result.get("bonus_amount", 0)

    return 1


# ══════════════════════════════════════════════════════════════════════
#  BONUS WINNER DETERMINATION
# ══════════════════════════════════════════════════════════════════════


def determine_bonus_winners(db: Session, role_type: str, month: date) -> list[dict]:
    """Return bonus winners for a role+month from persisted multiplier scores.

    Returns list of 0-2 winners. Winners must meet Avail Score thresholds.
    """
    month_start = month.replace(day=1)
    rows = (
        db.query(MultiplierScoreSnapshot, User.name)
        .join(User, User.id == MultiplierScoreSnapshot.user_id)
        .filter(
            MultiplierScoreSnapshot.month == month_start,
            MultiplierScoreSnapshot.role_type == role_type,
            MultiplierScoreSnapshot.qualified.is_(True),
        )
        .order_by(MultiplierScoreSnapshot.total_points.desc())
        .all()
    )

    winners = []
    for snap, user_name in rows:
        if len(winners) >= 2:
            break

        if len(winners) == 0 and snap.avail_score >= QUALIFY_SCORE_1ST:
            winners.append(
                {
                    "user_id": snap.user_id,
                    "user_name": user_name,
                    "rank": 1,
                    "total_points": snap.total_points,
                    "avail_score": snap.avail_score,
                    "bonus_amount": BONUS_1ST,
                }
            )
        elif len(winners) == 1 and snap.avail_score >= QUALIFY_SCORE_2ND:
            winners.append(
                {
                    "user_id": snap.user_id,
                    "user_name": user_name,
                    "rank": 2,
                    "total_points": snap.total_points,
                    "avail_score": snap.avail_score,
                    "bonus_amount": BONUS_2ND,
                }
            )
        elif (
            len(winners) == 2 and snap.avail_score >= QUALIFY_SCORE_3RD
        ):  # pragma: no cover — unreachable: break at >=2
            winners.append(
                {
                    "user_id": snap.user_id,
                    "user_name": user_name,
                    "rank": 3,
                    "total_points": snap.total_points,
                    "avail_score": snap.avail_score,
                    "bonus_amount": BONUS_3RD,
                }
            )

    return winners


# ══════════════════════════════════════════════════════════════════════
#  API QUERIES
# ══════════════════════════════════════════════════════════════════════


def get_multiplier_scores(db: Session, role_type: str, month: date) -> list[dict]:
    """Return ranked multiplier scores for a role type and month."""
    month_start = month.replace(day=1)
    rows = (
        db.query(MultiplierScoreSnapshot, User.name)
        .join(User, User.id == MultiplierScoreSnapshot.user_id)
        .filter(
            MultiplierScoreSnapshot.month == month_start,
            MultiplierScoreSnapshot.role_type == role_type,
        )
        .order_by(MultiplierScoreSnapshot.rank)
        .all()
    )

    results = []
    for snap, user_name in rows:
        entry = {
            "user_id": snap.user_id,
            "user_name": user_name,
            "rank": snap.rank,
            "total_points": snap.total_points,
            "offer_points": snap.offer_points,
            "bonus_points": snap.bonus_points,
            "avail_score": snap.avail_score,
            "qualified": snap.qualified,
            "bonus_amount": snap.bonus_amount,
            "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
        }

        # Include role-specific breakdown
        if role_type == "buyer":
            entry["breakdown"] = {
                "offers_total": snap.offers_total,
                "offers_base": snap.offers_base_count,
                "offers_quoted": snap.offers_quoted_count,
                "offers_bp": snap.offers_bp_count,
                "offers_po": snap.offers_po_count,
                "pts_base": snap.offers_base_pts,
                "pts_quoted": snap.offers_quoted_pts,
                "pts_bp": snap.offers_bp_pts,
                "pts_po": snap.offers_po_pts,
                "rfqs_sent": snap.rfqs_sent_count,
                "pts_rfqs": snap.rfqs_sent_pts,
                "stock_lists": snap.stock_lists_count,
                "pts_stock": snap.stock_lists_pts,
            }
        else:
            entry["breakdown"] = {
                "quotes_sent": snap.quotes_sent_count,
                "quotes_won": snap.quotes_won_count,
                "pts_quote_sent": snap.quotes_sent_pts,
                "pts_quote_won": snap.quotes_won_pts,
                "proactive_sent": snap.proactive_sent_count,
                "proactive_converted": snap.proactive_converted_count,
                "pts_proactive_sent": snap.proactive_sent_pts,
                "pts_proactive_converted": snap.proactive_converted_pts,
                "new_accounts": snap.new_accounts_count,
                "pts_accounts": snap.new_accounts_pts,
            }

        results.append(entry)

    return results
