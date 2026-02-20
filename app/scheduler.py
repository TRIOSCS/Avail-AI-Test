"""Background scheduler — APScheduler-based automated tasks.

Each job runs independently with its own database session. Jobs are configured
with appropriate intervals using APScheduler's AsyncIOScheduler.

Job overview:
  - Auto-archive stale requisitions: every 5 min
  - Token refresh: every 5 min
  - Inbox scan: configurable (default 30 min)
  - Batch results processing: every 5 min
  - Contacts sync: every 24h
  - Engagement scoring: every 12h
  - Webhook subscriptions: every 5 min
  - Ownership sweep: every 12h
  - Routing expiration: every 12h
  - PO verification: configurable (default 30 min)
  - Stock sale auto-complete: daily at configured hour
  - Proactive matching: every 5 min
  - Performance tracking: every 12h
  - Deep email mining: every 4h
  - Deep enrichment sweep: every 12h
"""

import asyncio
import base64
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from .http_client import http

# Global scheduler instance
scheduler = AsyncIOScheduler(
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    }
)


def _utc(dt):
    """Make a naive datetime UTC-aware (no-op if already aware)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ── Token Management ────────────────────────────────────────────────────


async def get_valid_token(user, db) -> str | None:
    """Get a valid Graph API token for user, refreshing if expired/near-expiry.

    Use this before EVERY Graph API call (background or foreground).
    Returns access_token string or None if refresh fails.
    """
    # Check if current token is still valid (with 5-min buffer)
    if user.access_token and user.token_expires_at:
        if datetime.now(timezone.utc) < _utc(user.token_expires_at) - timedelta(minutes=5):
            return user.access_token

    # Token expired or near-expiry — refresh it
    token = await refresh_user_token(user, db)
    if token:
        user.m365_last_healthy = datetime.now(timezone.utc)
        user.m365_error_reason = None
        db.commit()
    else:
        user.m365_error_reason = "Token refresh failed"
        db.commit()
    return token


async def refresh_user_token(user, db) -> str | None:
    """Refresh a single user's Azure token. Returns new access_token or None."""
    from .config import settings

    if not user.refresh_token:
        return None

    result = await _refresh_access_token(
        user.refresh_token,
        settings.azure_client_id,
        settings.azure_client_secret,
        settings.azure_tenant_id,
    )
    if not result:
        user.m365_connected = False
        db.commit()
        logger.warning(f"Token refresh failed for {user.email}")
        return None

    access_token, new_refresh = result
    user.access_token = access_token
    user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    user.m365_connected = True
    if new_refresh:
        user.refresh_token = new_refresh
    db.commit()
    logger.info(f"Token refreshed for {user.email}")
    return access_token


async def _refresh_access_token(
    refresh_token: str, client_id: str, client_secret: str, tenant_id: str
) -> tuple[str, str | None] | None:
    """Use a refresh token to get a new access token from Azure AD.

    Returns (access_token, new_refresh_token_or_None) or None on failure.
    """
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    try:
        r = await http.post(
            token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": "openid profile email offline_access Mail.Send Mail.ReadWrite Contacts.Read MailboxSettings.Read User.Read Calendars.Read ChannelMessage.Send Team.ReadBasic.All",
            },
            timeout=15,
        )

        if r.status_code != 200:
            logger.warning(f"Token refresh failed: {r.status_code} — {r.text[:200]}")
            return None

        tokens = r.json()
        return (tokens.get("access_token"), tokens.get("refresh_token"))

    except Exception as e:
        logger.warning(f"Token refresh error: {e}")
        return None


# ── Scheduler Configuration ────────────────────────────────────────────


