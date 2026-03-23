"""ICsource admin API endpoints.

Thin router for monitoring and managing the ICS search queue.
Provides queue stats, item listing, force-search, skip, and worker health.

Called by: AVAIL admin UI, monitoring dashboards
Depends on: ics_worker.queue_manager, database
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_admin
from app.models import IcsSearchQueue, IcsWorkerStatus
from app.models.auth import User
from app.services.ics_worker.queue_manager import get_queue_stats, mark_status

router = APIRouter(tags=["ics-admin"])


@router.get("/api/ics/queue/stats")
async def ics_queue_stats(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return ICS search queue statistics by status."""
    return get_queue_stats(db)


@router.get("/api/ics/queue/items")
async def ics_queue_items(
    status: str = "queued",
    limit: int = 50,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List ICS queue items filtered by status."""
    items = (
        db.query(IcsSearchQueue)
        .filter(IcsSearchQueue.status == status)
        .order_by(IcsSearchQueue.priority.asc(), IcsSearchQueue.created_at.asc())
        .limit(min(limit, 200))
        .all()
    )
    return [
        {
            "id": item.id,
            "mpn": item.mpn,
            "manufacturer": item.manufacturer,
            "status": item.status,
            "priority": item.priority,
            "gate_decision": item.gate_decision,
            "gate_reason": item.gate_reason,
            "commodity_class": item.commodity_class,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "last_searched_at": item.last_searched_at.isoformat() if item.last_searched_at else None,
            "results_count": item.results_count,
            "error_message": item.error_message,
        }
        for item in items
    ]


@router.post("/api/ics/queue/{item_id}/force-search")
async def ics_force_search(
    item_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Force a queue item to be searched (re-queue regardless of status)."""
    item = db.get(IcsSearchQueue, item_id)
    if not item:
        raise HTTPException(404, "Queue item not found")
    mark_status(db, item, "queued")
    logger.info("ICS admin: force-search for queue item {} (mpn={})", item_id, item.mpn)
    return {"ok": True, "id": item_id, "status": "queued"}


@router.post("/api/ics/queue/{item_id}/skip")
async def ics_skip(
    item_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Manually skip a queue item (set to gated_out)."""
    item = db.get(IcsSearchQueue, item_id)
    if not item:
        raise HTTPException(404, "Queue item not found")
    item.gate_decision = "skip"
    item.gate_reason = "Manually skipped by admin"
    mark_status(db, item, "gated_out")
    logger.info("ICS admin: skip queue item {} (mpn={})", item_id, item.mpn)
    return {"ok": True, "id": item_id, "status": "gated_out"}


@router.get("/api/ics/worker/health")
async def ics_worker_health(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return ICS worker health status including queue stats and circuit breaker."""
    stats = get_queue_stats(db)
    ws = db.get(IcsWorkerStatus, 1)

    if not ws:
        return {
            "worker_status": "unknown",
            "last_search_at": None,
            "searches_today": 0,
            "sightings_today": 0,
            "circuit_breaker": {"is_open": False, "trip_reason": None},
            "queue_stats": stats,
        }

    # Determine overall status — circuit breaker takes priority
    if ws.circuit_breaker_open:
        status = "circuit_breaker_open"
    elif ws.is_running:
        status = "running"
    else:
        status = "stopped"

    return {
        "worker_status": status,
        "last_heartbeat": ws.last_heartbeat.isoformat() if ws.last_heartbeat else None,
        "last_search_at": ws.last_search_at.isoformat() if ws.last_search_at else None,
        "searches_today": ws.searches_today or 0,
        "sightings_today": ws.sightings_today or 0,
        "circuit_breaker": {
            "is_open": ws.circuit_breaker_open or False,
            "trip_reason": ws.circuit_breaker_reason,
        },
        "queue_stats": stats,
    }
