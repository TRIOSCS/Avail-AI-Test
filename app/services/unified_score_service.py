"""Unified Score Service — cross-role normalized scoring for combined leaderboard.

Maps AvailScore metrics (b1-b5, o1-o5) into 5 universal categories, normalizes
to 0-100%, and applies weights to produce a single unified score. Traders average
buyer + sales category percentages. AI blurbs cached with 2-hour TTL.

Category weights:
  Prospecting 20% | Execution 25% | Follow-Through 20% | Closing 25% | Depth 10%

Called by: scheduler.py (daily, after multiplier scores), routers/performance.py (refresh)
Depends on: models/performance.py (AvailScoreSnapshot, MultiplierScoreSnapshot),
            models/unified_score.py (UnifiedScoreSnapshot),
            utils/claude_client.py (claude_structured for blurbs)
"""

from loguru import logger
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_
from sqlalchemy.orm import Session

from ..models import User
from ..models.performance import AvailScoreSnapshot, MultiplierScoreSnapshot
from ..models.unified_score import UnifiedScoreSnapshot


# ── Category Weights ────────────────────────────────────────────────
CATEGORY_WEIGHTS = {
    "prospecting": 0.20,
    "execution": 0.25,
    "followthrough": 0.20,
    "closing": 0.25,
    "depth": 0.10,
}

# Max possible raw points per category per role (sum of max metric scores)
# Buyer:  b2(10)+b5(10)=20, b1(10)+b4(10)+o1(10)=30, b3(10)+o2(10)=20, o3(10)+o4(10)=20, o5(10)=10
# Sales:  b1(10)+b5(10)=20, b2(10)+b4(10)+o3(10)=30, b3(10)+o4(10)=20, o1(10)+o2(10)+o5(10)=30, o5(10)=10


def _buyer_categories(snap: AvailScoreSnapshot) -> dict[str, float]:
    """Extract 5 category percentages from a buyer AvailScoreSnapshot.

    Buyer metric mapping:
      Prospecting:    B2 Multi-Source + B5 Stock Lists → /20
      Execution:      B1 Speed to Source + B4 Pipeline Hygiene + O1 Sourcing Ratio → /30
      Follow-Through: B3 Vendor Follow-Up + O2 Offer→Quote → /20
      Closing:        O3 Win Rate + O4 BP Completion → /20
      Depth:          O5 Vendor Diversity → /10
    """
    b1 = snap.b1_score or 0
    b2 = snap.b2_score or 0
    b3 = snap.b3_score or 0
    b4 = snap.b4_score or 0
    b5 = snap.b5_score or 0
    o1 = snap.o1_score or 0
    o2 = snap.o2_score or 0
    o3 = snap.o3_score or 0
    o4 = snap.o4_score or 0
    o5 = snap.o5_score or 0

    return {
        "prospecting": _safe_pct(b2 + b5, 20),
        "execution": _safe_pct(b1 + b4 + o1, 30),
        "followthrough": _safe_pct(b3 + o2, 20),
        "closing": _safe_pct(o3 + o4, 20),
        "depth": _safe_pct(o5, 10),
    }


