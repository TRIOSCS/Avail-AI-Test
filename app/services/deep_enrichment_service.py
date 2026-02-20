"""Deep Enrichment Service — orchestrates multi-source enrichment with confidence routing.

Combines Hunter.io, RocketReach, Clearbit, Apollo, Clay, Explorium, and AI
enrichment with signature parsing and specialty detection. Routes results
through a three-tier confidence system:
  - >= auto_apply_threshold (0.8): auto-apply + log
  - >= review_threshold (0.5): queue for human review
  - < review_threshold: low-confidence record (visible but not queued)
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("avail.deep_enrichment")


# ── Contact linking ──────────────────────────────────────────────────


def link_contact_to_entities(db, sender_email: str, signature_data: dict) -> None:
    """Match sender by email domain to VendorCard/Company, create/update VendorContact or SiteContact."""
    from ..models import VendorCard, VendorContact, Company, SiteContact, CustomerSite

    if not sender_email or "@" not in sender_email:
        return

    domain = sender_email.split("@")[-1].lower()
    full_name = signature_data.get("full_name")
    title = signature_data.get("title")
    phone = signature_data.get("phone") or signature_data.get("mobile")

    # Try matching to VendorCard by domain or domain_aliases
    cards = (
        db.query(VendorCard)
        .filter(VendorCard.domain == domain)
        .all()
    )
    if not cards:
        # Try domain_aliases (JSON array)
        from sqlalchemy import cast, String
        cards = (
            db.query(VendorCard)
            .filter(VendorCard.domain_aliases.cast(String).contains(domain))
            .all()
        )

    for card in cards:
        existing = (
            db.query(VendorContact)
            .filter(
                VendorContact.vendor_card_id == card.id,
                VendorContact.email == sender_email.lower(),
            )
            .first()
        )
        if existing:
            # Update if we have better data
            if full_name and not existing.full_name:
                existing.full_name = full_name
            if title and not existing.title:
                existing.title = title
            if phone and not existing.phone:
                existing.phone = phone
            existing.last_seen_at = datetime.now(timezone.utc)
            existing.interaction_count = (existing.interaction_count or 0) + 1
        else:
            if full_name:
                contact = VendorContact(
                    vendor_card_id=card.id,
                    full_name=full_name,
                    title=title,
                    email=sender_email.lower(),
                    phone=phone,
                    source="email_signature",
                    confidence=int(signature_data.get("confidence", 0.5) * 100),
                )
                db.add(contact)

    # Try matching to Company by domain
    companies = (
        db.query(Company)
        .filter(Company.domain == domain)
        .all()
    )
    for company in companies:
        # Find any site for this company to attach the contact
        site = (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id == company.id)
            .first()
        )
        if site and full_name:
            existing = (
                db.query(SiteContact)
                .filter(
                    SiteContact.customer_site_id == site.id,
                    SiteContact.email == sender_email.lower(),
                )
                .first()
            )
            if not existing:
                sc = SiteContact(
                    customer_site_id=site.id,
                    full_name=full_name,
                    title=title,
                    email=sender_email.lower(),
                    phone=phone,
                )
                db.add(sc)

    try:
        db.flush()
    except Exception as e:
        log.debug("Contact linking flush error: %s", e)
        db.rollback()


# ── Confidence routing ───────────────────────────────────────────────


def route_enrichment(
    db,
    entity_type: str,
    entity_id: int,
    field_name: str,
    current_value,
    proposed_value,
    confidence: float,
    source: str,
    enrichment_type: str = "company_info",
    job_id: int | None = None,
) -> str:
    """Route an enrichment result through the three-tier confidence system.

    Returns: "auto_applied" | "pending" | "low_confidence"
    """
    from ..config import settings
    from ..models import EnrichmentQueue

    auto_threshold = settings.deep_enrichment_auto_apply_threshold
    review_threshold = settings.deep_enrichment_review_threshold

    # Serialize values for storage
    current_str = json.dumps(current_value) if current_value is not None else None
    proposed_str = json.dumps(proposed_value) if not isinstance(proposed_value, str) else proposed_value

    # Build the queue entry kwargs
    entry_kwargs = {
        "enrichment_type": enrichment_type,
        "field_name": field_name,
        "current_value": current_str,
        "proposed_value": proposed_str,
        "confidence": confidence,
        "source": source,
        "batch_job_id": job_id,
    }

    # Set polymorphic target
    if entity_type == "vendor_card":
        entry_kwargs["vendor_card_id"] = entity_id
    elif entity_type == "company":
        entry_kwargs["company_id"] = entity_id
    elif entity_type == "vendor_contact":
        entry_kwargs["vendor_contact_id"] = entity_id

    if confidence >= auto_threshold:
        # Auto-apply
        _apply_field_update(db, entity_type, entity_id, field_name, proposed_value)
        entry_kwargs["status"] = "auto_applied"
        db.add(EnrichmentQueue(**entry_kwargs))
        return "auto_applied"
    elif confidence >= review_threshold:
        # Queue for review
        entry_kwargs["status"] = "pending"
        db.add(EnrichmentQueue(**entry_kwargs))
        return "pending"
    else:
        # Low confidence — record but don't queue
        entry_kwargs["status"] = "low_confidence"
        db.add(EnrichmentQueue(**entry_kwargs))
        return "low_confidence"


def _apply_field_update(db, entity_type: str, entity_id: int, field_name: str, value) -> None:
    """Apply a single field update to an entity."""
    from ..models import VendorCard, Company, VendorContact

    model_map = {
        "vendor_card": VendorCard,
        "company": Company,
        "vendor_contact": VendorContact,
    }
    model = model_map.get(entity_type)
    if not model:
        return

    entity = db.get(model, entity_id)
    if not entity:
        return

    # Handle JSON array fields (brand_tags, commodity_tags)
    if field_name in ("brand_tags", "commodity_tags"):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
        setattr(entity, field_name, value)
    else:
        setattr(entity, field_name, value)


def apply_queue_item(db, queue_item, user_id: int | None = None) -> bool:
    """Apply a pending enrichment queue item. Returns True on success."""
    from ..models import EnrichmentQueue

    if queue_item.status not in ("pending", "low_confidence"):
        return False

    proposed = queue_item.proposed_value
    # Try to parse JSON values
    try:
        proposed = json.loads(proposed)
    except (json.JSONDecodeError, TypeError):
        pass

    entity_type = None
    entity_id = None
    if queue_item.vendor_card_id:
        entity_type = "vendor_card"
        entity_id = queue_item.vendor_card_id
    elif queue_item.company_id:
        entity_type = "company"
        entity_id = queue_item.company_id
    elif queue_item.vendor_contact_id:
        entity_type = "vendor_contact"
        entity_id = queue_item.vendor_contact_id

    if entity_type and entity_id:
        _apply_field_update(db, entity_type, entity_id, queue_item.field_name, proposed)

    queue_item.status = "approved"
    queue_item.reviewed_by_id = user_id
    queue_item.reviewed_at = datetime.now(timezone.utc)
    return True


# ── Deep enrichment orchestrators ────────────────────────────────────


async def deep_enrich_vendor(vendor_card_id: int, db, job_id: int | None = None, force: bool = False) -> dict:
    """Deep enrich a vendor card with all available sources.

    1. Skip if recently enriched (unless force=True)
    2. Company enrichment via existing enrich_entity()
    3. Email verification via Hunter.io
    4. Contact discovery: Apollo → Hunter → RocketReach → Clearbit → dedupe
    5. Specialty detection
    6. AI material analysis
    7. Confidence routing per field
    8. Update deep_enrichment_at timestamp
    """
    from ..config import settings
    from ..models import VendorCard, VendorContact

    card = db.get(VendorCard, vendor_card_id)
    if not card:
        return {"status": "not_found"}

    # Skip if recently enriched (bypass when force=True)
    if not force:
        stale_days = settings.deep_enrichment_stale_days
        if card.deep_enrichment_at:
            age = datetime.now(timezone.utc) - (
                card.deep_enrichment_at.replace(tzinfo=timezone.utc)
                if card.deep_enrichment_at.tzinfo is None
                else card.deep_enrichment_at
            )
            if age < timedelta(days=stale_days):
                return {"status": "skipped", "reason": "recently_enriched"}

    enriched_fields = []
    errors = []

    # 1. Company enrichment via existing waterfall
    if card.domain:
        try:
            from ..enrichment_service import enrich_entity, apply_enrichment_to_vendor
            data = await enrich_entity(card.domain, card.display_name)
            if data:
                for field in ("legal_name", "industry", "employee_size", "hq_city",
                              "hq_state", "hq_country", "linkedin_url", "website"):
                    proposed = data.get(field)
                    current = getattr(card, field, None)
                    if proposed and not current:
                        result = route_enrichment(
                            db, "vendor_card", card.id, field,
                            current, proposed,
                            confidence=0.85,
                            source=data.get("source", "enrichment"),
                            enrichment_type="company_info",
                            job_id=job_id,
                        )
                        if result == "auto_applied":
                            enriched_fields.append(field)
        except Exception as e:
            errors.append(f"company_enrichment: {e}")
            log.warning("Company enrichment failed for vendor %d: %s", vendor_card_id, e)

    # 2. Email verification via Hunter.io (parallel across contacts)
    try:
        from ..connectors.hunter_client import verify_email
        contacts = db.query(VendorContact).filter(
            VendorContact.vendor_card_id == vendor_card_id,
            VendorContact.email.isnot(None),
        ).limit(20).all()

        async def _verify_one(contact):
            try:
                result = await verify_email(contact.email)
                if result and result.get("status") in ("valid", "accept_all"):
                    if not contact.is_verified:
                        contact.is_verified = True
                        return f"verified:{contact.email}"
            except Exception:
                pass
            return None

        verify_results = await asyncio.gather(*[_verify_one(c) for c in contacts])
        enriched_fields.extend(r for r in verify_results if r)
    except Exception as e:
        errors.append(f"email_verification: {e}")

    # 3-5: Run contact discovery, specialty detection, and AI analysis in parallel
    _contact_errors = []
    _specialty_result = {}
    _material_ok = False

    async def _contact_discovery():
        nonlocal _contact_errors
        if not card.domain:
            return
        try:
            from ..enrichment_service import find_suggested_contacts
            new_contacts = await find_suggested_contacts(
                card.domain, card.display_name
            )
            existing_emails = {
                c.email.lower()
                for c in db.query(VendorContact).filter(
                    VendorContact.vendor_card_id == vendor_card_id,
                    VendorContact.email.isnot(None),
                ).all()
            }
            for contact_data in new_contacts:
                email = (contact_data.get("email") or "").lower()
                if email and email not in existing_emails:
                    confidence = 0.7
                    src = contact_data.get("source", "unknown")
                    if src == "apollo":
                        confidence = 0.85
                    elif src in ("hunter", "rocketreach", "clearbit"):
                        confidence = 0.8
                    route_enrichment(
                        db, "vendor_card", card.id,
                        f"new_contact:{email}",
                        None,
                        json.dumps(contact_data),
                        confidence=confidence,
                        source=src,
                        enrichment_type="contact_info",
                        job_id=job_id,
                    )
                    existing_emails.add(email)
        except Exception as e:
            _contact_errors.append(f"contact_discovery: {e}")

    async def _specialty_detection():
        nonlocal _specialty_result
        try:
            from .specialty_detector import analyze_vendor_specialties
            loop = asyncio.get_event_loop()
            _specialty_result = await loop.run_in_executor(
                None, analyze_vendor_specialties, vendor_card_id, db
            )
        except Exception as e:
            _contact_errors.append(f"specialty_detection: {e}")

    async def _material_analysis():
        nonlocal _material_ok
        try:
            from ..routers.vendors import _analyze_vendor_materials
            await _analyze_vendor_materials(vendor_card_id, db_session=db)
            _material_ok = True
        except Exception as e:
            _contact_errors.append(f"material_analysis: {e}")

    await asyncio.gather(
        _contact_discovery(),
        _specialty_detection(),
        _material_analysis(),
    )

    errors.extend(_contact_errors)
    specialties = _specialty_result
    if _material_ok:
        enriched_fields.append("material_tags")

    # Apply specialty results
    if specialties.get("brand_tags"):
        route_enrichment(
            db, "vendor_card", card.id, "brand_tags",
            card.brand_tags,
            specialties["brand_tags"],
            confidence=specialties.get("confidence", 0.5),
            source="specialty_analysis",
            enrichment_type="brand_tags",
            job_id=job_id,
        )
    if specialties.get("commodity_tags"):
        route_enrichment(
            db, "vendor_card", card.id, "commodity_tags",
            card.commodity_tags,
            specialties["commodity_tags"],
            confidence=specialties.get("confidence", 0.5),
            source="specialty_analysis",
            enrichment_type="commodity_tags",
            job_id=job_id,
        )

    # Update timestamp
    card.deep_enrichment_at = datetime.now(timezone.utc)
    if specialties and specialties.get("confidence"):
        card.specialty_confidence = specialties["confidence"]

    try:
        db.commit()
    except Exception as e:
        log.error("Deep enrich vendor commit failed: %s", e)
        db.rollback()

    return {
        "status": "completed",
        "vendor_card_id": vendor_card_id,
        "enriched_fields": enriched_fields,
        "errors": errors,
    }


async def deep_enrich_company(company_id: int, db, job_id: int | None = None, force: bool = False) -> dict:
    """Deep enrich a company with all available sources."""
    from ..config import settings
    from ..models import Company

    company = db.get(Company, company_id)
    if not company:
        return {"status": "not_found"}

    # Skip if recently enriched (bypass when force=True)
    if not force:
        stale_days = settings.deep_enrichment_stale_days
        if company.deep_enrichment_at:
            age = datetime.now(timezone.utc) - (
                company.deep_enrichment_at.replace(tzinfo=timezone.utc)
                if company.deep_enrichment_at.tzinfo is None
                else company.deep_enrichment_at
            )
            if age < timedelta(days=stale_days):
                return {"status": "skipped", "reason": "recently_enriched"}

    enriched_fields = []
    errors = []

    domain = company.domain
    if not domain and company.website:
        import re
        m = re.search(r"(?:https?://)?(?:www\.)?([a-zA-Z0-9\-]+\.[a-zA-Z]{2,})", company.website)
        if m:
            domain = m.group(1).lower()

    if domain:
        try:
            from ..enrichment_service import enrich_entity
            data = await enrich_entity(domain, company.name)
            if data:
                for field in ("legal_name", "industry", "employee_size", "hq_city",
                              "hq_state", "hq_country", "linkedin_url", "website", "domain"):
                    proposed = data.get(field)
                    current = getattr(company, field, None)
                    if proposed and not current:
                        result = route_enrichment(
                            db, "company", company.id, field,
                            current, proposed,
                            confidence=0.85,
                            source=data.get("source", "enrichment"),
                            enrichment_type="company_info",
                            job_id=job_id,
                        )
                        if result == "auto_applied":
                            enriched_fields.append(field)
        except Exception as e:
            errors.append(f"company_enrichment: {e}")

        # Clearbit company enrichment for extra firmographics
        try:
            from ..connectors.clearbit_client import enrich_company as clearbit_enrich
            cb = await clearbit_enrich(domain)
            if cb:
                for field in ("industry", "employee_size", "hq_city", "hq_state", "hq_country"):
                    proposed = cb.get(field)
                    current = getattr(company, field, None)
                    if proposed and not current:
                        route_enrichment(
                            db, "company", company.id, field,
                            current, proposed,
                            confidence=0.8,
                            source="clearbit",
                            enrichment_type="company_info",
                            job_id=job_id,
                        )
        except Exception as e:
            errors.append(f"clearbit: {e}")

        # Contact discovery for company
        try:
            from ..enrichment_service import find_suggested_contacts
            new_contacts = await find_suggested_contacts(domain, company.name)
            from ..models import SiteContact, CustomerSite
            # Find a site to attach contacts to
            site = db.query(CustomerSite).filter(
                CustomerSite.company_id == company_id
            ).first()
            if site and new_contacts:
                existing_emails = {
                    c.email.lower()
                    for c in db.query(SiteContact).filter(
                        SiteContact.customer_site_id == site.id,
                        SiteContact.email.isnot(None),
                    ).all()
                }
                for contact_data in new_contacts:
                    email = (contact_data.get("email") or "").lower()
                    if email and email not in existing_emails:
                        sc = SiteContact(
                            customer_site_id=site.id,
                            full_name=contact_data.get("full_name"),
                            title=contact_data.get("title"),
                            email=email,
                            phone=contact_data.get("phone"),
                        )
                        db.add(sc)
                        existing_emails.add(email)
                        enriched_fields.append(f"contact:{email}")
        except Exception as e:
            errors.append(f"contact_discovery: {e}")

    company.deep_enrichment_at = datetime.now(timezone.utc)

    try:
        db.commit()
    except Exception as e:
        log.error("Deep enrich company commit failed: %s", e)
        db.rollback()

    return {
        "status": "completed",
        "company_id": company_id,
        "enriched_fields": enriched_fields,
        "errors": errors,
    }


# ── Backfill job runner ──────────────────────────────────────────────


async def run_backfill_job(db, started_by_id: int, scope: dict | None = None) -> int:
    """Create and run a backfill enrichment job. Returns job ID.

    Scope options: {
        entity_types: ["vendor", "company"],
        max_items: 500,
        include_deep_email: true,
        lookback_days: 365,
    }
    """
    from ..models import EnrichmentJob, VendorCard, Company, User

    scope = scope or {}
    entity_types = scope.get("entity_types", ["vendor", "company"])
    max_items = min(scope.get("max_items", 500), 2000)

    # Count items to process
    total = 0
    if "vendor" in entity_types:
        total += db.query(VendorCard).filter(
            (VendorCard.deep_enrichment_at.is_(None)) |
            (VendorCard.deep_enrichment_at < datetime.now(timezone.utc) - timedelta(days=30))
        ).count()
    if "company" in entity_types:
        total += db.query(Company).filter(
            (Company.deep_enrichment_at.is_(None)) |
            (Company.deep_enrichment_at < datetime.now(timezone.utc) - timedelta(days=30))
        ).count()

    total = min(total, max_items)

    job = EnrichmentJob(
        job_type="backfill",
        status="running",
        total_items=total,
        scope=scope,
        started_by_id=started_by_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()

    # Run as background task
    asyncio.create_task(_execute_backfill(job.id, entity_types, max_items, scope))

    return job.id


async def _execute_backfill(job_id: int, entity_types: list, max_items: int, scope: dict):
    """Execute the backfill job in the background."""
    from ..database import SessionLocal
    from ..models import EnrichmentJob, VendorCard, Company, User

    db = SessionLocal()
    try:
        job = db.get(EnrichmentJob, job_id)
        if not job:
            return

        processed = 0
        enriched = 0
        error_count = 0
        error_log = []

        # Process vendors
        if "vendor" in entity_types:
            vendors = (
                db.query(VendorCard)
                .filter(
                    (VendorCard.deep_enrichment_at.is_(None)) |
                    (VendorCard.deep_enrichment_at < datetime.now(timezone.utc) - timedelta(days=30))
                )
                .order_by(VendorCard.sighting_count.desc().nullslast())
                .limit(max_items)
                .all()
            )

            batch_size = 20
            sem = asyncio.Semaphore(5)

            for i in range(0, len(vendors), batch_size):
                batch = vendors[i:i + batch_size]

                # Check if job was cancelled
                db.refresh(job)
                if job.status == "cancelled":
                    job.completed_at = datetime.now(timezone.utc)
                    db.commit()
                    return

                async def _enrich_vendor(card):
                    async with sem:
                        try:
                            result = await deep_enrich_vendor(card.id, db, job_id=job.id)
                            return result
                        except Exception as e:
                            return {"status": "error", "error": f"vendor_{card.id}: {str(e)[:100]}"}

                batch_results = await asyncio.gather(
                    *[_enrich_vendor(c) for c in batch], return_exceptions=True
                )

                for r in batch_results:
                    if isinstance(r, Exception):
                        error_count += 1
                        error_log.append(str(r)[:100])
                    elif isinstance(r, dict):
                        if r.get("status") == "completed":
                            enriched += 1
                        if r.get("errors"):
                            error_count += len(r["errors"])
                            error_log.extend(r["errors"][:3])
                        if r.get("error"):
                            error_count += 1
                            error_log.append(r["error"])
                    processed += 1

                # Update progress
                job.processed_items = processed
                job.enriched_items = enriched
                job.error_count = error_count
                db.commit()

                # Rate limiting between batches
                await asyncio.sleep(1)

        # Process companies
        if "company" in entity_types:
            remaining = max_items - processed
            if remaining > 0:
                companies = (
                    db.query(Company)
                    .filter(
                        (Company.deep_enrichment_at.is_(None)) |
                        (Company.deep_enrichment_at < datetime.now(timezone.utc) - timedelta(days=30))
                    )
                    .limit(remaining)
                    .all()
                )

                batch_size = 20
                co_sem = asyncio.Semaphore(5)

                for i in range(0, len(companies), batch_size):
                    batch = companies[i:i + batch_size]

                    db.refresh(job)
                    if job.status == "cancelled":
                        job.completed_at = datetime.now(timezone.utc)
                        db.commit()
                        return

                    async def _enrich_co(company):
                        async with co_sem:
                            try:
                                return await deep_enrich_company(company.id, db, job_id=job.id)
                            except Exception as e:
                                return {"status": "error", "error": f"company_{company.id}: {str(e)[:100]}"}

                    batch_results = await asyncio.gather(
                        *[_enrich_co(c) for c in batch], return_exceptions=True
                    )

                    for r in batch_results:
                        if isinstance(r, Exception):
                            error_count += 1
                            error_log.append(str(r)[:100])
                        elif isinstance(r, dict):
                            if r.get("status") == "completed":
                                enriched += 1
                            if r.get("errors"):
                                error_count += len(r["errors"])
                                error_log.extend(r["errors"][:3])
                            if r.get("error"):
                                error_count += 1
                                error_log.append(r["error"])
                        processed += 1

                    job.processed_items = processed
                    job.enriched_items = enriched
                    job.error_count = error_count
                    db.commit()
                    await asyncio.sleep(1)

        # Deep email mining per user (if enabled)
        if scope.get("include_deep_email"):
            try:
                from ..scheduler import get_valid_token
                from ..connectors.email_mining import EmailMiner
                from .signature_parser import extract_signature, cache_signature_extract

                users = db.query(User).filter(
                    User.refresh_token.isnot(None),
                    User.m365_connected == True,
                ).all()

                lookback = scope.get("lookback_days", 365)
                for user in users:
                    try:
                        token = await get_valid_token(user, db)
                        if not token:
                            continue
                        miner = EmailMiner(token, db=db, user_id=user.id)
                        scan_result = await miner.deep_scan_inbox(
                            lookback_days=lookback, max_messages=2000
                        )

                        # Process signatures from scan results
                        for domain, domain_data in scan_result.get("per_domain", {}).items():
                            for email_addr in domain_data.get("emails", []):
                                sig_data = await extract_signature(
                                    "",  # No body available in summary
                                    sender_name=domain_data.get("sender_names", [""])[0] if domain_data.get("sender_names") else "",
                                    sender_email=email_addr,
                                )
                                if sig_data.get("confidence", 0) > 0.3:
                                    cache_signature_extract(db, email_addr, sig_data)
                                    link_contact_to_entities(db, email_addr, sig_data)

                        user.last_deep_email_scan = datetime.now(timezone.utc)
                        db.commit()
                    except Exception as e:
                        log.warning("Deep email scan failed for user %s: %s", user.email, e)
                        error_log.append(f"email_scan_{user.email}: {str(e)[:100]}")
                        db.rollback()
            except Exception as e:
                error_log.append(f"deep_email_mining: {str(e)[:100]}")

        # Complete job
        job.status = "completed"
        job.processed_items = processed
        job.enriched_items = enriched
        job.error_count = error_count
        job.error_log = error_log[:50]  # Cap error log
        job.completed_at = datetime.now(timezone.utc)
        db.commit()

        log.info(
            "Backfill job %d completed: %d processed, %d enriched, %d errors",
            job_id, processed, enriched, error_count,
        )

    except Exception as e:
        log.error("Backfill job %d failed: %s", job_id, e)
        try:
            job = db.get(EnrichmentJob, job_id)
            if job:
                job.status = "failed"
                job.error_log = [str(e)[:500]]
                job.completed_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
