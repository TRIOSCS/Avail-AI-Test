"""Unified Score Service — cross-role normalized scoring for combined leaderboard.

Maps AvailScore metrics (b1-b5, o1-o5) into 4 universal categories, normalizes
to 0-100%, and applies weights to produce a single unified score. Traders average
buyer + sales category percentages. AI blurbs cached with 2-hour TTL.

Category weights:
  Execution 30% | Follow-Through 25% | Closing 30% | Depth 15%

Called by: scheduler.py (daily, after multiplier scores), routers/performance.py (refresh)
Depends on: models/performance.py (AvailScoreSnapshot, MultiplierScoreSnapshot),
            models/unified_score.py (UnifiedScoreSnapshot),
            utils/claude_client.py (claude_structured for blurbs)
"""

from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import UserRole
from ..models import User
from ..models.performance import AvailScoreSnapshot, MultiplierScoreSnapshot
from ..models.unified_score import UnifiedScoreSnapshot

# ── Category Weights ────────────────────────────────────────────────
CATEGORY_WEIGHTS = {
    "execution": 0.30,
    "followthrough": 0.25,
    "closing": 0.30,
    "depth": 0.15,
}

# Max possible raw points per category per role (sum of max metric scores)
# Buyer:  b1(10)+b4(10)+o1(10)=30, b3(10)+o2(10)=20, o3(10)+o4(10)=20, o5(10)=10
# Sales:  b2(10)+b4(10)+o3(10)=30, b3(10)+o4(10)=20, o1(10)+o2(10)=20, o5(10)=10


def _buyer_categories(snap: AvailScoreSnapshot) -> dict[str, float]:
    """Extract 4 category percentages from a buyer AvailScoreSnapshot.

    Buyer metric mapping:
      Execution:      B1 Speed to Source + B4 Pipeline Hygiene + O1 Sourcing Ratio → /30
      Follow-Through: B3 Vendor Follow-Up + O2 Offer→Quote → /20
      Closing:        O3 Win Rate + O4 BP Completion → /20
      Depth:          O5 Vendor Diversity → /10
    """
    b1 = snap.b1_score or 0
    b3 = snap.b3_score or 0
    b4 = snap.b4_score or 0
    o1 = snap.o1_score or 0
    o2 = snap.o2_score or 0
    o3 = snap.o3_score or 0
    o4 = snap.o4_score or 0
    o5 = snap.o5_score or 0

    return {
        "execution": _safe_pct(b1 + b4 + o1, 30),
        "followthrough": _safe_pct(b3 + o2, 20),
        "closing": _safe_pct(o3 + o4, 20),
        "depth": _safe_pct(o5, 10),
    }


def _sales_categories(snap: AvailScoreSnapshot) -> dict[str, float]:
    """Extract 4 category percentages from a sales AvailScoreSnapshot.

    Sales metric mapping:
      Execution:      B2 Outreach Consistency + B4 Proactive Selling + O3 Quote Volume → /30
      Follow-Through: B3 Quote Follow-Up + O4 Proactive Conversion → /20
      Closing:        O1 Win Rate + O2 Revenue → /20
      Depth:          O5 Strategic Wins → /10
    """
    b2 = snap.b2_score or 0
    b3 = snap.b3_score or 0
    b4 = snap.b4_score or 0
    o1 = snap.o1_score or 0
    o2 = snap.o2_score or 0
    o3 = snap.o3_score or 0
    o4 = snap.o4_score or 0
    o5 = snap.o5_score or 0

    return {
        "execution": _safe_pct(b2 + b4 + o3, 30),
        "followthrough": _safe_pct(b3 + o4, 20),
        "closing": _safe_pct(o1 + o2, 20),
        "depth": _safe_pct(o5, 10),
    }


def _safe_pct(raw: float, max_raw: float) -> float:
    """Convert raw score to 0-100 percentage, clamped."""
    if max_raw <= 0:
        return 0.0
    return min(100.0, max(0.0, (raw / max_raw) * 100))


