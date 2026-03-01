"""Phase 8 — Monthly prospecting scheduler jobs.

Six jobs that run on a monthly cycle to grow and maintain the prospect pool:
1. discover_prospects    — 1st of month, 9PM UTC
2. enrich_pool           — 2nd of month, 2AM UTC
3. find_contacts         — 3rd of month, 2AM UTC
4. refresh_scores        — 15th of month, 2AM UTC
5. expire_and_resurface  — Last day of month, 9PM UTC
6. pool_health_report    — 1st of month, 8AM UTC

All jobs:
- Check PROSPECTING_ENABLED before running
- Create DiscoveryBatch audit records
- Log start/finish with timing
- Catch all exceptions (never crash the scheduler)
- Are idempotent (safe to re-run)
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.discovery_batch import DiscoveryBatch
from app.models.prospect_account import ProspectAccount
from app.services.prospect_scoring import calculate_fit_score, calculate_readiness_score


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure datetime is tz-aware UTC (SQLite returns naive datetimes)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── Discovery Slice Rotation ─────────────────────────────────────────

DISCOVERY_ROTATION = [
    {
        "segment": "Aerospace & Defense",
        "regions": ["US"],
        "intent_keywords": ["aerospace components", "military electronics", "avionics"],
    },
    {
        "segment": "Aerospace & Defense",
        "regions": ["EU", "Asia"],
        "intent_keywords": ["aerospace components", "military electronics", "avionics"],
    },
    {
        "segment": "Service Supply Chain",
        "regions": ["US"],
        "intent_keywords": ["MRO electronics", "aftermarket components", "service parts"],
    },
    {
        "segment": "Service Supply Chain",
        "regions": ["EU", "Asia"],
        "intent_keywords": ["MRO electronics", "aftermarket components", "service parts"],
    },
    {
        "segment": "EMS / Electronics Mfg",
        "regions": ["US", "EU", "Asia"],
        "intent_keywords": ["PCB assembly", "contract manufacturing", "electronic components"],
    },
    {
        "segment": "Automotive + catch-all",
        "regions": ["US", "EU", "Asia"],
        "intent_keywords": ["automotive electronics", "EV components", "ADAS semiconductors"],
    },
]


def get_next_discovery_slice(db: Session) -> dict:
    """Determine the next segment/region to search based on rotation history.

    Queries the last completed explorium batch, finds its position in the
    rotation, and returns the next slice. Wraps around after month 6.
    """
    last_batch = (
        db.query(DiscoveryBatch)
        .filter(
            DiscoveryBatch.source == "explorium",
            DiscoveryBatch.status == "complete",
        )
        .order_by(DiscoveryBatch.created_at.desc())
        .first()
    )

    if not last_batch or not last_batch.segment:
        return DISCOVERY_ROTATION[0]

    # Find position of last batch in rotation
    for i, slot in enumerate(DISCOVERY_ROTATION):
        if slot["segment"] == last_batch.segment:
            # Check if regions match to get exact position
            last_regions = set(last_batch.regions or [])
            slot_regions = set(slot["regions"])
            if last_regions == slot_regions:
                next_idx = (i + 1) % len(DISCOVERY_ROTATION)
                return DISCOVERY_ROTATION[next_idx]

    # Couldn't match — start from beginning
    return DISCOVERY_ROTATION[0]


# ── Job 1: Discover Prospects ────────────────────────────────────────


async def job_discover_prospects() -> dict:
    """1st of month — run discovery for next segment slice + email mining."""
    if not settings.prospecting_enabled:
        logger.info("Prospecting disabled — skipping discovery")
        return {"skipped": True, "reason": "disabled"}

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        slice_info = get_next_discovery_slice(db)
        batch_id = f"discovery_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}"

        batch = DiscoveryBatch(
            batch_id=batch_id,
            source="explorium",
            segment=slice_info["segment"],
            regions=slice_info["regions"],
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(batch)
        db.commit()

        explorium_count = 0
        email_count = 0

        # Explorium discovery
        try:
            from app.services.prospect_discovery_explorium import run_explorium_discovery_batch

            existing_domains = {d[0] for d in db.query(ProspectAccount.domain).all() if d[0]}
            results = await run_explorium_discovery_batch(batch_id, existing_domains)
            for r in results:
                pa = ProspectAccount(**r.model_dump() if hasattr(r, "model_dump") else r)
                pa.discovery_batch_id = batch.id
                db.add(pa)
            explorium_count = len(results)
            db.commit()
        except Exception as e:
            logger.error("Explorium discovery failed: {}", e)
            db.rollback()

        # Email mining (always runs)
        try:
            from app.services.prospect_discovery_email import run_email_mining_batch
            from app.utils.graph_client import get_graph_client

            graph = get_graph_client()
            email_results = await run_email_mining_batch(batch_id, graph, db)
            for r in email_results:
                pa = ProspectAccount(**r.model_dump() if hasattr(r, "model_dump") else r)
                pa.discovery_batch_id = batch.id
                db.add(pa)
            email_count = len(email_results)
            db.commit()
        except Exception as e:
            logger.error("Email mining failed: {}", e)
            db.rollback()

        # Update batch record
        batch.status = "complete"
        batch.prospects_found = explorium_count + email_count
        batch.prospects_new = explorium_count + email_count
        batch.completed_at = datetime.now(timezone.utc)
        db.commit()

        summary = {
            "batch_id": batch_id,
            "segment": slice_info["segment"],
            "regions": slice_info["regions"],
            "explorium_count": explorium_count,
            "email_count": email_count,
        }
        logger.info(
            "Discovery complete: {} from Explorium, {} from email mining",
            explorium_count,
            email_count,
        )
        return summary

    except Exception as e:
        logger.error("Discovery job failed: {}", e)
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()


# ── Job 2: Enrich Pool ──────────────────────────────────────────────


async def job_enrich_pool() -> dict:
    """2nd of month — enrich signals, similar customers, AI writeups."""
    if not settings.prospecting_enabled:
        logger.info("Prospecting disabled — skipping enrichment")
        return {"skipped": True, "reason": "disabled"}

    try:
        from app.services.prospect_signals import run_signal_enrichment_batch

        result = await run_signal_enrichment_batch(min_fit_score=40)
        logger.info("Pool enrichment complete: {}", result)
        return result
    except Exception as e:
        logger.error("Pool enrichment failed: {}", e)
        return {"error": str(e)}


# ── Job 3: Find Contacts ────────────────────────────────────────────


async def job_find_contacts() -> dict:
    """3rd of month — find procurement contacts for high-fit prospects."""
    if not settings.prospecting_enabled:
        logger.info("Prospecting disabled — skipping contact enrichment")
        return {"skipped": True, "reason": "disabled"}

    try:
        from app.services.prospect_contacts import run_contact_enrichment_batch

        result = await run_contact_enrichment_batch(
            min_fit_score=settings.prospecting_min_fit_for_contacts,
        )
        logger.info(
            "Contacts found for {} prospects, {} verified emails",
            result.get("prospects_processed", 0),
            result.get("total_verified", 0),
        )
        return result
    except Exception as e:
        logger.error("Contact enrichment failed: {}", e)
        return {"error": str(e)}


# ── Job 4: Refresh Scores ───────────────────────────────────────────


async def job_refresh_scores() -> dict:
    """15th of month — re-score all suggested prospects."""
    if not settings.prospecting_enabled:
        logger.info("Prospecting disabled — skipping score refresh")
        return {"skipped": True, "reason": "disabled"}

    from app.database import SessionLocal

    db = None
    try:
        db = SessionLocal()
        prospects = db.query(ProspectAccount).filter(ProspectAccount.status == "suggested").all()

        refreshed = 0
        upgraded = 0
        downgraded = 0

        for p in prospects:
            old_fit = p.fit_score or 0
            old_readiness = p.readiness_score or 0

            # Recalculate fit
            prospect_data = {
                "name": p.name,
                "industry": p.industry,
                "naics_code": p.naics_code,
                "employee_count_range": p.employee_count_range,
                "region": p.region,
            }
            new_fit, reasoning = calculate_fit_score(prospect_data)

            # Recalculate readiness
            signals = p.readiness_signals or {}
            new_readiness, _ = calculate_readiness_score(prospect_data, signals)

            p.fit_score = new_fit
            p.fit_reasoning = reasoning
            p.readiness_score = new_readiness
            refreshed += 1

            composite_old = old_fit * 0.6 + old_readiness * 0.4
            composite_new = new_fit * 0.6 + new_readiness * 0.4

            if composite_new > composite_old + 10:
                upgraded += 1
            elif composite_new < composite_old - 10:
                downgraded += 1

        db.commit()

        summary = {
            "refreshed": refreshed,
            "upgraded": upgraded,
            "downgraded": downgraded,
        }
        logger.info(
            "Refreshed {} prospects, {} moved up, {} moved down",
            refreshed,
            upgraded,
            downgraded,
        )
        return summary

    except Exception as e:
        logger.error("Score refresh failed: {}", e)
        if db:
            db.rollback()
        return {"error": str(e)}
    finally:
        if db:
            db.close()


# ── Job 5: Expire and Resurface ─────────────────────────────────────


async def job_expire_and_resurface() -> dict:
    """Last day of month — expire stale prospects, resurface dismissed ones."""
    if not settings.prospecting_enabled:
        logger.info("Prospecting disabled — skipping expire/resurface")
        return {"skipped": True, "reason": "disabled"}

    from app.database import SessionLocal

    db = None
    try:
        db = SessionLocal()
        now = datetime.now(timezone.utc)
        expire_cutoff = now - timedelta(days=settings.prospecting_expire_days)
        enrich_cutoff = now - timedelta(days=60)

        # EXPIRE: old, stale, low-readiness prospects
        candidates = (
            db.query(ProspectAccount)
            .filter(
                ProspectAccount.status == "suggested",
                ProspectAccount.created_at < expire_cutoff,
            )
            .all()
        )

        expired_count = 0
        for p in candidates:
            # Don't expire if enriched recently
            if p.last_enriched_at and _ensure_utc(p.last_enriched_at) > enrich_cutoff:
                continue
            # Don't expire high-readiness
            if (p.readiness_score or 0) > 60:
                continue
            # Don't expire active intent signals
            signals = p.readiness_signals or {}
            intent = signals.get("intent", {})
            if isinstance(intent, dict) and intent.get("strength") in ("strong", "moderate"):
                continue

            p.status = "expired"
            expired_count += 1

        db.commit()

        # RESURFACE: dismissed/expired with new signals
        resurface_candidates = (
            db.query(ProspectAccount)
            .filter(
                ProspectAccount.status.in_(["dismissed", "expired"]),
                ProspectAccount.last_enriched_at.isnot(None),
                ProspectAccount.last_enriched_at > now - timedelta(days=30),
            )
            .all()
        )

        resurfaced_count = 0
        for p in resurface_candidates:
            signals = p.readiness_signals or {}
            intent = signals.get("intent", {})
            hiring = signals.get("hiring", {})
            has_fresh_signals = (isinstance(intent, dict) and intent.get("strength") in ("strong", "moderate")) or (
                isinstance(hiring, dict) and hiring.get("type")
            )
            if has_fresh_signals and (p.readiness_score or 0) >= 40:
                p.status = "suggested"
                p.dismissed_by = None
                p.dismissed_at = None
                p.dismiss_reason = None
                resurfaced_count += 1

        db.commit()

        summary = {"expired": expired_count, "resurfaced": resurfaced_count}
        logger.info("Expired {}, resurfaced {}", expired_count, resurfaced_count)
        return summary

    except Exception as e:
        logger.error("Expire/resurface failed: {}", e)
        if db:
            db.rollback()
        return {"error": str(e)}
    finally:
        if db:
            db.close()


# ── Job 6: Pool Health Report ────────────────────────────────────────


async def job_pool_health_report() -> dict:
    """1st of month 8AM — log pool statistics for awareness."""
    from app.database import SessionLocal

    db = None
    try:
        db = SessionLocal()
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Status breakdown
        status_counts = dict(
            db.query(ProspectAccount.status, func.count(ProspectAccount.id)).group_by(ProspectAccount.status).all()
        )

        # Source breakdown
        source_counts = dict(
            db.query(ProspectAccount.discovery_source, func.count(ProspectAccount.id))
            .group_by(ProspectAccount.discovery_source)
            .all()
        )

        # Region breakdown (suggested only)
        region_counts = dict(
            db.query(ProspectAccount.region, func.count(ProspectAccount.id))
            .filter(ProspectAccount.status == "suggested")
            .group_by(ProspectAccount.region)
            .all()
        )

        # This month's activity
        claimed_this_month = (
            db.query(func.count(ProspectAccount.id))
            .filter(
                ProspectAccount.status == "claimed",
                ProspectAccount.claimed_at >= month_start,
            )
            .scalar()
            or 0
        )
        dismissed_this_month = (
            db.query(func.count(ProspectAccount.id))
            .filter(
                ProspectAccount.status == "dismissed",
                ProspectAccount.dismissed_at >= month_start,
            )
            .scalar()
            or 0
        )

        # Credit usage from recent batches
        credits_used = (
            db.query(func.sum(DiscoveryBatch.credits_used)).filter(DiscoveryBatch.created_at >= month_start).scalar()
            or 0
        )

        report = {
            "by_status": status_counts,
            "by_source": source_counts,
            "by_region": region_counts,
            "claimed_this_month": claimed_this_month,
            "dismissed_this_month": dismissed_this_month,
            "credits_used_this_month": credits_used,
        }

        logger.info(
            "Pool health: {} by status, claimed={}, dismissed={}, credits={}",
            status_counts,
            claimed_this_month,
            dismissed_this_month,
            credits_used,
        )
        return report

    except Exception as e:
        logger.error("Pool health report failed: {}", e)
        return {"error": str(e)}
    finally:
        if db:
            db.close()
