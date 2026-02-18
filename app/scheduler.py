"""Background scheduler — automated M365 integration tasks.

Runs on a 5-minute tick loop. Each tick checks what needs to run:
  - Token refresh: Every 30 min — keeps all users' Azure tokens valid
  - Inbox scan: Every 30 min — scans all users' inboxes for vendor replies + stock lists
  - Contacts sync: Every 24h — pulls Outlook contacts into VendorCards
"""

import asyncio
import base64
import logging
from datetime import datetime, timezone, timedelta

from .http_client import http

log = logging.getLogger(__name__)


def _utc(dt):
    """Make a naive datetime UTC-aware (no-op if already aware)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# v1.3.0: Track last ownership sweep run (simple module-level timestamp)
_last_ownership_sweep = datetime.min.replace(tzinfo=timezone.utc)
_last_performance_compute = datetime.min.replace(tzinfo=timezone.utc)
_last_deep_email_scan = datetime.min.replace(tzinfo=timezone.utc)
_last_deep_enrichment_sweep = datetime.min.replace(tzinfo=timezone.utc)

# Buy plan: PO verification interval & stock sale auto-complete
_last_po_verify = datetime.min.replace(tzinfo=timezone.utc)
_last_stock_autocomplete_date = None  # date object — prevents double-runs

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
        log.warning(f"Token refresh failed for {user.email}")
        return None

    access_token, new_refresh = result
    user.access_token = access_token
    user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    user.m365_connected = True
    if new_refresh:
        user.refresh_token = new_refresh
    db.commit()
    log.info(f"Token refreshed for {user.email}")
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
            log.warning(f"Token refresh failed: {r.status_code} — {r.text[:200]}")
            return None

        tokens = r.json()
        return (tokens.get("access_token"), tokens.get("refresh_token"))

    except Exception as e:
        log.warning(f"Token refresh error: {e}")
        return None


# ── Main Scheduler Loop ─────────────────────────────────────────────────


async def start_scheduler():
    """Launch the background scheduler loop. Call once on app startup."""
    from .config import settings

    log.info(
        f"Background scheduler started — inbox scan every {settings.inbox_scan_interval_min} min"
    )

    # Wait 10 seconds on startup before first tick (let app fully boot)
    await asyncio.sleep(10)

    while True:
        try:
            await _scheduler_tick()
        except Exception as e:
            log.error(f"Scheduler tick error: {e}")
        await asyncio.sleep(300)  # Check every 5 minutes


async def _scheduler_tick():
    """Check what tasks need to run this tick."""
    from .config import settings
    from .database import SessionLocal
    from .models import User, Requisition

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        # ── Auto-archive stale requisitions (runs every tick, no auth needed) ──
        try:
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
                log.info(f"Auto-archived {archived_count} stale requisition(s)")
        except Exception as e:
            log.error(f"Auto-archive error: {e}")
            db.rollback()

        users = db.query(User).filter(User.refresh_token.isnot(None)).all()
        if not users:
            log.debug(
                "Scheduler tick: no users with refresh tokens — skipping email tasks"
            )
            return

        scan_interval = timedelta(minutes=settings.inbox_scan_interval_min)
        log.debug(f"Scheduler tick: {len(users)} user(s) with refresh tokens")

        # ── Token refresh (all users, every 30 min) ──
        for user in users:
            needs_refresh = False
            if user.token_expires_at:
                exp = (
                    user.token_expires_at.replace(tzinfo=timezone.utc)
                    if user.token_expires_at.tzinfo is None
                    else user.token_expires_at
                )
                needs_refresh = now > exp - timedelta(minutes=15)
            elif not user.access_token:
                needs_refresh = True

            if needs_refresh:
                try:
                    await refresh_user_token(user, db)
                except Exception as e:
                    log.error(f"Token refresh error for {user.email}: {e}")

        # ── Inbox scan (all users, every 30 min) ── with per-user timeout
        for user in users:
            if not user.access_token or not user.m365_connected:
                continue

            should_scan = False
            if not user.last_inbox_scan:
                should_scan = True  # First-time — will do backfill
            elif now - _utc(user.last_inbox_scan) > scan_interval:
                should_scan = True

            if should_scan:
                try:
                    await asyncio.wait_for(_scan_user_inbox(user, db), timeout=90)
                except asyncio.TimeoutError:
                    log.error(f"Inbox scan TIMEOUT for {user.email} (90s) — skipping")
                    user.m365_error_reason = "Inbox scan timed out"
                    db.commit()
                except Exception as e:
                    log.error(f"Inbox scan error for {user.email}: {e}")
                    user.m365_error_reason = str(e)[:200]
                    db.commit()
                    db.rollback()

        # ── Process pending AI batches (every tick) ──
        try:
            from .email_service import process_batch_results

            batch_applied = await process_batch_results(db)
            if batch_applied:
                log.info(f"Batch processing: {batch_applied} results applied")
        except Exception as e:
            log.error(f"Batch results processing error: {e}")
            db.rollback()

        # ── Contacts sync (all users, every 24h) ──
        if settings.contacts_sync_enabled:
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
                        log.error(f"Contacts sync error for {user.email}: {e}")
                        db.rollback()

        # ── Upgrade 4: Engagement scoring (daily) ──
        try:
            from .models import VendorCard

            # Check if we've computed today already
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
            log.error(f"Engagement scoring error: {e}")
            db.rollback()

        # ── v1.3.0: Graph webhook subscriptions (every tick) ──
        if settings.activity_tracking_enabled:
            try:
                from .services.webhook_service import (
                    ensure_all_users_subscribed,
                    renew_expiring_subscriptions,
                )

                await renew_expiring_subscriptions(db)
                await ensure_all_users_subscribed(db)
            except Exception as e:
                log.error(f"Webhook subscription error: {e}")
                db.rollback()

        # ── v1.3.0: Customer ownership sweep (daily) ──
        if settings.activity_tracking_enabled:
            try:
                from .services.ownership_service import run_ownership_sweep

                global _last_ownership_sweep
                if now - _last_ownership_sweep > timedelta(hours=12):
                    await run_ownership_sweep(db)
                    _last_ownership_sweep = now
            except Exception as e:
                log.error(f"Ownership sweep error: {e}")
                db.rollback()

        # ── v1.3.0: Routing & offer expiration sweeps (daily) ──
        if settings.activity_tracking_enabled:
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
                log.error(f"Routing expiration error: {e}")
                db.rollback()

        # ── Buy Plan PO verification scan (every po_verify_interval_min) ──
        try:
            global _last_po_verify
            po_interval = timedelta(minutes=settings.po_verify_interval_min)
            if now - _last_po_verify >= po_interval:
                from .models import BuyPlan
                from .services.buyplan_service import verify_po_sent

                unverified_plans = (
                    db.query(BuyPlan).filter(BuyPlan.status == "po_entered").all()
                )
                for plan in unverified_plans:
                    try:
                        await verify_po_sent(plan, db)
                    except Exception as e:
                        log.error(f"PO verify error for plan {plan.id}: {e}")
                _last_po_verify = now
        except Exception as e:
            log.error(f"PO verification scan error: {e}")
            db.rollback()

        # ── Stock sale auto-complete safety net (daily at configured hour) ──
        try:
            global _last_stock_autocomplete_date
            from zoneinfo import ZoneInfo

            local_tz = ZoneInfo(settings.buyplan_auto_complete_tz)
            local_now = now.astimezone(local_tz)
            target_hour = settings.buyplan_auto_complete_hour
            today_local = local_now.date()

            if (
                local_now.hour >= target_hour
                and _last_stock_autocomplete_date != today_local
            ):
                from .services.buyplan_service import auto_complete_stock_sales

                completed = auto_complete_stock_sales(db)
                _last_stock_autocomplete_date = today_local
                if completed:
                    log.info(f"Stock sale auto-complete: {completed} plan(s) completed")
        except Exception as e:
            log.error(f"Stock sale auto-complete error: {e}")
            db.rollback()

        # ── Proactive offer matching (every tick) ──
        if settings.proactive_matching_enabled:
            try:
                from .services.proactive_service import scan_new_offers_for_matches

                result = scan_new_offers_for_matches(db)
                if result.get("matches_created"):
                    log.info(
                        f"Proactive matching: {result['matches_created']} new matches from {result['scanned']} offers"
                    )
            except Exception as e:
                log.error(f"Proactive matching error: {e}")
                db.rollback()

        # ── Performance tracking: vendor scorecards + buyer leaderboard (daily) ──
        try:
            global _last_performance_compute
            if now - _last_performance_compute > timedelta(hours=12):
                from .services.performance_service import (
                    compute_all_vendor_scorecards,
                    compute_buyer_leaderboard,
                )

                vs_result = compute_all_vendor_scorecards(db)
                log.info(
                    f"Vendor scorecards: {vs_result['updated']} updated, "
                    f"{vs_result['skipped_cold_start']} cold-start"
                )
                current_month = now.date().replace(day=1)
                bl_result = compute_buyer_leaderboard(db, current_month)
                log.info(
                    f"Buyer leaderboard: {bl_result['entries']} entries for {current_month}"
                )
                # Recompute previous month during grace period (first 7 days)
                if now.day <= 7:
                    prev_month = (current_month - timedelta(days=1)).replace(day=1)
                    compute_buyer_leaderboard(db, prev_month)
                _last_performance_compute = now
        except Exception as e:
            log.error(f"Performance tracking error: {e}")
            db.rollback()

        # ── Deep email mining (every 4 hours, per user) ──
        if settings.deep_email_mining_enabled:
            try:
                global _last_deep_email_scan
                if now - _last_deep_email_scan > timedelta(hours=4):
                    from .connectors.email_mining import EmailMiner
                    from .services.signature_parser import extract_signature, cache_signature_extract
                    from .services.deep_enrichment_service import link_contact_to_entities

                    for user in users:
                        if not user.access_token or not user.m365_connected:
                            continue

                        # Check per-user staleness
                        if user.last_deep_email_scan:
                            last_scan = _utc(user.last_deep_email_scan)
                            if now - last_scan < timedelta(hours=4):
                                continue

                        try:
                            token = await get_valid_token(user, db)
                            if not token:
                                continue
                            miner = EmailMiner(token, db=db, user_id=user.id)
                            scan_result = await asyncio.wait_for(
                                miner.deep_scan_inbox(lookback_days=30, max_messages=500),
                                timeout=120,
                            )
                            # Process per-domain results for contact linking
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
                            log.info(
                                f"Deep email scan [{user.email}]: {scan_result.get('messages_scanned', 0)} msgs, "
                                f"{scan_result.get('contacts_found', 0)} contacts"
                            )
                        except asyncio.TimeoutError:
                            log.warning(f"Deep email scan TIMEOUT for {user.email}")
                        except Exception as e:
                            log.error(f"Deep email scan error for {user.email}: {e}")
                            db.rollback()

                    _last_deep_email_scan = now
            except Exception as e:
                log.error(f"Deep email mining error: {e}")
                db.rollback()

        # ── Deep enrichment sweep (every 12 hours) ──
        if settings.deep_enrichment_enabled:
            try:
                global _last_deep_enrichment_sweep
                if now - _last_deep_enrichment_sweep > timedelta(hours=12):
                    from .models import VendorCard, Company
                    from .services.deep_enrichment_service import deep_enrich_vendor, deep_enrich_company

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
                            await deep_enrich_vendor(vid, db)
                        except Exception as e:
                            log.warning(f"Enrichment sweep vendor {vid} error: {e}")
                            db.rollback()

                    async def _safe_enrich_company(cid):
                        try:
                            await deep_enrich_company(cid, db)
                        except Exception as e:
                            log.warning(f"Enrichment sweep company {cid} error: {e}")
                            db.rollback()

                    all_vendor_ids = [vid for (vid,) in stale_vendors] + [vid for (vid,) in recent_vendors]
                    # Process in batches of 10 to avoid overwhelming external APIs
                    for i in range(0, len(all_vendor_ids), 10):
                        batch = all_vendor_ids[i:i + 10]
                        await asyncio.gather(*[_safe_enrich_vendor(vid) for vid in batch], return_exceptions=True)

                    if stale_companies:
                        await asyncio.gather(
                            *[_safe_enrich_company(cid) for (cid,) in stale_companies],
                            return_exceptions=True,
                        )

                    _last_deep_enrichment_sweep = now
                    log.info(
                        f"Deep enrichment sweep: {len(stale_vendors)} vendors, "
                        f"{len(recent_vendors)} new vendors, {len(stale_companies)} companies"
                    )
            except Exception as e:
                log.error(f"Deep enrichment sweep error: {e}")
                db.rollback()

    except Exception as e:
        log.error(f"Scheduler batch error: {e}")
    finally:
        db.close()


# ── Inbox Scanning ──────────────────────────────────────────────────────


async def _scan_user_inbox(user, db):
    """Scan a single user's inbox for vendor replies and stock lists."""
    from .config import settings
    from .email_service import poll_inbox

    is_backfill = user.last_inbox_scan is None
    if is_backfill:
        log.info(
            f"First-time inbox backfill for {user.email} ({settings.inbox_backfill_days} days)"
        )

    # Poll inbox for replies (poll_inbox handles dedup via message_id)
    try:
        token = await get_valid_token(user, db)
        if not token:
            log.warning(f"Skipping inbox poll for {user.email} — no valid token")
            return
        new_responses = await poll_inbox(
            token=token,
            db=db,
            scanned_by_user_id=user.id,
        )
        if new_responses:
            log.info(f"Inbox scan [{user.email}]: {len(new_responses)} new responses")
    except Exception as e:
        log.error(f"Inbox poll failed for {user.email}: {e}")

    # Scan for stock list attachments
    try:
        await _scan_stock_list_attachments(user, db, is_backfill)
    except Exception as e:
        log.error(f"Stock list scan failed for {user.email}: {e}")

    # Enrich vendor cards from inbox (email mining)
    try:
        await _mine_vendor_contacts(user, db, is_backfill)
    except Exception as e:
        log.error(f"Vendor mining failed for {user.email}: {e}")

    # Upgrade 3: Scan Sent Items for outbound AVAIL RFQs
    try:
        await _scan_outbound_rfqs(user, db, is_backfill)
    except Exception as e:
        log.error(f"Outbound scan failed for {user.email}: {e}")

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

    log.info(
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
                log.error(f"Stock list import failed [{att_info.get('filename')}]: {e}")


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
        log.warning(f"Attachment download failed: {e}")
        return

    if not att_data or "error" in att_data:
        log.warning(f"Attachment download error: {att_data}")
        return

    content_bytes = att_data.get("contentBytes")
    if not content_bytes:
        return

    file_bytes = base64.b64decode(content_bytes)

    # H3: Validate file type before parsing
    from app.utils.file_validation import validate_file

    is_valid, detected_type = validate_file(file_bytes, filename)
    if not is_valid:
        log.warning(f"File validation failed for {filename}: detected {detected_type}")
        return

    # Parse the file — use new AI-powered parser (Upgrade 2), fallback to legacy
    try:
        from app.services.attachment_parser import parse_attachment

        rows = await parse_attachment(
            file_bytes, filename, vendor_domain=vendor_domain, db=db
        )
    except Exception as e:
        log.warning(f"AI attachment parser failed, using legacy parser: {e}")
        rows = _parse_stock_file(file_bytes, filename)

    if not rows:
        log.info(f"No valid rows in {filename}")
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
            source_company_id = sender_match["id"]
            log.info(
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
                log.debug(f"MaterialCard flush conflict for '{mpn}': {e}")
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
        log.info(f"Auto-imported {imported} parts from {list_type} {filename} ({vendor_name})")
    except Exception as e:
        log.error(f"Stock list commit failed: {e}")
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
        log.debug(f"Teams stock match check skipped: {e}")


def _parse_stock_file(file_bytes: bytes, filename: str) -> list[dict]:
    """Parse a stock list file (CSV/XLSX) into rows with mpn, qty, price, manufacturer."""
    from .file_utils import parse_tabular_file, normalize_stock_row

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
    from .vendor_utils import normalize_vendor_name
    from .models import VendorCard

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
                log.debug(f"VendorCard flush conflict for '{norm}': {e}")
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
            log.info(f"Contact mining [{user.email}]: enriched {enriched} contacts")
    except Exception as e:
        log.error(f"Contact mining commit failed for {user.email}: {e}")
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
            log.info(
                f"Outbound scan [{user.email}]: {rfqs} RFQs, {updated} vendor cards updated"
            )
    except Exception as e:
        log.error(f"Outbound scan commit failed for {user.email}: {e}")
        db.rollback()


# ── Upgrade 4: Engagement Score Computation ───────────────────────────


async def _compute_engagement_scores_job(db):
    """Recompute engagement scores for all vendors with outreach data.

    Called once per day by the scheduler tick.
    """
    from .services.engagement_scorer import compute_all_engagement_scores

    try:
        result = await compute_all_engagement_scores(db)
        log.info(
            f"Engagement scoring complete: {result['updated']} updated, {result['skipped']} skipped"
        )
    except Exception as e:
        log.error(f"Engagement scoring failed: {e}")


# ── Contacts Sync (Outlook → VendorCards) ───────────────────────────────


async def _sync_user_contacts(user, db):
    """Pull contacts from Outlook into VendorCards."""
    from .models import VendorCard
    from .vendor_utils import (
        normalize_vendor_name,
        merge_emails_into_card,
        merge_phones_into_card,
    )

    # Use GraphClient for pagination with retry (H1, H6)
    from app.utils.graph_client import GraphClient

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
        log.warning(f"Contacts sync failed for {user.email}: {e}")
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
                log.debug(f"VendorCard flush conflict for '{norm}': {e}")
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
        log.info(
            f"Contacts sync [{user.email}]: {len(contacts)} contacts, {enriched} new emails"
        )
    except Exception as e:
        log.error(f"Contacts sync commit failed: {e}")
        db.rollback()