def _merge_trader_categories(
    buyer_cats: dict[str, float] | None,
    sales_cats: dict[str, float] | None,
) -> dict[str, float]:
    """Average buyer + sales category percentages for traders.

    If only one role has data, use that alone (no penalization).
    """
    if buyer_cats and sales_cats:
        return {k: (buyer_cats[k] + sales_cats[k]) / 2 for k in CATEGORY_WEIGHTS}
    return buyer_cats or sales_cats or {k: 0.0 for k in CATEGORY_WEIGHTS}


def _weighted_score(cats: dict[str, float]) -> float:
    """Apply category weights to produce unified score 0-100."""
    return sum(cats.get(k, 0) * w for k, w in CATEGORY_WEIGHTS.items())


def compute_all_unified_scores(db: Session, month: date | None = None) -> dict:
    """Compute unified scores for all active users, rank, and save.

    Returns summary dict: {computed: int, saved: int}
    """
    month = (month or date.today()).replace(day=1)

    # Get all active human users
    _human = [User.is_active.is_(True), ~User.email.like("%@availai.local")]
    users = db.query(User).filter(*_human).all()

    # Load all AvailScoreSnapshots for this month
    avail_snaps = db.query(AvailScoreSnapshot).filter(AvailScoreSnapshot.month == month).all()
    # Index by (user_id, role_type)
    avail_idx: dict[tuple[int, str], AvailScoreSnapshot] = {}
    for s in avail_snaps:
        avail_idx[(s.user_id, s.role_type)] = s

    # Load MultiplierScoreSnapshots for cached display
    mult_snaps = db.query(MultiplierScoreSnapshot).filter(MultiplierScoreSnapshot.month == month).all()
    mult_idx: dict[tuple[int, str], MultiplierScoreSnapshot] = {}
    for s in mult_snaps:
        mult_idx[(s.user_id, s.role_type)] = s

    results = []
    for user in users:
        buyer_snap = avail_idx.get((user.id, "buyer"))
        sales_snap = avail_idx.get((user.id, "sales"))

        if not buyer_snap and not sales_snap:
            continue  # no data at all

        buyer_cats = _buyer_categories(buyer_snap) if buyer_snap else None
        sales_cats = _sales_categories(sales_snap) if sales_snap else None

        # Determine primary role and compute categories
        if user.role == UserRole.TRADER:
            cats = _merge_trader_categories(buyer_cats, sales_cats)
            primary_role = "trader"
        elif user.role in (UserRole.SALES, UserRole.MANAGER):
            cats = sales_cats or {k: 0.0 for k in CATEGORY_WEIGHTS}
            primary_role = "sales"
        else:
            cats = buyer_cats or {k: 0.0 for k in CATEGORY_WEIGHTS}
            primary_role = "buyer"

        score = _weighted_score(cats)

        # Get cached source scores
        buyer_mult = mult_idx.get((user.id, "buyer"))
        sales_mult = mult_idx.get((user.id, "sales"))

        results.append(
            {
                "user_id": user.id,
                "user_name": user.name,
                "primary_role": primary_role,
                "cats": cats,
                "score": round(score, 2),
                "avail_score_buyer": buyer_snap.total_score if buyer_snap else None,
                "avail_score_sales": sales_snap.total_score if sales_snap else None,
                "multiplier_points_buyer": buyer_mult.total_points if buyer_mult else None,
                "multiplier_points_sales": sales_mult.total_points if sales_mult else None,
            }
        )

    # Rank by unified score descending
    results.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i

    # Upsert snapshots
    saved = 0
    for r in results:
        existing = (
            db.query(UnifiedScoreSnapshot)
            .filter(
                UnifiedScoreSnapshot.user_id == r["user_id"],
                UnifiedScoreSnapshot.month == month,
            )
            .first()
        )
        if existing:
            snap = existing
        else:
            snap = UnifiedScoreSnapshot(user_id=r["user_id"], month=month)
            db.add(snap)

        snap.prospecting_pct = 0.0  # category removed, column kept for backward compat
        snap.execution_pct = round(r["cats"]["execution"], 2)
        snap.followthrough_pct = round(r["cats"]["followthrough"], 2)
        snap.closing_pct = round(r["cats"]["closing"], 2)
        snap.depth_pct = round(r["cats"]["depth"], 2)
        snap.unified_score = r["score"]
        snap.rank = r["rank"]
        snap.primary_role = r["primary_role"]
        snap.avail_score_buyer = r["avail_score_buyer"]
        snap.avail_score_sales = r["avail_score_sales"]
        snap.multiplier_points_buyer = r["multiplier_points_buyer"]
        snap.multiplier_points_sales = r["multiplier_points_sales"]
        snap.updated_at = datetime.now(timezone.utc)
        saved += 1

    db.commit()
    logger.info(f"Unified scores: {saved} users scored for {month}")

    # Generate AI blurbs for entries that need refresh
    _refresh_blurbs(db, month, results)

    return {"computed": len(results), "saved": saved}


