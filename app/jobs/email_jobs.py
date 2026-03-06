"""Email, contacts, and calendar background jobs.

Called by: app/jobs/__init__.py via register_email_jobs()
Depends on: app.database, app.models, app.connectors.email_mining, app.services.*
"""

import asyncio
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job
from ..utils.token_manager import _utc


def register_email_jobs(scheduler, settings):
    """Register email/contacts/calendar jobs with the scheduler."""
    if settings.contacts_sync_enabled:
        scheduler.add_job(_job_contacts_sync, IntervalTrigger(hours=24), id="contacts_sync", name="Contacts sync")

    if settings.activity_tracking_enabled and settings.ownership_sweep_enabled:
        scheduler.add_job(_job_ownership_sweep, IntervalTrigger(hours=12), id="ownership_sweep", name="Ownership sweep")
        scheduler.add_job(
            _job_site_ownership_sweep,
            CronTrigger(hour=3, minute=0),
            id="site_ownership_sweep",
            name="Site ownership sweep",
        )
    elif settings.activity_tracking_enabled:
        logger.info("Ownership sweep disabled (OWNERSHIP_SWEEP_ENABLED=false) — activity tracking still active")

    if settings.deep_email_mining_enabled:
        scheduler.add_job(
            _job_deep_email_mining, IntervalTrigger(hours=4), id="deep_email_mining", name="Deep email mining"
        )

    if settings.contact_scoring_enabled:
        scheduler.add_job(
            _job_contact_scoring,
            CronTrigger(hour=2, minute=0),
            id="contact_scoring",
            name="Contact relationship scoring",
        )

    scheduler.add_job(
        _job_contact_status_compute,
        CronTrigger(hour=3, minute=0),
        id="contact_status_compute",
        name="Contact status auto-compute",
    )

    scheduler.add_job(
        _job_email_health_update,
        CronTrigger(hour=1, minute=0),
        id="email_health_update",
        name="Vendor email health scores",
    )

    scheduler.add_job(
        _job_calendar_scan, CronTrigger(hour=6, minute=0), id="calendar_scan", name="Calendar vendor meeting scan"
    )

    if settings.customer_enrichment_enabled:
        scheduler.add_job(
            _job_email_reverification,
            CronTrigger(month="2,5,8,11", day=15, hour=5, minute=0),
            id="email_reverification",
            name="Quarterly email re-verification",
        )


@_traced_job
async def _job_contacts_sync():
    """Sync Outlook contacts for all connected users."""
    from ..database import SessionLocal
    from ..models import User

    # Short-lived session to identify users needing sync
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()
        user_ids = []
        for user in users:
            if not user.access_token or not user.m365_connected:
                continue
            should_sync = False
            if not user.last_contacts_sync:
                should_sync = True
            elif now - _utc(user.last_contacts_sync) > timedelta(hours=24):
                should_sync = True
            if should_sync:
                user_ids.append(user.id)
    except Exception as e:
        logger.error(f"Contacts sync job error: {e}")
        return
    finally:
        db.close()

    # Sync each user with its own session
    for user_id in user_ids:
        sync_db = SessionLocal()
        try:
            user = sync_db.get(User, user_id)
            if not user:
                continue
            await asyncio.wait_for(_sync_user_contacts(user, sync_db), timeout=300)
        except asyncio.TimeoutError:
            logger.warning(f"Contacts sync timed out for user {user_id}")
            sync_db.rollback()
        except Exception as e:
            logger.warning(f"Contacts sync failed for user {user_id}: {e}")
            sync_db.rollback()
        finally:
            sync_db.close()


