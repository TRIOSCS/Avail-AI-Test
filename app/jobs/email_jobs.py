"""Email, contacts, and calendar background jobs.

Called by: app/jobs/__init__.py via register_email_jobs()
Depends on: app.database, app.models, app.connectors.email_mining, app.services.*

Includes:
  - Contacts sync (Outlook -> VendorCards via delta query)
  - Inbox scanning (vendor replies, stock lists, outbound RFQs)
  - Sent folder scanning (track outbound emails, link to requisitions)
  - Deep email mining, contact scoring, calendar scan
  - Email health, reverification, ownership sweep
"""

import asyncio
import re
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

    # Sent folder scan — track outbound emails, link [AVAIL-] tagged messages
    scheduler.add_job(
        _job_scan_sent_folders,
        IntervalTrigger(minutes=30),
        id="scan_sent_folders",
        name="Sent folder scan",
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

        await asyncio.get_running_loop().run_in_executor(None, run_site_ownership_sweep, db)
    except Exception as e:
        logger.error(f"Site ownership sweep error: {e}")
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
            asyncio.get_running_loop().run_in_executor(None, batch_update_email_health, db),
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
    poll_succeeded = False
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
        poll_succeeded = True
    except Exception as e:
        logger.error(f"Inbox poll failed for {user.email}: {e}")

    # Use sequential sub-operations on a single session to avoid concurrent
    # access to the same SQLAlchemy Session object.
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

    async def _safe_excess_bid_scan():
        try:
            await _scan_excess_bid_responses(user, db)
        except Exception as e:
            logger.error(f"Excess bid scan failed for {user.email}: {e}")

    await _safe_stock_scan()
    await _safe_mine_contacts()
    await _safe_outbound_scan()
    await _safe_excess_bid_scan()

    if poll_succeeded:
        user.last_inbox_scan = datetime.now(timezone.utc)
        db.commit()


# ── Excess Bid Response Scanning ────────────────────────────────────────


async def _scan_excess_bid_responses(user, db):
    """Scan inbox for replies to excess bid solicitations.

    Called by: _scan_user_inbox (scheduler, every 30 min)
    Depends on: GraphClient, BidSolicitation, parse_bid_from_email
    """
    from ..config import settings
    from ..models.excess import BidSolicitation
    from ..services.excess_service import parse_bid_from_email
    from ..utils.graph_client import GraphClient
    from ..utils.token_manager import get_valid_token

    if not settings.excess_bid_scan_enabled:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.excess_bid_parse_lookback_days)
    pending = (
        db.query(BidSolicitation.id)
        .filter(
            BidSolicitation.sent_by == user.id,
            BidSolicitation.status == "sent",
            BidSolicitation.sent_at >= cutoff,
        )
        .count()
    )
    if not pending:
        return

    token = await get_valid_token(user, db)
    if not token:
        return

    gc = GraphClient(token)

    try:
        result = await gc.get_json(
            "/me/messages",
            params={
                "$filter": "contains(subject, '[EXCESS-BID-')",
                "$top": "50",
                "$select": "subject,body,receivedDateTime",
                "$orderby": "receivedDateTime desc",
            },
        )
    except Exception as e:
        logger.error("Graph inbox search failed for excess bids ({}): {}", user.email, e)
        return

    messages = result.get("value", [])
    parsed_count = 0

    for msg in messages:
        subject = msg.get("subject", "")
        match = _EXCESS_BID_RE.search(subject)
        if not match:
            continue

        solicitation_id = int(match.group(1))
        solicitation = db.get(BidSolicitation, solicitation_id)
        if not solicitation or solicitation.status != "sent":
            continue

        body_content = msg.get("body", {}).get("content", "")
        if not body_content:
            continue

        try:
            bid = await parse_bid_from_email(db, solicitation_id, body_content)
            if bid:
                parsed_count += 1
                logger.info("Auto-parsed bid for solicitation {} from inbox", solicitation_id)
        except Exception as e:
            logger.warning("Failed to parse bid for solicitation {}: {}", solicitation_id, e)

    if parsed_count:
        logger.info("Parsed {} excess bid responses from inbox for {}", parsed_count, user.email)


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


# ── Sent Folder Scanning ─────────────────────────────────────────────────

# Regex to extract requisition ID from [AVAIL-123] tags in email subjects
_AVAIL_TAG_RE = re.compile(r"\[AVAIL-(\d+)\]")

# Regex to extract solicitation ID from [EXCESS-BID-123] tags in bid reply subjects
_EXCESS_BID_RE = re.compile(r"\[EXCESS-BID-(\d+)\]")


@_traced_job
async def _job_scan_sent_folders():
    """Scan sent folders for all connected users.

    Runs every 30 minutes. For each user with email_scan_enabled (m365_connected), uses
    delta query on SentItems to incrementally find outbound emails. Stores delta token
    per user in SyncState for incremental scanning.
    """
    from ..database import SessionLocal
    from ..models import User

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()
        users_to_scan = [u.id for u in users if u.access_token and u.m365_connected]
    except Exception as e:
        logger.error(f"Sent folder scan user query error: {e}")
        return
    finally:
        db.close()

    for user_id in users_to_scan:
        scan_db = SessionLocal()
        try:
            user = scan_db.get(User, user_id)
            if not user:
                continue
            await asyncio.wait_for(scan_sent_folder(user, scan_db), timeout=120)
        except asyncio.TimeoutError:
            logger.warning(f"Sent folder scan TIMEOUT for user {user_id}")
            scan_db.rollback()
        except Exception as e:
            logger.error(f"Sent folder scan error for user {user_id}: {e}")
            scan_db.rollback()
        finally:
            scan_db.close()


