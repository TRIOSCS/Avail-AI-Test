"""Dashboard leaderboard endpoints — team-leaderboard, unified-leaderboard, reactivation-signals, scoring-info.

Provides leaderboard views, reactivation signals, and scoring system info.

Called by: app/static/app.js (leaderboard tab, reactivation signals)
Depends on: models/auth.py, models/performance.py, services/unified_score_service.py
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy.orm import Session

from ...cache.decorators import cached_endpoint
from ...database import get_db
from ...dependencies import require_user

router = APIRouter()


@router.get("/team-leaderboard")
@cached_endpoint(prefix="team_leaderboard", ttl_hours=0.5, key_params=["role"])
def team_leaderboard(
    role: str = Query(default="buyer", pattern="^(buyer|sales)$"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Combined leaderboard: Avail Score + Multiplier Points + Bonus status.

    Returns one ranked list per role with both scoring systems merged.
    Called by: app.js (team scope leaderboard tab)
    """
    from datetime import date

    from ...models.auth import User
    from ...models.performance import AvailScoreSnapshot, MultiplierScoreSnapshot

    current_month = date.today().replace(day=1)

    # Fetch Avail Scores for this role+month
    avail_rows = (
        db.query(AvailScoreSnapshot, User.name)
        .join(User, User.id == AvailScoreSnapshot.user_id)
        .filter(
            AvailScoreSnapshot.month == current_month,
            AvailScoreSnapshot.role_type == role,
        )
        .all()
    )
    avail_map = {}
    for snap, uname in avail_rows:
        avail_map[snap.user_id] = {
            "user_name": uname,
            "avail_score": snap.total_score or 0,
            "behavior_total": snap.behavior_total or 0,
            "outcome_total": snap.outcome_total or 0,
            "avail_rank": snap.rank,
            "avail_qualified": snap.qualified,
            "avail_bonus": snap.bonus_amount or 0,
            "avail_updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
            # Include full metric breakdown
            **{f"b{i}_score": getattr(snap, f"b{i}_score", 0) or 0 for i in range(1, 6)},
            **{f"b{i}_label": getattr(snap, f"b{i}_label", "") or "" for i in range(1, 6)},
            **{f"b{i}_raw": getattr(snap, f"b{i}_raw", "") or "" for i in range(1, 6)},
            **{f"o{i}_score": getattr(snap, f"o{i}_score", 0) or 0 for i in range(1, 6)},
            **{f"o{i}_label": getattr(snap, f"o{i}_label", "") or "" for i in range(1, 6)},
            **{f"o{i}_raw": getattr(snap, f"o{i}_raw", "") or "" for i in range(1, 6)},
        }

    # Fetch Multiplier Scores for this role+month
    mult_rows = (
        db.query(MultiplierScoreSnapshot, User.name)
        .join(User, User.id == MultiplierScoreSnapshot.user_id)
        .filter(
            MultiplierScoreSnapshot.month == current_month,
            MultiplierScoreSnapshot.role_type == role,
        )
        .all()
    )
    mult_map = {}
    for snap, uname in mult_rows:
        entry = {
            "user_name": uname,
            "total_points": snap.total_points or 0,
            "offer_points": snap.offer_points or 0,
            "bonus_points": snap.bonus_points or 0,
            "mult_rank": snap.rank,
            "mult_qualified": snap.qualified,
            "mult_bonus": snap.bonus_amount or 0,
            "mult_updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
        }
        # Role-specific breakdown
        if role == "buyer":
            entry["breakdown"] = {
                "offers_total": snap.offers_total or 0,
                "offers_base": snap.offers_base_count or 0,
                "offers_quoted": snap.offers_quoted_count or 0,
                "offers_bp": snap.offers_bp_count or 0,
                "offers_po": snap.offers_po_count or 0,
                "pts_base": snap.offers_base_pts or 0,
                "pts_quoted": snap.offers_quoted_pts or 0,
                "pts_bp": snap.offers_bp_pts or 0,
                "pts_po": snap.offers_po_pts or 0,
                "rfqs_sent": snap.rfqs_sent_count or 0,
                "pts_rfqs": snap.rfqs_sent_pts or 0,
                "stock_lists": snap.stock_lists_count or 0,
                "pts_stock": snap.stock_lists_pts or 0,
            }
        else:  # pragma: no cover
            entry["breakdown"] = {
                "quotes_sent": snap.quotes_sent_count or 0,
                "quotes_won": snap.quotes_won_count or 0,
                "pts_quote_sent": snap.quotes_sent_pts or 0,
                "pts_quote_won": snap.quotes_won_pts or 0,
                "proactive_sent": snap.proactive_sent_count or 0,
                "proactive_converted": snap.proactive_converted_count or 0,
                "pts_proactive_sent": snap.proactive_sent_pts or 0,
                "pts_proactive_converted": snap.proactive_converted_pts or 0,
                "new_accounts": snap.new_accounts_count or 0,
                "pts_accounts": snap.new_accounts_pts or 0,
            }
        mult_map[snap.user_id] = entry

    # Merge all user IDs from both systems
    all_uids = set(avail_map.keys()) | set(mult_map.keys())

    # Fetch user roles for trader badge display
    role_map = {}
    if all_uids:
        role_rows = db.query(User.id, User.role).filter(User.id.in_(all_uids)).all()
        role_map = {r.id: r.role for r in role_rows}

    entries = []
    for uid in all_uids:
        av = avail_map.get(uid, {})
        mu = mult_map.get(uid, {})
        name = av.get("user_name") or mu.get("user_name", f"User #{uid}")
        entries.append(
            {
                "user_id": uid,
                "user_name": name,
                "user_role": role_map.get(uid, "buyer"),
                # Avail Score data
                "avail_score": av.get("avail_score", 0),
                "behavior_total": av.get("behavior_total", 0),
                "outcome_total": av.get("outcome_total", 0),
                "avail_rank": av.get("avail_rank"),
                "avail_qualified": av.get("avail_qualified", False),
                "avail_bonus": av.get("avail_bonus", 0),
                # Multiplier data
                "total_points": mu.get("total_points", 0),
                "offer_points": mu.get("offer_points", 0),
                "bonus_points": mu.get("bonus_points", 0),
                "mult_rank": mu.get("mult_rank"),
                "mult_qualified": mu.get("mult_qualified", False),
                "mult_bonus": mu.get("mult_bonus", 0),
                "breakdown": mu.get("breakdown", {}),
                # Full Avail metric breakdown for expandable rows
                **{k: av.get(k, 0) for k in av if k.startswith(("b", "o")) and "_" in k},
                "updated_at": mu.get("mult_updated_at") or av.get("avail_updated_at"),
            }
        )

    # Sort by total_points desc, tiebreak by avail_score
    entries.sort(key=lambda e: (e["total_points"], e["avail_score"]), reverse=True)
    for i, e in enumerate(entries):
        e["rank"] = i + 1

    # Recompute avail_rank and mult_rank from current data so they are
    # consistent with the entries list (snapshot ranks may be stale).
    by_avail = sorted(entries, key=lambda e: e["avail_score"], reverse=True)
    for i, e in enumerate(by_avail):
        e["avail_rank"] = i + 1

    by_mult = sorted(entries, key=lambda e: e["total_points"], reverse=True)
    for i, e in enumerate(by_mult):
        e["mult_rank"] = i + 1

    return {
        "month": current_month.isoformat(),
        "role": role,
        "entries": entries,
    }