@_traced_job
async def _job_ownership_sweep():
    """Run customer ownership sweep."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.ownership_service import run_ownership_sweep

        await run_ownership_sweep(db)
    except Exception as e:
        logger.error(f"Ownership sweep error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_site_ownership_sweep():
    """Run site-level ownership sweep (prospecting pool)."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.ownership_service import run_site_ownership_sweep

        run_site_ownership_sweep(db)
    except Exception as e:
        logger.error(f"Site ownership sweep error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_deep_email_mining():
    """Deep email mining scan for all connected users."""
    from ..database import SessionLocal
    from ..models import User
    from ..utils.token_manager import get_valid_token

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()

        from ..connectors.email_mining import EmailMiner
        from ..services.deep_enrichment_service import link_contact_to_entities

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
                                link_contact_to_entities(
                                    db,
                                    email_addr,
                                    {
                                        "full_name": domain_data.get("sender_names", [""])[0]
                                        if domain_data.get("sender_names")
                                        else None,
                                        "confidence": 0.6,
                                    },
                                )
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


@_traced_job
async def _job_contact_scoring():
    """Nightly: compute relationship scores for all vendor contacts."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.contact_intelligence import compute_all_contact_scores

        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, compute_all_contact_scores, db),
            timeout=300,
        )
        logger.info(f"Contact scoring: {result['updated']} updated, {result['skipped']} skipped")
    except asyncio.TimeoutError:
        logger.error("Contact scoring timed out after 300s")
        db.rollback()
    except Exception as e:
        logger.error(f"Contact scoring error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_contact_status_compute():
    """Auto-compute contact_status for SiteContacts based on activity history.

    Rules:
      - Never downgrade 'champion' (manual designation only)
      - Last activity ≤7 days → 'active'
      - Last activity 7-30 days → keep current (no auto-downgrade from active)
      - Last activity 30-90 days → 'quiet'
      - Last activity >90 days → 'inactive'
      - No activity ever → keep 'new' or set 'inactive' if created >90 days ago
    """
    from sqlalchemy import func

    from ..database import SessionLocal
    from ..models import SiteContact
    from ..models.intelligence import ActivityLog

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        # Subquery: most recent activity per site_contact_id
        last_activity_sq = (
            db.query(
                ActivityLog.site_contact_id,
                func.max(ActivityLog.created_at).label("last_at"),
            )
            .filter(ActivityLog.site_contact_id.isnot(None))
            .group_by(ActivityLog.site_contact_id)
            .subquery()
        )

        # Fetch all active site contacts with their last activity
        contacts = (
            db.query(SiteContact, last_activity_sq.c.last_at)
            .outerjoin(last_activity_sq, SiteContact.id == last_activity_sq.c.site_contact_id)
            .filter(SiteContact.is_active.is_(True))
            .all()
        )

        updated = 0
        for sc, last_at in contacts:
            # Never downgrade champion
            if sc.contact_status == "champion":
                continue

            if last_at is not None:
                last_at = _utc(last_at)
                days = (now - last_at).days
                if days <= 7:
                    new_status = "active"
                elif days <= 30:
                    # Don't auto-downgrade active contacts in the 7-30 day window
                    continue
                elif days <= 90:
                    new_status = "quiet"
                else:
                    new_status = "inactive"
            else:
                # No activity ever
                created = _utc(sc.created_at) if sc.created_at else now
                days_since_created = (now - created).days
                if days_since_created > 90:
                    new_status = "inactive"
                else:
                    continue  # Keep 'new' status

            if sc.contact_status != new_status:
                sc.contact_status = new_status
                updated += 1

        db.commit()
        logger.info(f"Contact status compute: {updated} contacts updated out of {len(contacts)}")
    except Exception as e:
        logger.error(f"Contact status compute error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_email_health_update():
    """Daily: recompute email health scores for active vendors."""
    from ..database import SessionLocal
    from ..services.response_analytics import batch_update_email_health

    db = SessionLocal()
    try:
        result = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, batch_update_email_health, db),
            timeout=300,
        )
        logger.info(f"Email health update: {result.get('updated', 0)} vendors scored")
    except asyncio.TimeoutError:
        logger.error("Email health update timed out after 300s")
        db.rollback()
    except Exception as e:
        logger.error(f"Email health update error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_calendar_scan():
    """Daily: scan calendar events for vendor meetings and trade shows."""
    from ..database import SessionLocal
    from ..models import User
    from ..services.calendar_intelligence import scan_calendar_events
    from ..utils.token_manager import get_valid_token

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()
        users_to_scan = [u for u in users if u.access_token and u.m365_connected]
    except Exception as e:
        logger.error(f"Calendar scan user query error: {e}")
        return
    finally:
        db.close()

    sem = asyncio.Semaphore(3)

    async def _safe_cal_scan(user_id):
        async with sem:
            scan_db = SessionLocal()
            try:
                user = scan_db.get(User, user_id)
                if not user:
                    return
                token = await get_valid_token(user, scan_db)
                if not token:
                    logger.warning(f"Calendar scan: no token for user {user.email}")
                    return
                result = await asyncio.wait_for(
                    scan_calendar_events(token, user.id, scan_db, lookback_days=30),
                    timeout=60,
                )
                logger.info(f"Calendar scan [{user.email}]: {result.get('events_found', 0)} events")
            except asyncio.TimeoutError:
                logger.warning(f"Calendar scan TIMEOUT for user {user_id}")
                scan_db.rollback()
            except Exception as e:
                logger.error(f"Calendar scan error for user {user_id}: {e}")
                scan_db.rollback()
            finally:
                scan_db.close()

    if users_to_scan:
        await asyncio.gather(*[_safe_cal_scan(u.id) for u in users_to_scan])


@_traced_job
async def _job_email_reverification():
    """Quarterly: re-verify emails older than 90 days."""
    from ..database import SessionLocal
    from ..services.customer_enrichment_batch import run_email_reverification

    db = SessionLocal()
    try:
        result = await run_email_reverification(db, max_contacts=200)
        db.commit()
        logger.info(
            "Email re-verification: %d processed, %d invalidated",
            result.get("processed", 0),
            result.get("invalidated", 0),
        )
    except Exception as e:
        logger.error(f"Email re-verification error: {e}")
        db.rollback()
    finally:
        db.close()


# ── Inbox Scanning Helpers ──────────────────────────────────────────────


async def _scan_user_inbox(user, db):
    """Scan a single user's inbox for vendor replies and stock lists."""
    from ..config import settings
    from ..email_service import poll_inbox
    from ..utils.token_manager import get_valid_token
    from .inventory_jobs import _scan_stock_list_attachments

    is_backfill = user.last_inbox_scan is None
    if is_backfill:
        logger.info(f"First-time inbox backfill for {user.email} ({settings.inbox_backfill_days} days)")

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


# ── Vendor Contact Mining ───────────────────────────────────────────────


async def _mine_vendor_contacts(user, db, is_backfill: bool = False):
    """Extract vendor contact info from recent emails into VendorCards."""
    from ..config import settings
    from ..connectors.email_mining import EmailMiner
    from ..models import VendorCard
    from ..utils.token_manager import get_valid_token
    from ..vendor_utils import normalize_vendor_name

    lookback = settings.inbox_backfill_days if is_backfill else 1
    fresh_token = await get_valid_token(user, db) or user.access_token
    miner = EmailMiner(fresh_token, db=db, user_id=user.id)
    results = await miner.scan_inbox(lookback_days=lookback, max_messages=200)

    contacts = results.get("contacts_enriched", [])
    if not contacts:
        return

    from ..vendor_utils import merge_emails_into_card, merge_phones_into_card

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


# ── Outbound RFQ Scanning ──────────────────────────────────────────────


async def _scan_outbound_rfqs(user, db, is_backfill: bool = False):
    """Scan Sent Items for AVAIL RFQs and update VendorCard outreach metrics."""
    from ..config import settings
    from ..connectors.email_mining import EmailMiner
    from ..models import VendorCard
    from ..utils.token_manager import get_valid_token

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
        unmatched_prefixes = {d.split(".")[0].lower(): d for d in all_domains if d not in domain_card_map and "." in d}
        if unmatched_prefixes:
            for vc in (
                db.query(VendorCard).filter(VendorCard.normalized_name.in_(list(unmatched_prefixes.keys()))).all()
            ):
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
            logger.info(f"Outbound scan [{user.email}]: {rfqs} RFQs, {updated} vendor cards updated")
    except Exception as e:
        logger.error(f"Outbound scan commit failed for {user.email}: {e}")
        db.rollback()


# ── Engagement Score Computation ───────────────────────────────────────


async def _compute_vendor_scores_job(db):
    """Recompute unified vendor scores for all vendors.

    Called every 12h by the scheduler.
    """
    from ..services.vendor_score import compute_all_vendor_scores

    try:
        result = await compute_all_vendor_scores(db)
        logger.info(f"Vendor scoring complete: {result['updated']} updated, {result['skipped']} skipped")
    except Exception as e:
        logger.error(f"Vendor scoring failed: {e}")


# ── Contacts Sync (Outlook → VendorCards) ───────────────────────────────


async def _sync_user_contacts(user, db):
    """Pull contacts from Outlook into VendorCards using delta query for efficiency."""
    from app.utils.graph_client import GraphClient, GraphSyncStateExpired

    from ..models import SyncState, VendorCard
    from ..vendor_utils import (
        merge_emails_into_card,
        merge_phones_into_card,
        normalize_vendor_name,
    )

    gc = GraphClient(user.access_token)

    # Load delta token for incremental sync
    folder_key = "contacts_sync"
    sync_state = db.query(SyncState).filter(SyncState.user_id == user.id, SyncState.folder == folder_key).first()
    delta_token = sync_state.delta_token if sync_state else None

    try:
        contacts, new_token = await gc.delta_query(
            "/me/contacts/delta",
            delta_token=delta_token,
            params={
                "$select": "displayName,emailAddresses,businessPhones,mobilePhone,companyName",
                "$top": "100",
            },
            max_items=2500,
        )
        # Persist new delta token
        if new_token:
            if sync_state:
                sync_state.delta_token = new_token
                sync_state.last_sync_at = datetime.now(timezone.utc)
            else:
                db.add(
                    SyncState(
                        user_id=user.id,
                        folder=folder_key,
                        delta_token=new_token,
                        last_sync_at=datetime.now(timezone.utc),
                    )
                )
            db.flush()
    except GraphSyncStateExpired:
        logger.warning(f"Contacts delta token expired for {user.email} — full resync")
        if sync_state:
            sync_state.delta_token = None
            db.flush()
        # Fall back to full pull
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
        outlook_emails = [(addr.get("address") or "").strip() for addr in c.get("emailAddresses", [])]
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
        logger.info(f"Contacts sync [{user.email}]: {len(contacts)} contacts, {enriched} new emails")
    except Exception as e:
        logger.error(f"Contacts sync commit failed: {e}")
        db.rollback()