def _sales_categories(snap: AvailScoreSnapshot) -> dict[str, float]:
    """Extract 5 category percentages from a sales AvailScoreSnapshot.

    Sales metric mapping:
      Prospecting:    B1 Account Coverage + B5 New Biz → /20
      Execution:      B2 Outreach Consistency + B4 Proactive Selling + O3 Quote Volume → /30
      Follow-Through: B3 Quote Follow-Up + O4 Proactive Conversion → /20
      Closing:        O1 Win Rate + O2 Revenue + O5 Strategic Wins → /30
      Depth:          O5 Strategic Wins → /10
    """
    b1 = snap.b1_score or 0
    b2 = snap.b2_score or 0
    b3 = snap.b3_score or 0
    b4 = snap.b4_score or 0
    b5 = snap.b5_score or 0
    o1 = snap.o1_score or 0
    o2 = snap.o2_score or 0
    o3 = snap.o3_score or 0
    o4 = snap.o4_score or 0
    o5 = snap.o5_score or 0

    return {
        "prospecting": _safe_pct(b1 + b5, 20),
        "execution": _safe_pct(b2 + b4 + o3, 30),
        "followthrough": _safe_pct(b3 + o4, 20),
        "closing": _safe_pct(o1 + o2 + o5, 30),
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
        return {
            k: (buyer_cats[k] + sales_cats[k]) / 2
            for k in CATEGORY_WEIGHTS
        }
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
    avail_snaps = (
        db.query(AvailScoreSnapshot)
        .filter(AvailScoreSnapshot.month == month)
        .all()
    )
    # Index by (user_id, role_type)
    avail_idx: dict[tuple[int, str], AvailScoreSnapshot] = {}
    for s in avail_snaps:
        avail_idx[(s.user_id, s.role_type)] = s

    # Load MultiplierScoreSnapshots for cached display
    mult_snaps = (
        db.query(MultiplierScoreSnapshot)
        .filter(MultiplierScoreSnapshot.month == month)
        .all()
    )
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
        if user.role == "trader":
            cats = _merge_trader_categories(buyer_cats, sales_cats)
            primary_role = "trader"
        elif user.role in ("sales", "manager"):
            cats = sales_cats or {k: 0.0 for k in CATEGORY_WEIGHTS}
            primary_role = "sales"
        else:
            cats = buyer_cats or {k: 0.0 for k in CATEGORY_WEIGHTS}
            primary_role = "buyer"

        score = _weighted_score(cats)

        # Get cached source scores
        buyer_mult = mult_idx.get((user.id, "buyer"))
        sales_mult = mult_idx.get((user.id, "sales"))

        results.append({
            "user_id": user.id,
            "user_name": user.name,
            "primary_role": primary_role,
            "cats": cats,
            "score": round(score, 2),
            "avail_score_buyer": buyer_snap.total_score if buyer_snap else None,
            "avail_score_sales": sales_snap.total_score if sales_snap else None,
            "multiplier_points_buyer": buyer_mult.total_points if buyer_mult else None,
            "multiplier_points_sales": sales_mult.total_points if sales_mult else None,
        })

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

        snap.prospecting_pct = round(r["cats"]["prospecting"], 2)
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
                r["user_name"], r["primary_role"], r["cats"],
                r["score"], r["rank"], total,
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
        f"Prospecting: {cats['prospecting']:.0f}%, Execution: {cats['execution']:.0f}%, "
        f"Follow-Through: {cats['followthrough']:.0f}%, Closing: {cats['closing']:.0f}%, "
        f"Depth: {cats['depth']:.0f}%\n"
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
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    claude_structured(user_msg, schema, system=system, model_tier="fast", max_tokens=256),
                )
                return future.result(timeout=30)
        else:
            return asyncio.run(
                claude_structured(user_msg, schema, system=system, model_tier="fast", max_tokens=256)
            )
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
    avail_snaps = (
        db.query(AvailScoreSnapshot)
        .filter(AvailScoreSnapshot.month == month)
        .all()
    )
    avail_idx: dict[tuple[int, str], AvailScoreSnapshot] = {}
    for s in avail_snaps:
        avail_idx[(s.user_id, s.role_type)] = s

    mult_snaps = (
        db.query(MultiplierScoreSnapshot)
        .filter(MultiplierScoreSnapshot.month == month)
        .all()
    )
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

        entries.append(entry)

    return {"month": month.isoformat(), "entries": entries}


def get_scoring_info() -> dict:
    """Return static scoring system explanation for the info pill tooltip."""
    return {
        "categories": [
            {"name": "Prospecting", "weight": 20, "description": "Building pipeline & vendor network"},
            {"name": "Execution", "weight": 25, "description": "Speed and volume of core work"},
            {"name": "Follow-Through", "weight": 20, "description": "Persistence and deal progression"},
            {"name": "Closing", "weight": 25, "description": "Win rate and revenue generation"},
            {"name": "Depth", "weight": 10, "description": "Strategic breadth and vendor diversity"},
        ],
        "total_range": "0-100",
        "normalization": "Scores are normalized by role so buyers, sales, and traders compete fairly.",
        "ai_refresh": "AI insights refresh every 2 hours.",
    }