def configure_scheduler():
    """Register all background jobs with the APScheduler instance."""
    from .config import settings

    # Core jobs (always run)
    scheduler.add_job(_job_auto_archive, IntervalTrigger(minutes=5),
                      id="auto_archive", name="Auto-archive stale requisitions")
    scheduler.add_job(_job_token_refresh, IntervalTrigger(minutes=5),
                      id="token_refresh", name="Token refresh")
    scheduler.add_job(_job_inbox_scan, IntervalTrigger(minutes=settings.inbox_scan_interval_min),
                      id="inbox_scan", name="Inbox scan")
    scheduler.add_job(_job_batch_results, IntervalTrigger(minutes=5),
                      id="batch_results", name="Process batch results")
    scheduler.add_job(_job_engagement_scoring, IntervalTrigger(hours=12),
                      id="engagement_scoring", name="Engagement scoring")

    # Contacts sync (configurable)
    if settings.contacts_sync_enabled:
        scheduler.add_job(_job_contacts_sync, IntervalTrigger(hours=24),
                          id="contacts_sync", name="Contacts sync")

    # Activity tracking jobs
    if settings.activity_tracking_enabled:
        scheduler.add_job(_job_webhook_subscriptions, IntervalTrigger(minutes=5),
                          id="webhook_subs", name="Webhook subscriptions")
        scheduler.add_job(_job_ownership_sweep, IntervalTrigger(hours=12),
                          id="ownership_sweep", name="Ownership sweep")
        scheduler.add_job(_job_routing_expiration, IntervalTrigger(hours=12),
                          id="routing_expiration", name="Routing expiration")

    # Buy plan jobs
    scheduler.add_job(_job_po_verification, IntervalTrigger(minutes=settings.po_verify_interval_min),
                      id="po_verification", name="PO verification")
    scheduler.add_job(_job_stock_autocomplete,
                      CronTrigger(hour=settings.buyplan_auto_complete_hour,
                                  timezone=settings.buyplan_auto_complete_tz),
                      id="stock_autocomplete", name="Stock sale auto-complete")

    # Proactive matching
    if settings.proactive_matching_enabled:
        scheduler.add_job(_job_proactive_matching, IntervalTrigger(minutes=5),
                          id="proactive_matching", name="Proactive matching")

    # Performance tracking
    scheduler.add_job(_job_performance_tracking, IntervalTrigger(hours=12),
                      id="performance_tracking", name="Performance tracking")

    # Deep enrichment (conditional)
    if settings.deep_email_mining_enabled:
        scheduler.add_job(_job_deep_email_mining, IntervalTrigger(hours=4),
                          id="deep_email_mining", name="Deep email mining")
    if settings.deep_enrichment_enabled:
        scheduler.add_job(_job_deep_enrichment, IntervalTrigger(hours=12),
                          id="deep_enrichment", name="Deep enrichment sweep")

    # Cache cleanup
    scheduler.add_job(_job_cache_cleanup, IntervalTrigger(hours=24),
                      id="cache_cleanup", name="Cache cleanup")

    job_count = len(scheduler.get_jobs())
    logger.info(f"APScheduler configured with {job_count} jobs")


# ── Individual Job Functions ───────────────────────────────────────────