def _refresh_blurbs(db: Session, month: date, results: list[dict]) -> None:
    """Generate AI blurbs for entries where blurb is stale or missing."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    total = len(results)

    for r in results:
        snap = (
            db.query(UnifiedScoreSnapshot)
            .filter(
                UnifiedScoreSnapshot.user_id == r["user_id"],
                UnifiedScoreSnapshot.month == month,
            )
            .first()
        )
        if not snap:
            continue

        # Skip if blurb is fresh (< 2 hours old)
        if snap.ai_blurb_generated_at and snap.ai_blurb_generated_at >= cutoff:
            continue

        try:
            blurb = _generate_blurb(
                r["user_name"],
                r["primary_role"],
                r["cats"],
                r["score"],
                r["rank"],
                total,
            )
            if blurb:
                snap.ai_blurb_strength = blurb.get("strength", "")
                snap.ai_blurb_improvement = blurb.get("improvement", "")
                snap.ai_blurb_generated_at = datetime.now(timezone.utc)
        except Exception as e:
            logger.warning(f"Blurb generation failed for user {r['user_id']}: {e}")

    db.commit()


def _generate_blurb(
    user_name: str,
    role: str,
    cats: dict[str, float],
    score: float,
    rank: int,
    total: int,
) -> dict | None:
    """Call Claude to generate strength + improvement blurb."""
    import asyncio

    from ..utils.claude_client import claude_structured

    # Find best and worst categories
    sorted_cats = sorted(cats.items(), key=lambda x: x[1], reverse=True)
    best_cat, best_pct = sorted_cats[0]
    worst_cat, worst_pct = sorted_cats[-1]

    system = (
        "You write brief, specific performance feedback for an electronic component "
        "trading team. Max 1 sentence each. Use 'You' address. Be encouraging but honest."
    )
    user_msg = (
        f"{user_name} ({role}) scores this month:\n"
        f"Execution: {cats['execution']:.0f}%, Follow-Through: {cats['followthrough']:.0f}%, "
        f"Closing: {cats['closing']:.0f}%, Depth: {cats['depth']:.0f}%\n"
        f"Unified Score: {score:.0f}/100 (#{rank} of {total})\n"
        f"Best category: {best_cat} at {best_pct:.0f}%. Weakest: {worst_cat} at {worst_pct:.0f}%.\n\n"
        'Return JSON: {"strength": "...", "improvement": "..."}'
    )
    schema = {
        "type": "object",
        "properties": {
            "strength": {"type": "string"},
            "improvement": {"type": "string"},
        },
        "required": ["strength", "improvement"],
    }

    try:
        try:
            asyncio.get_running_loop()
            # Already in async context — run in thread pool
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    claude_structured(user_msg, schema, system=system, model_tier="fast", max_tokens=256),
                )
                return future.result(timeout=30)
        except RuntimeError:
            return asyncio.run(claude_structured(user_msg, schema, system=system, model_tier="fast", max_tokens=256))
    except Exception as e:
        logger.warning(f"Claude blurb call failed: {e}")
        return None


def get_unified_leaderboard(db: Session, month: date | None = None) -> dict:
    """Return unified leaderboard data joined with AvailScore breakdown.

    Returns: {month, entries: [...]}
    """
    month = (month or date.today()).replace(day=1)

    snaps = (
        db.query(UnifiedScoreSnapshot)
        .filter(UnifiedScoreSnapshot.month == month)
        .order_by(UnifiedScoreSnapshot.rank)
        .all()
    )

    # Load full AvailScore + Multiplier breakdowns for expandable rows
    avail_snaps = db.query(AvailScoreSnapshot).filter(AvailScoreSnapshot.month == month).all()
    avail_idx: dict[tuple[int, str], AvailScoreSnapshot] = {}
    for s in avail_snaps:
        avail_idx[(s.user_id, s.role_type)] = s

    mult_snaps = db.query(MultiplierScoreSnapshot).filter(MultiplierScoreSnapshot.month == month).all()
    mult_idx: dict[tuple[int, str], MultiplierScoreSnapshot] = {}
    for s in mult_snaps:
        mult_idx[(s.user_id, s.role_type)] = s

    # Build user name lookup
    user_ids = [s.user_id for s in snaps]
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}

    entries = []
    for snap in snaps:
        user = users.get(snap.user_id)
        entry = {
            "user_id": snap.user_id,
            "user_name": user.name if user else f"User #{snap.user_id}",
            "user_role": user.role if user else "unknown",
            "primary_role": snap.primary_role,
            "unified_score": snap.unified_score,
            "rank": snap.rank,
            "prospecting_pct": snap.prospecting_pct,
            "execution_pct": snap.execution_pct,
            "followthrough_pct": snap.followthrough_pct,
            "closing_pct": snap.closing_pct,
            "depth_pct": snap.depth_pct,
            "ai_blurb_strength": snap.ai_blurb_strength,
            "ai_blurb_improvement": snap.ai_blurb_improvement,
            "avail_score_buyer": snap.avail_score_buyer,
            "avail_score_sales": snap.avail_score_sales,
            "multiplier_points_buyer": snap.multiplier_points_buyer,
            "multiplier_points_sales": snap.multiplier_points_sales,
        }

        # Add full b1-o5 breakdown for each applicable role
        for role_type in ("buyer", "sales"):
            avail = avail_idx.get((snap.user_id, role_type))
            if avail:
                prefix = f"{role_type}_"
                for metric in ("b1", "b2", "b3", "b4", "b5", "o1", "o2", "o3", "o4", "o5"):
                    entry[f"{prefix}{metric}_score"] = getattr(avail, f"{metric}_score", 0) or 0
                    entry[f"{prefix}{metric}_label"] = getattr(avail, f"{metric}_label", "") or ""
                    entry[f"{prefix}{metric}_raw"] = getattr(avail, f"{metric}_raw", "") or ""
                entry[f"{prefix}behavior_total"] = avail.behavior_total or 0
                entry[f"{prefix}outcome_total"] = avail.outcome_total or 0

            mult = mult_idx.get((snap.user_id, role_type))
            if mult:
                entry[f"{role_type}_total_points"] = mult.total_points or 0
                entry[f"{role_type}_offer_points"] = mult.offer_points or 0
                entry[f"{role_type}_bonus_points"] = mult.bonus_points or 0
                entry[f"{role_type}_qualified"] = mult.qualified or False
                entry[f"{role_type}_mult_bonus"] = mult.bonus_amount or 0
                # Full tier breakdown for detail view
                if role_type == UserRole.BUYER:
                    entry[f"{role_type}_breakdown"] = {
                        "offers_base": mult.offers_base_count or 0,
                        "pts_base": mult.offers_base_pts or 0,
                        "offers_quoted": mult.offers_quoted_count or 0,
                        "pts_quoted": mult.offers_quoted_pts or 0,
                        "offers_bp": mult.offers_bp_count or 0,
                        "pts_bp": mult.offers_bp_pts or 0,
                        "offers_po": mult.offers_po_count or 0,
                        "pts_po": mult.offers_po_pts or 0,
                        "rfqs_sent": mult.rfqs_sent_count or 0,
                        "pts_rfqs": mult.rfqs_sent_pts or 0,
                        "stock_lists": mult.stock_lists_count or 0,
                        "pts_stock": mult.stock_lists_pts or 0,
                    }
                else:
                    entry[f"{role_type}_breakdown"] = {
                        "quotes_sent": mult.quotes_sent_count or 0,
                        "pts_quote_sent": mult.quotes_sent_pts or 0,
                        "quotes_won": mult.quotes_won_count or 0,
                        "pts_quote_won": mult.quotes_won_pts or 0,
                        "proactive_sent": mult.proactive_sent_count or 0,
                        "pts_proactive_sent": mult.proactive_sent_pts or 0,
                        "proactive_converted": mult.proactive_converted_count or 0,
                        "pts_proactive_converted": mult.proactive_converted_pts or 0,
                        "new_accounts": mult.new_accounts_count or 0,
                        "pts_accounts": mult.new_accounts_pts or 0,
                    }

        # Convenience: top-level avail_score and total_points (use primary role)
        pr = snap.primary_role or "buyer"
        entry["avail_score"] = entry.get(f"{pr}_behavior_total", 0) + entry.get(f"{pr}_outcome_total", 0)
        entry["total_points"] = entry.get(f"{pr}_total_points", 0)
        # For traders, sum both roles' points
        if pr == UserRole.TRADER:
            entry["total_points"] = (entry.get("buyer_total_points", 0) or 0) + (
                entry.get("sales_total_points", 0) or 0
            )
            entry["avail_score"] = snap.avail_score_buyer or snap.avail_score_sales or 0
        # Qualification & bonus aggregation
        entry["avail_qualified"] = entry.get("buyer_qualified", False) or entry.get("sales_qualified", False)
        entry["mult_qualified"] = entry.get("buyer_qualified", False) or entry.get("sales_qualified", False)
        entry["avail_bonus"] = (entry.get("buyer_mult_bonus", 0) or 0) + (entry.get("sales_mult_bonus", 0) or 0)
        entry["mult_bonus"] = entry.get("avail_bonus", 0)

        entries.append(entry)

    return {"month": month.isoformat(), "entries": entries}


def get_scoring_info() -> dict:
    """Return static scoring system explanation for the info pill tooltip."""
    return {
        "categories": [
            {
                "name": "Execution",
                "weight": 30,
                "description": "Speed and volume of core work — sourcing speed, outreach consistency, pipeline throughput",
            },
            {
                "name": "Follow-Through",
                "weight": 25,
                "description": "Persistence and deal progression — vendor follow-ups, quote conversions, offer advancement",
            },
            {
                "name": "Closing",
                "weight": 30,
                "description": "Win rate and revenue generation — deals won, buy plans completed, revenue produced",
            },
            {
                "name": "Depth",
                "weight": 15,
                "description": "Strategic breadth — vendor diversity for buyers, strategic wins for sales",
            },
        ],
        "total_range": "0-100",
        "normalization": "Scores are normalized by role so buyers, sales, and traders compete fairly. Traders average their buyer and sales scores.",
        "ai_refresh": "AI insights refresh every 2 hours.",
        "avail_score": {
            "description": "10 metrics (5 behaviors + 5 outcomes) scored 0-10 each, totaling 0-100.",
            "buyer_behaviors": [
                "Speed to Source",
                "Multi-Source Breadth",
                "Vendor Follow-Up",
                "Pipeline Hygiene",
                "Stock List Uploads",
            ],
            "buyer_outcomes": [
                "Sourcing Ratio",
                "Offer-to-Quote",
                "Win Rate",
                "Buy Plan Completion",
                "Vendor Diversity",
            ],
            "sales_behaviors": [
                "Account Coverage",
                "Outreach Consistency",
                "Quote Follow-Up",
                "Proactive Selling",
                "New Business",
            ],
            "sales_outcomes": ["Win Rate", "Revenue", "Quote Volume", "Proactive Conversion", "Strategic Wins"],
        },
        "multiplier_points": {
            "description": "Activity-based points. Offers earn their highest tier only (non-stacking).",
            "buyer_tiers": "1pt base, 3pt quoted, 5pt buy plan, 8pt PO confirmed. Bonus: 0.25pt per RFQ, 2pt per stock list.",
            "sales_tiers": "2pt per quote sent, 8pt per quote won, 1pt per proactive sent, 4pt per proactive converted, 3pt per new account.",
        },
        "bonus_tiers": {
            "description": "Top 3 qualify if minimum activity and Avail Score thresholds are met.",
            "prizes": "1st: $500, 2nd: $250, 3rd: $100",
        },
    }