async def scan_sent_folder(user, db):
    """Scan a single user's SentItems folder using delta query.

    For each sent message:
    - If subject contains [AVAIL-{id}], link to that requisition
    - Create ActivityLog with activity_type="email_sent", direction="outbound"
    - Store recipient email for contact matching
    - Detect and flag file attachments for the attachment_parser pipeline

    Called by: _job_scan_sent_folders (scheduler, every 30 min)
    Depends on: GraphClient.delta_query, models.ActivityLog, models.SyncState
    """
    from app.utils.graph_client import GraphClient, GraphSyncStateExpired

    from ..models import SyncState
    from ..models.intelligence import ActivityLog
    from ..utils.token_manager import get_valid_token

    token = await get_valid_token(user, db)
    if not token:
        logger.warning(f"Sent folder scan: no token for {user.email}")
        return []

    gc = GraphClient(token)

    # Load or initialize delta token for SentItems
    folder_key = "sent_items_scan"
    sync_state = db.query(SyncState).filter(SyncState.user_id == user.id, SyncState.folder == folder_key).first()
    delta_token = sync_state.delta_token if sync_state else None

    try:
        messages, new_token = await gc.delta_query(
            "/me/mailFolders/SentItems/messages/delta",
            delta_token=delta_token,
            params={
                "$select": "id,subject,from,toRecipients,sentDateTime,hasAttachments,internetMessageHeaders",
                "$top": "100",
            },
            max_items=500,
        )
    except GraphSyncStateExpired:
        logger.warning(f"Sent folder delta expired for {user.email} — full resync")
        if sync_state:
            sync_state.delta_token = None
            db.flush()
        messages, new_token = await gc.delta_query(
            "/me/mailFolders/SentItems/messages/delta",
            delta_token=None,
            params={
                "$select": "id,subject,from,toRecipients,sentDateTime,hasAttachments,internetMessageHeaders",
                "$top": "100",
            },
            max_items=500,
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

    # Process each sent message
    created_logs = []
    attachment_queue = []
    for msg in messages:
        msg_id = msg.get("id", "")
        subject = msg.get("subject", "")
        sent_dt = msg.get("sentDateTime")

        # Skip if we already logged this message (dedup by external_id)
        existing = (
            db.query(ActivityLog)
            .filter(
                ActivityLog.external_id == msg_id,
                ActivityLog.user_id == user.id,
            )
            .first()
        )
        if existing:
            continue

        # Extract first recipient email
        recipients = msg.get("toRecipients", [])
        first_recipient = ""
        if recipients:
            first_recipient = recipients[0].get("emailAddress", {}).get("address", "")

        # Check for [AVAIL-{id}] tag to link to requisition
        requisition_id = None
        tag_match = _AVAIL_TAG_RE.search(subject)
        if tag_match:
            requisition_id = int(tag_match.group(1))

        # Parse sentDateTime
        occurred_at = None
        if sent_dt:
            try:
                occurred_at = datetime.fromisoformat(sent_dt.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                occurred_at = datetime.now(timezone.utc)

        # Create ActivityLog entry
        log_entry = ActivityLog(
            user_id=user.id,
            activity_type="email_sent",
            channel="email",
            direction="outbound",
            event_type="email",
            subject=subject[:500] if subject else None,
            contact_email=first_recipient or None,
            external_id=msg_id,
            requisition_id=requisition_id,
            auto_logged=True,
            occurred_at=occurred_at,
        )
        db.add(log_entry)
        created_logs.append(log_entry)

        # Check for file attachments (exclude inline images)
        if msg.get("hasAttachments"):
            attachment_info = await detect_attachments(gc, msg_id)
            if attachment_info:
                attachment_queue.append({"message_id": msg_id, "attachments": attachment_info})

    try:
        db.commit()
        if created_logs:
            logger.info(f"Sent folder scan [{user.email}]: {len(created_logs)} outbound emails logged")
        if attachment_queue:
            logger.info(
                f"Sent folder scan [{user.email}]: {len(attachment_queue)} messages queued for attachment parsing"
            )
    except Exception as e:
        logger.error(f"Sent folder scan commit failed for {user.email}: {e}")
        db.rollback()

    return created_logs


# ── Attachment Detection ──────────────────────────────────────────────────


async def detect_attachments(gc, message_id: str) -> list[dict]:
    """Fetch attachment metadata for a message and return file attachments.

    Excludes inline images (contentType starting with 'image/' where isInline is True).
    Returns list of dicts with name, contentType, and size for each file attachment.

    Called by: scan_sent_folder(), _scan_user_inbox()
    Depends on: GraphClient.get_json
    """
    try:
        data = await gc.get_json(
            f"/me/messages/{message_id}/attachments",
            params={"$select": "name,contentType,size,isInline"},
        )
    except Exception as e:
        logger.warning(f"Failed to fetch attachments for message {message_id[:20]}: {e}")
        return []

    attachments = data.get("value", [])
    file_attachments = []
    for att in attachments:
        content_type = (att.get("contentType") or "").lower()
        is_inline = att.get("isInline", False)

        # Skip inline images — they're embedded in the email body, not real attachments
        if is_inline and content_type.startswith("image/"):
            continue

        file_attachments.append(
            {
                "name": att.get("name", ""),
                "content_type": content_type,
                "size": att.get("size", 0),
            }
        )

    return file_attachments