async def _job_auto_archive():
    """Auto-archive stale requisitions (no activity for 30 days)."""
    from .database import SessionLocal
    from .models import Requisition

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=30)
        archived_count = (
            db.query(Requisition)
            .filter(
                Requisition.status == "active",
                Requisition.last_searched_at.isnot(None),
                Requisition.last_searched_at < cutoff,
            )
            .update({"status": "archived"}, synchronize_session="fetch")
        )
        if archived_count:
            db.commit()
            logger.info(f"Auto-archived {archived_count} stale requisition(s)")
    except Exception as e:
        logger.error(f"Auto-archive error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_token_refresh():
    """Refresh tokens for all users with refresh tokens."""
    from .database import SessionLocal
    from .models import User

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()
        users_to_refresh = []
        for user in users:
            needs_refresh = False
            if user.token_expires_at:
                exp = _utc(user.token_expires_at)
                needs_refresh = now > exp - timedelta(minutes=15)
            elif not user.access_token:
                needs_refresh = True

            if needs_refresh:
                users_to_refresh.append(user)

        # Refresh all users in parallel
        async def _safe_refresh(user):
            try:
                await refresh_user_token(user, db)
            except Exception as e:
                logger.error(f"Token refresh error for {user.email}: {e}")

        if users_to_refresh:
            await asyncio.gather(*[_safe_refresh(u) for u in users_to_refresh])
    except Exception as e:
        logger.error(f"Token refresh job error: {e}")
    finally:
        db.close()


async def _job_inbox_scan():
    """Scan inboxes for all connected users."""
    from .config import settings
    from .database import SessionLocal
    from .models import User

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()
        scan_interval = timedelta(minutes=settings.inbox_scan_interval_min)

        users_to_scan = []
        for user in users:
            if not user.access_token or not user.m365_connected:
                continue

            should_scan = False
            if not user.last_inbox_scan:
                should_scan = True
            elif now - _utc(user.last_inbox_scan) > scan_interval:
                should_scan = True

            if should_scan:
                users_to_scan.append(user)

        # Scan all users in parallel with concurrency limit
        sem = asyncio.Semaphore(3)

        async def _safe_scan(user):
            async with sem:
                try:
                    await asyncio.wait_for(_scan_user_inbox(user, db), timeout=90)
                except asyncio.TimeoutError:
                    logger.error(f"Inbox scan TIMEOUT for {user.email} (90s) — skipping")
                    user.m365_error_reason = "Inbox scan timed out"
                    db.commit()
                except Exception as e:
                    logger.error(f"Inbox scan error for {user.email}: {e}")
                    user.m365_error_reason = str(e)[:200]
                    db.commit()
                    db.rollback()

        if users_to_scan:
            await asyncio.gather(*[_safe_scan(u) for u in users_to_scan])
    except Exception as e:
        logger.error(f"Inbox scan job error: {e}")
    finally:
        db.close()


async def _job_batch_results():
    """Process pending AI batch results."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        from .email_service import process_batch_results
        batch_applied = await asyncio.wait_for(process_batch_results(db), timeout=120)
        if batch_applied:
            logger.info(f"Batch processing: {batch_applied} results applied")
    except asyncio.TimeoutError:
        logger.error("Batch results processing timed out (120s)")
        db.rollback()
    except Exception as e:
        logger.error(f"Batch results processing error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_contacts_sync():
    """Sync Outlook contacts for all connected users."""
    from .database import SessionLocal
    from .models import User

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()
        for user in users:
            if not user.access_token or not user.m365_connected:
                continue
            should_sync = False
            if not user.last_contacts_sync:
                should_sync = True
            elif now - _utc(user.last_contacts_sync) > timedelta(hours=24):
                should_sync = True

            if should_sync:
                try:
                    await _sync_user_contacts(user, db)
                except Exception as e:
                    logger.warning(f"Contacts sync failed for {user.email}: {e}")
                    db.rollback()
                    continue
    except Exception as e:
        logger.error(f"Contacts sync job error: {e}")
    finally:
        db.close()


async def _job_engagement_scoring():
    """Compute engagement scores for all vendors with outreach data."""
    from .database import SessionLocal
    from .models import VendorCard

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        latest = (
            db.query(VendorCard.engagement_computed_at)
            .filter(VendorCard.engagement_computed_at.isnot(None))
            .order_by(VendorCard.engagement_computed_at.desc())
            .first()
        )

        should_compute = True
        if latest and latest[0]:
            last_computed = latest[0]
            if last_computed.tzinfo is None:
                last_computed = last_computed.replace(tzinfo=timezone.utc)
            if now - last_computed < timedelta(hours=12):
                should_compute = False

        if should_compute:
            await _compute_engagement_scores_job(db)
    except Exception as e:
        logger.error(f"Engagement scoring error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_webhook_subscriptions():
    """Manage Graph webhook subscriptions."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        from .services.webhook_service import (
            ensure_all_users_subscribed,
            renew_expiring_subscriptions,
        )
        await renew_expiring_subscriptions(db)
        await ensure_all_users_subscribed(db)
    except Exception as e:
        logger.error(f"Webhook subscription error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_ownership_sweep():
    """Run customer ownership sweep."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        from .services.ownership_service import run_ownership_sweep
        await run_ownership_sweep(db)
    except Exception as e:
        logger.error(f"Ownership sweep error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_routing_expiration():
    """Expire stale routing assignments and offers."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        from .services.routing_service import (
            expire_stale_assignments,
            expire_stale_offers,
        )
        expired_assignments = expire_stale_assignments(db)
        expired_offers = expire_stale_offers(db)
        if expired_assignments or expired_offers:
            db.commit()
    except Exception as e:
        logger.error(f"Routing expiration error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_po_verification():
    """Verify PO sent status for pending buy plans."""
    from .database import SessionLocal
    from .models import BuyPlan

    db = SessionLocal()
    try:
        from .services.buyplan_service import verify_po_sent

        unverified_plans = (
            db.query(BuyPlan).filter(BuyPlan.status == "po_entered").all()
        )

        async def _safe_verify(plan):
            try:
                await verify_po_sent(plan, db)
            except Exception as e:
                logger.error(f"PO verify error for plan {plan.id}: {e}")

        if unverified_plans:
            await asyncio.gather(*[_safe_verify(p) for p in unverified_plans])
    except Exception as e:
        logger.error(f"PO verification scan error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_stock_autocomplete():
    """Auto-complete stock sales at configured hour."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        from .services.buyplan_service import auto_complete_stock_sales
        completed = auto_complete_stock_sales(db)
        if completed:
            logger.info(f"Stock sale auto-complete: {completed} plan(s) completed")
    except Exception as e:
        logger.error(f"Stock sale auto-complete error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_proactive_matching():
    """Scan new offers for proactive matching."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        from .services.proactive_service import scan_new_offers_for_matches
        result = scan_new_offers_for_matches(db)
        if result.get("matches_created"):
            logger.info(
                f"Proactive matching: {result['matches_created']} new matches from {result['scanned']} offers"
            )
    except Exception as e:
        logger.error(f"Proactive matching error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_performance_tracking():
    """Compute vendor scorecards and buyer leaderboard."""
    from .database import SessionLocal

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        from .services.performance_service import (
            compute_all_vendor_scorecards,
            compute_buyer_leaderboard,
        )

        loop = asyncio.get_running_loop()
        vs_result = await loop.run_in_executor(None, compute_all_vendor_scorecards, db)
        logger.info(
            f"Vendor scorecards: {vs_result['updated']} updated, "
            f"{vs_result['skipped_cold_start']} cold-start"
        )
        current_month = now.date().replace(day=1)
        bl_result = await loop.run_in_executor(None, compute_buyer_leaderboard, db, current_month)
        logger.info(
            f"Buyer leaderboard: {bl_result['entries']} entries for {current_month}"
        )
        # Recompute previous month during grace period (first 7 days)
        if now.day <= 7:
            prev_month = (current_month - timedelta(days=1)).replace(day=1)
            await loop.run_in_executor(None, compute_buyer_leaderboard, db, prev_month)
    except Exception as e:
        logger.error(f"Performance tracking error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_deep_email_mining():
    """Deep email mining scan for all connected users."""
    from .database import SessionLocal
    from .models import User

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()

        from .connectors.email_mining import EmailMiner
        from .services.deep_enrichment_service import link_contact_to_entities

        users_to_scan = []
        for user in users:
            if not user.access_token or not user.m365_connected:
                continue
            if user.last_deep_email_scan:
                last_scan = _utc(user.last_deep_email_scan)
                if now - last_scan < timedelta(hours=4):
                    continue
            users_to_scan.append(user)

        sem = asyncio.Semaphore(3)

        async def _safe_deep_scan(user):
            async with sem:
                try:
                    token = await get_valid_token(user, db)
                    if not token:
                        return
                    miner = EmailMiner(token, db=db, user_id=user.id)
                    scan_result = await asyncio.wait_for(
                        miner.deep_scan_inbox(lookback_days=30, max_messages=500),
                        timeout=120,
                    )
                    for domain, domain_data in scan_result.get("per_domain", {}).items():
                        for email_addr in domain_data.get("emails", [])[:10]:
                            try:
                                link_contact_to_entities(db, email_addr, {
                                    "full_name": domain_data.get("sender_names", [""])[0] if domain_data.get("sender_names") else None,
                                    "confidence": 0.6,
                                })
                            except Exception:
                                pass

                    user.last_deep_email_scan = now
                    db.commit()
                    logger.info(
                        f"Deep email scan [{user.email}]: {scan_result.get('messages_scanned', 0)} msgs, "
                        f"{scan_result.get('contacts_found', 0)} contacts"
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Deep email scan TIMEOUT for {user.email}")
                except Exception as e:
                    logger.error(f"Deep email scan error for {user.email}: {e}")
                    db.rollback()

        if users_to_scan:
            await asyncio.gather(*[_safe_deep_scan(u) for u in users_to_scan])
    except Exception as e:
        logger.error(f"Deep email mining error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_deep_enrichment():
    """Deep enrichment sweep for vendors and companies."""
    from .config import settings
    from .database import SessionLocal
    from .models import Company, VendorCard

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        from .services.deep_enrichment_service import deep_enrich_company, deep_enrich_vendor

        # Enrich up to 50 vendors per sweep
        stale_vendors = (
            db.query(VendorCard.id)
            .filter(
                (VendorCard.deep_enrichment_at.is_(None)) |
                (VendorCard.deep_enrichment_at < now - timedelta(days=settings.deep_enrichment_stale_days))
            )
            .order_by(VendorCard.sighting_count.desc().nullslast())
            .limit(50)
            .all()
        )

        # Enrich recently created entities (last 24h with no enrichment)
        recent_vendors = (
            db.query(VendorCard.id)
            .filter(
                VendorCard.created_at > now - timedelta(hours=24),
                VendorCard.deep_enrichment_at.is_(None),
            )
            .limit(20)
            .all()
        )

        # Enrich up to 20 companies
        stale_companies = (
            db.query(Company.id)
            .filter(
                (Company.deep_enrichment_at.is_(None)) |
                (Company.deep_enrichment_at < now - timedelta(days=settings.deep_enrichment_stale_days))
            )
            .limit(20)
            .all()
        )

        # Run all enrichments concurrently in batches of 10
        async def _safe_enrich_vendor(vid):
            try:
                db.begin_nested()  # SAVEPOINT for per-vendor isolation
                await deep_enrich_vendor(vid, db)
                db.commit()  # release savepoint
            except Exception as e:
                logger.warning(f"Enrichment sweep vendor {vid} error: {e}")
                db.rollback()  # rollback to savepoint only

        async def _safe_enrich_company(cid):
            try:
                db.begin_nested()  # SAVEPOINT for per-company isolation
                await deep_enrich_company(cid, db)
                db.commit()  # release savepoint
            except Exception as e:
                logger.warning(f"Enrichment sweep company {cid} error: {e}")
                db.rollback()  # rollback to savepoint only

        all_vendor_ids = [vid for (vid,) in stale_vendors] + [vid for (vid,) in recent_vendors]
        # Process in batches of 10 to avoid overwhelming external APIs
        for i in range(0, len(all_vendor_ids), 10):
            batch = all_vendor_ids[i:i + 10]
            tasks = [_safe_enrich_vendor(vid) for vid in batch]
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=300,
            )

        if stale_companies:
            await asyncio.wait_for(
                asyncio.gather(
                    *[_safe_enrich_company(cid) for (cid,) in stale_companies],
                    return_exceptions=True,
                ),
                timeout=300,
            )

        logger.info(
            f"Deep enrichment sweep: {len(stale_vendors)} vendors, "
            f"{len(recent_vendors)} new vendors, {len(stale_companies)} companies"
        )
    except Exception as e:
        logger.error(f"Deep enrichment sweep error: {e}")
        db.rollback()
    finally:
        db.close()


async def _job_cache_cleanup():
    """Clean up expired cache entries."""
    try:
        from .cache.intel_cache import cleanup_expired
        cleanup_expired()
    except Exception as e:
        logger.error(f"Cache cleanup error: {e}")


# ── Inbox Scanning ──────────────────────────────────────────────────────


async def _scan_user_inbox(user, db):
    """Scan a single user's inbox for vendor replies and stock lists."""
    from .config import settings
    from .email_service import poll_inbox

    is_backfill = user.last_inbox_scan is None
    if is_backfill:
        logger.info(
            f"First-time inbox backfill for {user.email} ({settings.inbox_backfill_days} days)"
        )

    # Poll inbox for replies (poll_inbox handles dedup via message_id)
    try:
        token = await get_valid_token(user, db)
        if not token:
            logger.warning(f"Skipping inbox poll for {user.email} — no valid token")
            return
        new_responses = await poll_inbox(
            token=token,
            db=db,
            scanned_by_user_id=user.id,
        )
        if new_responses:
            logger.info(f"Inbox scan [{user.email}]: {len(new_responses)} new responses")
    except Exception as e:
        logger.error(f"Inbox poll failed for {user.email}: {e}")

    # Run independent sub-operations in parallel
    async def _safe_stock_scan():
        try:
            await _scan_stock_list_attachments(user, db, is_backfill)
        except Exception as e:
            logger.error(f"Stock list scan failed for {user.email}: {e}")

    async def _safe_mine_contacts():
        try:
            await _mine_vendor_contacts(user, db, is_backfill)
        except Exception as e:
            logger.error(f"Vendor mining failed for {user.email}: {e}")

    async def _safe_outbound_scan():
        try:
            await _scan_outbound_rfqs(user, db, is_backfill)
        except Exception as e:
            logger.error(f"Outbound scan failed for {user.email}: {e}")

    await asyncio.gather(
        _safe_stock_scan(),
        _safe_mine_contacts(),
        _safe_outbound_scan(),
    )

    user.last_inbox_scan = datetime.now(timezone.utc)
    db.commit()


async def _scan_stock_list_attachments(user, db, is_backfill: bool = False):
    """Find and process stock list attachments from vendor emails."""
    from .config import settings
    from .connectors.email_mining import EmailMiner

    lookback = settings.inbox_backfill_days if is_backfill else 1

    fresh_token = await get_valid_token(user, db) or user.access_token
    miner = EmailMiner(fresh_token, db=db, user_id=user.id)
    stock_emails = await miner.scan_for_stock_lists(lookback_days=lookback)

    if not stock_emails:
        return

    logger.info(
        f"Stock list scan [{user.email}]: found {len(stock_emails)} emails with attachments"
    )

    for email_info in stock_emails:
        for att_info in email_info.get("stock_files", []):
            try:
                await _download_and_import_stock_list(
                    user,
                    db,
                    message_id=att_info["message_id"],
                    attachment_id=att_info["attachment_id"],
                    filename=att_info["filename"],
                    vendor_name=email_info.get("vendor_name", "Unknown"),
                    vendor_email=email_info.get("from_email", ""),
                )
            except Exception as e:
                logger.error(f"Stock list import failed [{att_info.get('filename')}]: {e}")


async def _download_and_import_stock_list(
    user,
    db,
    message_id: str,
    attachment_id: str,
    filename: str,
    vendor_name: str,
    vendor_email: str,
):
    """Download an attachment via Graph API and import as material cards + sightings."""
    from .models import MaterialCard, MaterialVendorHistory
    from .vendor_utils import normalize_vendor_name

    # Extract vendor domain from email for column mapping cache
    vendor_domain = ""
    if vendor_email and "@" in vendor_email:
        vendor_domain = vendor_email.split("@", 1)[1].lower()

    # Download the attachment via GraphClient (H1: immutable IDs, H6: retry)
    from app.utils.graph_client import GraphClient

    dl_token = await get_valid_token(user, db) or user.access_token
    gc = GraphClient(dl_token)
    try:
        att_data = await gc.get_json(
            f"/me/messages/{message_id}/attachments/{attachment_id}"
        )
    except Exception as e:
        logger.warning(f"Attachment download failed: {e}")
        return

    if not att_data or "error" in att_data:
        logger.warning(f"Attachment download error: {att_data}")
        return

    content_bytes = att_data.get("contentBytes")
    if not content_bytes:
        return

    file_bytes = base64.b64decode(content_bytes)

    # H3: Validate file type before parsing
    from app.utils.file_validation import validate_file

    is_valid, detected_type = validate_file(file_bytes, filename)
    if not is_valid:
        logger.warning(f"File validation failed for {filename}: detected {detected_type}")
        return

    # Parse the file — use new AI-powered parser (Upgrade 2), fallback to legacy
    try:
        from app.services.attachment_parser import parse_attachment

        rows = await parse_attachment(
            file_bytes, filename, vendor_domain=vendor_domain, db=db
        )
    except Exception as e:
        logger.warning(f"AI attachment parser failed, using legacy parser: {e}")
        rows = _parse_stock_file(file_bytes, filename)

    if not rows:
        logger.info(f"No valid rows in {filename}")
        return

    # Phase 2B: Classify sender — stock list (vendor) vs excess list (customer)
    sender_match = None
    is_excess_list = False
    source_company_id = None
    if vendor_email:
        from app.services.activity_service import match_email_to_entity

        sender_match = match_email_to_entity(vendor_email, db)
        if sender_match and sender_match["type"] == "company":
            is_excess_list = True
            source_company_id = sender_match["id"]  # noqa: F841
            logger.info(
                f"Excess list detected from company '{sender_match['name']}' ({vendor_email}): {filename}"
            )

    # Import into material cards — batch pre-load for performance
    imported = 0
    norm_vendor = normalize_vendor_name(vendor_name)

    # Pre-load existing MaterialCards in one query instead of per-row
    valid_mpns = list({(row.get("mpn") or "").strip().upper() for row in rows
                       if (row.get("mpn") or "").strip() and len((row.get("mpn") or "").strip()) >= 3})
    card_map = {}
    mvh_map = {}
    if valid_mpns:
        # Batch in chunks of 500 to keep IN clause manageable
        for i in range(0, len(valid_mpns), 500):
            chunk = valid_mpns[i:i + 500]
            for c in db.query(MaterialCard).filter(MaterialCard.normalized_mpn.in_(chunk)).all():
                card_map[c.normalized_mpn] = c
        # Pre-load existing MVH entries for this vendor
        existing_card_ids = [c.id for c in card_map.values()]
        if existing_card_ids:
            for i in range(0, len(existing_card_ids), 500):
                chunk = existing_card_ids[i:i + 500]
                for m in db.query(MaterialVendorHistory).filter(
                    MaterialVendorHistory.material_card_id.in_(chunk),
                    MaterialVendorHistory.vendor_name == norm_vendor,
                ).all():
                    mvh_map[m.material_card_id] = m

    for row in rows:
        mpn = (row.get("mpn") or "").strip().upper()
        if not mpn or len(mpn) < 3:
            continue

        card = card_map.get(mpn)
        if not card:
            card = MaterialCard(
                normalized_mpn=mpn,
                display_mpn=row.get("mpn", mpn).strip(),
                manufacturer=row.get("manufacturer", ""),
                description=row.get("description", ""),
            )
            db.add(card)
            try:
                db.flush()
                card_map[mpn] = card
            except Exception as e:
                logger.debug(f"MaterialCard flush conflict for '{mpn}': {e}")
                db.rollback()
                continue

        # Add/update vendor history (richer fields from Upgrade 2)
        mvh = mvh_map.get(card.id)
        if mvh:
            mvh.last_seen = datetime.now(timezone.utc)
            mvh.times_seen = (mvh.times_seen or 0) + 1
            if row.get("qty"):
                mvh.last_qty = row["qty"]
            if row.get("unit_price"):
                mvh.last_price = row["unit_price"]
            elif row.get("price"):
                mvh.last_price = row["price"]
            if row.get("manufacturer"):
                mvh.last_manufacturer = row["manufacturer"]
        else:
            mvh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name=norm_vendor,
                source_type="excess_list" if is_excess_list else "email_auto_import",
                last_qty=row.get("qty"),
                last_price=row.get("unit_price") or row.get("price"),
                last_manufacturer=row.get("manufacturer", ""),
            )
            db.add(mvh)

        imported += 1

    try:
        db.commit()
        list_type = "excess list" if is_excess_list else "stock list"
        logger.info(f"Auto-imported {imported} parts from {list_type} {filename} ({vendor_name})")
    except Exception as e:
        logger.error(f"Stock list commit failed: {e}")
        db.rollback()
        return

    # Teams: check if imported MPNs match any open requirements
    try:
        from sqlalchemy import func as sa_func

        from app.models import Requirement, Requisition
        from app.services.teams import send_stock_match_alert

        imported_mpns = [r.get("mpn", "").strip().upper() for r in rows if r.get("mpn")]
        if imported_mpns:
            matches = (
                db.query(Requirement.id, Requirement.primary_mpn, Requirement.requisition_id)
                .join(Requisition, Requirement.requisition_id == Requisition.id)
                .filter(
                    Requisition.status.in_(["active", "sourcing", "offers"]),
                    sa_func.upper(Requirement.primary_mpn).in_(imported_mpns),
                )
                .all()
            )
            if matches:
                match_list = [
                    {"mpn": m.primary_mpn, "requirement_id": m.id, "requisition_id": m.requisition_id}
                    for m in matches
                ]
                await send_stock_match_alert(
                    matches=match_list,
                    filename=filename,
                    vendor_name=vendor_name,
                )
    except Exception as e:
        logger.debug(f"Teams stock match check skipped: {e}")


def _parse_stock_file(file_bytes: bytes, filename: str) -> list[dict]:
    """Parse a stock list file (CSV/XLSX) into rows with mpn, qty, price, manufacturer."""
    from .file_utils import normalize_stock_row, parse_tabular_file

    raw_rows = parse_tabular_file(file_bytes, filename)
    rows = []
    for r in raw_rows:
        parsed = normalize_stock_row(r)
        if parsed:
            rows.append(parsed)
    return rows[:5000]  # Cap at 5000 rows per file


# ── Vendor Contact Mining ───────────────────────────────────────────────


async def _mine_vendor_contacts(user, db, is_backfill: bool = False):
    """Extract vendor contact info from recent emails into VendorCards."""
    from .config import settings
    from .connectors.email_mining import EmailMiner
    from .models import VendorCard
    from .vendor_utils import normalize_vendor_name

    lookback = settings.inbox_backfill_days if is_backfill else 1
    fresh_token = await get_valid_token(user, db) or user.access_token
    miner = EmailMiner(fresh_token, db=db, user_id=user.id)
    results = await miner.scan_inbox(lookback_days=lookback, max_messages=200)

    contacts = results.get("contacts_enriched", [])
    if not contacts:
        return

    from .vendor_utils import merge_emails_into_card, merge_phones_into_card

    # Pre-load existing VendorCards in one query instead of per-contact
    norm_names = []
    for contact in contacts:
        vn = contact.get("vendor_name", "")
        if vn:
            norm_names.append(normalize_vendor_name(vn))
    card_map = {}
    if norm_names:
        for c in db.query(VendorCard).filter(VendorCard.normalized_name.in_(norm_names)).all():
            card_map[c.normalized_name] = c

    enriched = 0
    for contact in contacts:
        vendor_name = contact.get("vendor_name", "")
        if not vendor_name:
            continue

        norm = normalize_vendor_name(vendor_name)
        card = card_map.get(norm)
        if not card:
            card = VendorCard(
                normalized_name=norm,
                display_name=vendor_name,
                emails=[],
                phones=[],
                source="email_mining",
            )
            db.add(card)
            try:
                db.flush()
                card_map[norm] = card
            except Exception as e:
                logger.debug(f"VendorCard flush conflict for '{norm}': {e}")
                db.rollback()
                continue

        enriched += merge_emails_into_card(card, contact.get("emails", []))
        merge_phones_into_card(card, contact.get("phones", []))

        websites = contact.get("websites", [])
        if not card.website and websites:
            card.website = f"https://{websites[0]}"

    try:
        db.commit()
        if enriched:
            logger.info(f"Contact mining [{user.email}]: enriched {enriched} contacts")
    except Exception as e:
        logger.error(f"Contact mining commit failed for {user.email}: {e}")
        db.rollback()


# ── Upgrade 3: Outbound RFQ Scanning ──────────────────────────────────


async def _scan_outbound_rfqs(user, db, is_backfill: bool = False):
    """Scan Sent Items for AVAIL RFQs and update VendorCard outreach metrics."""
    from .config import settings
    from .connectors.email_mining import EmailMiner
    from .models import VendorCard

    lookback = settings.inbox_backfill_days if is_backfill else 7
    fresh_token = await get_valid_token(user, db) or user.access_token
    miner = EmailMiner(fresh_token, db=db, user_id=user.id)

    results = await miner.scan_sent_items(lookback_days=lookback, max_messages=300)

    rfqs = results.get("rfqs_detected", 0)
    vendors = results.get("vendors_contacted", {})

    if not vendors:
        return

    # Pre-load VendorCards by domain in one query instead of per-domain
    all_domains = list(vendors.keys())
    domain_card_map = {}
    if all_domains:
        for vc in db.query(VendorCard).filter(VendorCard.domain.in_(all_domains)).all():
            domain_card_map[vc.domain] = vc
        # Also pre-load by normalized name prefix for fallback
        unmatched_prefixes = {
            d.split(".")[0].lower(): d for d in all_domains
            if d not in domain_card_map and "." in d
        }
        if unmatched_prefixes:
            for vc in db.query(VendorCard).filter(
                VendorCard.normalized_name.in_(list(unmatched_prefixes.keys()))
            ).all():
                original_domain = unmatched_prefixes.get(vc.normalized_name)
                if original_domain:
                    domain_card_map[original_domain] = vc

    # Update VendorCard outreach counts
    updated = 0
    for domain, count in vendors.items():
        card = domain_card_map.get(domain)

        if card:
            card.total_outreach = (card.total_outreach or 0) + count
            card.last_contact_at = datetime.now(timezone.utc)
            updated += 1

    try:
        db.commit()
        if updated:
            logger.info(
                f"Outbound scan [{user.email}]: {rfqs} RFQs, {updated} vendor cards updated"
            )
    except Exception as e:
        logger.error(f"Outbound scan commit failed for {user.email}: {e}")
        db.rollback()


# ── Upgrade 4: Engagement Score Computation ───────────────────────────


async def _compute_engagement_scores_job(db):
    """Recompute engagement scores for all vendors with outreach data.

    Called once per day by the scheduler tick.
    """
    from .services.engagement_scorer import compute_all_engagement_scores

    try:
        result = await compute_all_engagement_scores(db)
        logger.info(
            f"Engagement scoring complete: {result['updated']} updated, {result['skipped']} skipped"
        )
    except Exception as e:
        logger.error(f"Engagement scoring failed: {e}")


# ── Contacts Sync (Outlook → VendorCards) ───────────────────────────────


async def _sync_user_contacts(user, db):
    """Pull contacts from Outlook into VendorCards."""
    # Use GraphClient for pagination with retry (H1, H6)
    from app.utils.graph_client import GraphClient

    from .models import VendorCard
    from .vendor_utils import (
        merge_emails_into_card,
        merge_phones_into_card,
        normalize_vendor_name,
    )

    gc = GraphClient(user.access_token)
    try:
        contacts = await gc.get_all_pages(
            "/me/contacts",
            params={
                "$top": "500",
                "$select": "displayName,emailAddresses,businessPhones,mobilePhone,companyName",
            },
            max_items=2500,
        )
    except Exception as e:
        logger.warning(f"Contacts sync failed for {user.email}: {e}")
        return

    enriched = 0

    # Pre-load existing VendorCards in one query instead of per-contact
    sync_norm_names = []
    for c in contacts:
        company = c.get("companyName") or c.get("displayName") or ""
        if company and len(company) >= 2:
            sync_norm_names.append(normalize_vendor_name(company))
    sync_card_map = {}
    if sync_norm_names:
        for vc in db.query(VendorCard).filter(VendorCard.normalized_name.in_(sync_norm_names)).all():
            sync_card_map[vc.normalized_name] = vc

    for c in contacts:
        company = c.get("companyName") or c.get("displayName") or ""
        if not company or len(company) < 2:
            continue

        norm = normalize_vendor_name(company)
        card = sync_card_map.get(norm)
        if not card:
            card = VendorCard(
                normalized_name=norm,
                display_name=company,
                emails=[],
                phones=[],
                source="outlook_contacts",
            )
            db.add(card)
            try:
                db.flush()
                sync_card_map[norm] = card
            except Exception as e:
                logger.debug(f"VendorCard flush conflict for '{norm}': {e}")
                db.rollback()
                continue

        # Merge emails from Outlook contact
        outlook_emails = [
            (addr.get("address") or "").strip() for addr in c.get("emailAddresses", [])
        ]
        enriched += merge_emails_into_card(card, outlook_emails)

        # Merge phones from Outlook contact
        all_phones = list(c.get("businessPhones", []) or [])
        mobile = c.get("mobilePhone")
        if mobile:
            all_phones.append(mobile)
        merge_phones_into_card(card, all_phones)

    try:
        user.last_contacts_sync = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            f"Contacts sync [{user.email}]: {len(contacts)} contacts, {enriched} new emails"
        )
    except Exception as e:
        logger.error(f"Contacts sync commit failed: {e}")
        db.rollback()