@router.get("/reactivation-signals")
def reactivation_signals(
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return active reactivation signals for the sales dashboard."""
    from ...models import ReactivationSignal

    signals = (
        db.query(ReactivationSignal)
        .filter(ReactivationSignal.dismissed_at.is_(None))
        .order_by(ReactivationSignal.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": s.id,
            "company_id": s.company_id,
            "material_card_id": s.material_card_id,
            "signal_type": s.signal_type,
            "reason": s.reason,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in signals
    ]


@router.get("/unified-leaderboard")
@cached_endpoint(prefix="unified_lb", ttl_hours=0.25, key_params=["month"])
def unified_leaderboard(
    month: str = Query(None, description="YYYY-MM format"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Return unified cross-role leaderboard with category breakdowns and AI blurbs."""
    from ...services.unified_score_service import get_unified_leaderboard

    if month:
        try:
            m = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            from fastapi import HTTPException

            raise HTTPException(400, "Invalid month format — use YYYY-MM")
    else:
        m = datetime.now(timezone.utc).date().replace(day=1)

    return get_unified_leaderboard(db, m)


@router.get("/scoring-info")
def scoring_info(user=Depends(require_user)):
    """Return static scoring system explanation for the info pill tooltip."""
    from ...services.unified_score_service import get_scoring_info

    return get_scoring_info()
