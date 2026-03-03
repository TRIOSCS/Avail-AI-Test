"""Admin data operations -- dedup, merge, CSV import, Teams, and account transfer.

Business rules:
- Vendor/company dedup uses fuzzy name matching with configurable threshold.
- CSV import deduplicates by company/vendor name (case-insensitive).
- Teams integration config is persisted in SystemConfig table.
- Mass account transfer enforces SITE_CAP_PER_USER on the target user.
- Merge operations delete the "remove" entity after reassigning all FKs.

Called by: app/routers/admin/__init__.py (included via router)
Depends on: app/services/vendor_merge_service.py, app/services/company_merge_service.py,
            app/vendor_utils, app/company_utils, app/models, app/dependencies
"""

import asyncio
import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_admin
from ...models import (
    Company,
    CustomerSite,
    SiteContact,
    SystemConfig,
    User,
    VendorCard,
    VendorContact,
)
from ...rate_limit import limiter
from ...schemas.crm import CompanyMergeRequest, MassTransferRequest

router = APIRouter(tags=["admin"])


# -- Schemas ---------------------------------------------------------------


class VendorMergeRequest(BaseModel):
    keep_id: int = Field(..., description="ID of the vendor card to keep")
    remove_id: int = Field(..., description="ID of the vendor card to merge into keep_id and delete")


class TeamsConfigRequest(BaseModel):
    team_id: str = Field(..., min_length=1)
    channel_id: str = Field(..., min_length=1)
    channel_name: Optional[str] = None
    enabled: bool = True
    hot_threshold: Optional[float] = None


# -- Vendor Dedup Suggestions (admin) --------------------------------------


@router.get("/api/admin/vendor-dedup-suggestions")
@limiter.limit("30/minute")
async def api_vendor_dedup_suggestions(
    request: Request,
    threshold: int = 85,
    limit: int = 50,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Find potential duplicate vendor cards using fuzzy name matching."""
    from ...vendor_utils import find_vendor_dedup_candidates

    loop = asyncio.get_running_loop()
    candidates = await loop.run_in_executor(
        None, find_vendor_dedup_candidates, db, max(70, min(threshold, 100)), min(limit, 200)
    )
    return {"candidates": candidates, "count": len(candidates)}


@router.post("/api/admin/vendor-merge")
@limiter.limit("30/minute")
async def merge_vendor_cards(
    request: Request,
    payload: VendorMergeRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Merge two vendor cards: reassign all FKs from remove_id to keep_id, then delete remove_id."""
    from ...services.vendor_merge_service import merge_vendor_cards as _merge

    try:
        result = _merge(payload.keep_id, payload.remove_id, db)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return result


# -- Company Dedup (admin only) --------------------------------------------


@router.get("/api/admin/company-dedup-suggestions")
@limiter.limit("30/minute")
async def api_company_dedup_suggestions(
    request: Request,
    threshold: int = 85,
    limit: int = 50,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Find potential duplicate companies using fuzzy name matching."""
    from ...company_utils import find_company_dedup_candidates

    loop = asyncio.get_running_loop()
    candidates = await loop.run_in_executor(
        None, find_company_dedup_candidates, db, max(70, min(threshold, 100)), min(limit, 200)
    )
    return {"candidates": candidates, "count": len(candidates)}


@router.get("/api/admin/company-merge-preview")
@limiter.limit("30/minute")
def api_company_merge_preview(
    request: Request,
    keep_id: int = 0,
    remove_id: int = 0,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Preview impact of merging two companies."""
    from ...models import ActivityLog, EnrichmentQueue, Requisition, Sighting

    keep = db.get(Company, keep_id)
    remove = db.get(Company, remove_id)
    if not keep or not remove:
        raise HTTPException(404, "One or both companies not found")

    # Sites to move vs delete
    remove_sites = db.query(CustomerSite).filter(CustomerSite.company_id == remove_id).all()
    sites_to_delete = 0
    sites_to_move = 0
    for s in remove_sites:
        is_empty_hq = (
            (s.site_name or "").strip().upper() == "HQ"
            and not s.contact_name
            and not s.contact_email
            and not s.address_line1
            and db.query(SiteContact).filter(SiteContact.customer_site_id == s.id).count() == 0
            and db.query(Requisition).filter(Requisition.customer_site_id == s.id).count() == 0
        )
        if is_empty_hq:
            sites_to_delete += 1
        else:
            sites_to_move += 1

    activities = db.query(ActivityLog).filter(ActivityLog.company_id == remove_id).count()
    enrichments = db.query(EnrichmentQueue).filter(EnrichmentQueue.company_id == remove_id).count()
    sightings = db.query(Sighting).filter(Sighting.source_company_id == remove_id).count()

    # Fields to fill
    fill_fields = []
    for field in (
        "domain",
        "linkedin_url",
        "legal_name",
        "employee_size",
        "hq_city",
        "hq_state",
        "hq_country",
        "website",
        "industry",
        "phone",
        "credit_terms",
        "tax_id",
        "currency",
        "preferred_carrier",
        "account_type",
    ):
        if getattr(keep, field) is None and getattr(remove, field) is not None:
            fill_fields.append(field)

    # Tags to merge
    keep_brands = set(keep.brand_tags or [])
    remove_brands = set(remove.brand_tags or [])
    keep_commodities = set(keep.commodity_tags or [])
    remove_commodities = set(remove.commodity_tags or [])
    new_tags = len(remove_brands - keep_brands) + len(remove_commodities - keep_commodities)

    return {
        "keep": {"id": keep.id, "name": keep.name},
        "remove": {"id": remove.id, "name": remove.name},
        "sites_to_move": sites_to_move,
        "sites_to_delete": sites_to_delete,
        "activities_to_reassign": activities,
        "enrichments_to_reassign": enrichments,
        "sightings_to_reassign": sightings,
        "fields_to_fill": fill_fields,
        "tags_to_merge": new_tags,
    }


@router.post("/api/admin/company-merge")
@limiter.limit("30/minute")
def api_company_merge(
    request: Request,
    payload: CompanyMergeRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Merge two companies: move sites, reassign FKs, delete the removed company."""
    from ...services.company_merge_service import merge_companies as _merge

    try:
        result = _merge(payload.keep_id, payload.remove_id, db)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return result


# -- Data Import (admin only) ---------------------------------------------


@router.post("/api/admin/import/customers")
@limiter.limit("2/minute")
async def import_customers(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Import customers from CSV. Expected columns: company_name, site_name,
    contact_name, contact_email, contact_phone, contact_title,
    address_line1, city, state, zip, country, payment_terms, shipping_terms"""
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(400, "No data rows found")

    companies_created = 0
    sites_created = 0
    contacts_created = 0

    seen_companies = {}
    seen_sites = {}

    for row in rows:
        company_name = (row.get("company_name") or "").strip()
        if not company_name:
            continue

        key = company_name.lower()
        if key not in seen_companies:
            company = db.query(Company).filter(Company.name.ilike(company_name)).first()
            if not company:
                company = Company(name=company_name)
                db.add(company)
                db.flush()
                companies_created += 1
            seen_companies[key] = company

        company = seen_companies[key]

        site_name = (row.get("site_name") or company_name).strip()
        site_key = f"{key}|{site_name.lower()}"
        if site_key not in seen_sites:
            site = (
                db.query(CustomerSite)
                .filter(
                    CustomerSite.company_id == company.id,
                    CustomerSite.site_name.ilike(site_name),
                )
                .first()
            )
            if not site:
                site = CustomerSite(
                    company_id=company.id,
                    site_name=site_name,
                    owner_id=user.id,
                    address_line1=row.get("address_line1", "").strip() or None,
                    city=row.get("city", "").strip() or None,
                    state=row.get("state", "").strip() or None,
                    zip=row.get("zip", "").strip() or None,
                    country=row.get("country", "").strip() or None,
                    payment_terms=row.get("payment_terms", "").strip() or None,
                    shipping_terms=row.get("shipping_terms", "").strip() or None,
                )
                db.add(site)
                db.flush()
                sites_created += 1
            seen_sites[site_key] = site

        site = seen_sites[site_key]

        contact_name = (row.get("contact_name") or "").strip()
        contact_email = (row.get("contact_email") or "").strip()
        if contact_name or contact_email:
            existing_contact = None
            if contact_email:
                existing_contact = (
                    db.query(SiteContact)
                    .filter(
                        SiteContact.customer_site_id == site.id,
                        SiteContact.email == contact_email.lower(),
                    )
                    .first()
                )
            if not existing_contact:
                sc = SiteContact(
                    customer_site_id=site.id,
                    full_name=contact_name or contact_email or "Unknown",
                    email=contact_email.lower() or None,
                    phone=(row.get("contact_phone") or "").strip() or None,
                    title=(row.get("contact_title") or "").strip() or None,
                )
                db.add(sc)
                contacts_created += 1

    db.commit()
    return {
        "status": "ok",
        "companies_created": companies_created,
        "sites_created": sites_created,
        "contacts_created": contacts_created,
        "rows_processed": len(rows),
    }


@router.post("/api/admin/import/vendors")
@limiter.limit("2/minute")
async def import_vendors(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Import vendors from CSV. Expected columns: vendor_name, domain, website,
    contact_name, contact_email, contact_phone, contact_title"""
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(400, "No data rows found")

    vendors_created = 0
    contacts_created = 0
    seen_vendors = {}

    for row in rows:
        vendor_name = (row.get("vendor_name") or "").strip()
        if not vendor_name:
            continue

        normalized = vendor_name.lower().strip()
        if normalized not in seen_vendors:
            vc = db.query(VendorCard).filter(VendorCard.normalized_name == normalized).first()
            if not vc:
                domain = (row.get("domain") or "").strip() or None
                website = (row.get("website") or "").strip() or None
                vc = VendorCard(
                    normalized_name=normalized,
                    display_name=vendor_name,
                    domain=domain,
                    website=website,
                )
                db.add(vc)
                db.flush()
                vendors_created += 1
            seen_vendors[normalized] = vc

        vc = seen_vendors[normalized]

        contact_name = (row.get("contact_name") or "").strip()
        contact_email = (row.get("contact_email") or "").strip()
        if contact_name or contact_email:
            existing = None
            if contact_email:
                existing = (
                    db.query(VendorContact)
                    .filter(
                        VendorContact.vendor_card_id == vc.id,
                        VendorContact.email == contact_email.lower(),
                    )
                    .first()
                )
            if not existing:
                vcon = VendorContact(
                    vendor_card_id=vc.id,
                    full_name=contact_name or None,
                    email=contact_email.lower() or None,
                    phone=(row.get("contact_phone") or "").strip() or None,
                    title=(row.get("contact_title") or "").strip() or None,
                    source="csv_import",
                )
                db.add(vcon)
                contacts_created += 1

    db.commit()
    return {
        "status": "ok",
        "vendors_created": vendors_created,
        "contacts_created": contacts_created,
        "rows_processed": len(rows),
    }


# -- Teams Integration (admin only) ----------------------------------------


@router.get("/api/admin/teams/config")
@limiter.limit("30/minute")
def api_get_teams_config(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get current Teams integration configuration."""
    from ...config import settings

    config = {
        "team_id": settings.teams_team_id,
        "channel_id": settings.teams_channel_id,
        "channel_name": "",
        "hot_threshold": settings.teams_hot_threshold,
        "enabled": False,
    }

    has_enabled_row = False

    # Runtime overrides from SystemConfig
    for row in (
        db.query(SystemConfig)
        .filter(
            SystemConfig.key.in_(
                [
                    "teams_team_id",
                    "teams_channel_id",
                    "teams_enabled",
                    "teams_channel_name",
                    "teams_hot_threshold",
                ]
            )
        )
        .all()
    ):
        if row.key == "teams_team_id" and row.value:
            config["team_id"] = row.value
        elif row.key == "teams_channel_id" and row.value:
            config["channel_id"] = row.value
        elif row.key == "teams_channel_name":
            config["channel_name"] = row.value
        elif row.key == "teams_enabled":
            has_enabled_row = True
            config["enabled"] = row.value.lower() == "true"
        elif row.key == "teams_hot_threshold":
            try:
                config["hot_threshold"] = float(row.value)
            except ValueError:
                pass

    # If no explicit enabled row, infer from env vars having team+channel set
    if not has_enabled_row and config["team_id"] and config["channel_id"]:
        config["enabled"] = True

    return config


@router.post("/api/admin/teams/config")
@limiter.limit("10/minute")
def api_set_teams_config(
    request: Request,
    body: TeamsConfigRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Save Teams integration configuration."""
    _upsert_config(db, "teams_team_id", body.team_id, user.email)
    _upsert_config(db, "teams_channel_id", body.channel_id, user.email)
    _upsert_config(db, "teams_enabled", str(body.enabled).lower(), user.email)
    if body.channel_name:
        _upsert_config(db, "teams_channel_name", body.channel_name, user.email)
    if body.hot_threshold is not None:
        _upsert_config(db, "teams_hot_threshold", str(body.hot_threshold), user.email)
    db.commit()
    logger.info(
        f"Teams config updated by {user.email}: team={body.team_id}, channel={body.channel_id}, enabled={body.enabled}"
    )
    return {"status": "saved"}


@router.get("/api/admin/teams/channels")
@limiter.limit("30/minute")
async def api_list_teams_channels(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List Teams channels the user has access to via Graph API."""
    from ...scheduler import get_valid_token
    from ...utils.graph_client import GraphClient

    token = await get_valid_token(user, db)
    if not token:
        raise HTTPException(400, "No valid Microsoft 365 token. Please reconnect M365.")

    gc = GraphClient(token)

    # Get teams the user is a member of
    teams_result = await gc.get_json("/me/joinedTeams", params={"$select": "id,displayName"})
    if "error" in teams_result:
        raise HTTPException(502, f"Graph API error: {teams_result.get('error', {}).get('message', 'Unknown')}")

    teams_list = teams_result.get("value", [])

    # Fetch channels for all teams in parallel
    async def _fetch_channels(team):
        channels_result = await gc.get_json(
            f"/teams/{team['id']}/channels",
            params={"$select": "id,displayName,membershipType"},
        )
        channels = channels_result.get("value", [])
        return [
            {
                "team_id": team["id"],
                "team_name": team.get("displayName", ""),
                "channel_id": ch["id"],
                "channel_name": ch.get("displayName", ""),
                "membership_type": ch.get("membershipType", ""),
            }
            for ch in channels
        ]

    channel_lists = await asyncio.gather(*[_fetch_channels(t) for t in teams_list], return_exceptions=True)
    result = []
    for channels in channel_lists:
        if isinstance(channels, Exception):
            continue
        result.extend(channels)

    return {"channels": result}


@router.post("/api/admin/teams/test")
@limiter.limit("3/minute")
async def api_test_teams_post(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Send a test Adaptive Card to the configured Teams channel."""
    from ...scheduler import get_valid_token
    from ...services.teams import _get_teams_config, _make_card, post_to_channel

    channel_id, team_id, enabled = _get_teams_config()
    if not channel_id or not team_id:
        raise HTTPException(400, "Teams channel not configured. Save a channel first.")

    token = await get_valid_token(user, db)
    if not token:
        raise HTTPException(400, "No valid Microsoft 365 token.")

    card = _make_card(
        title="AVAIL TEST",
        subtitle="Teams integration is working correctly.",
        facts=[
            {"title": "Sent By", "value": user.name or user.email},
            {"title": "Status", "value": "Connection verified"},
        ],
        action_url="",
        action_title="Open AVAIL",
        accent_color="accent",
    )

    ok = await post_to_channel(team_id, channel_id, card, token)
    if not ok:
        raise HTTPException(502, "Failed to post to Teams channel. Check permissions.")
    return {"status": "sent", "message": "Test card posted to Teams channel."}


# -- Mass Account Transfer (admin only) ------------------------------------


@router.get("/api/admin/transfer/preview")
@limiter.limit("30/minute")
def api_transfer_preview(
    request: Request,
    source_user_id: int = 0,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return all sites owned by source user with company names."""
    source = db.get(User, source_user_id)
    if not source:
        raise HTTPException(404, "Source user not found")

    sites = db.query(CustomerSite).filter(CustomerSite.owner_id == source_user_id).all()

    # Batch-fetch company names to avoid N+1
    company_ids = {s.company_id for s in sites if s.company_id}
    companies = {}
    if company_ids:
        for c in db.query(Company).filter(Company.id.in_(company_ids)).all():
            companies[c.id] = c.name

    return {
        "source_user": {"id": source.id, "name": source.name, "email": source.email},
        "site_count": len(sites),
        "sites": [
            {
                "id": s.id,
                "site_name": s.site_name,
                "company_name": companies.get(s.company_id, ""),
                "city": s.city,
                "state": s.state,
                "is_active": s.is_active if s.is_active is not None else True,
            }
            for s in sites
        ],
    }


@router.post("/api/admin/transfer/execute")
@limiter.limit("10/minute")
def api_transfer_execute(
    request: Request,
    body: MassTransferRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Transfer site ownership from one user to another."""
    if body.source_user_id == body.target_user_id:
        raise HTTPException(400, "Source and target user must be different")

    source = db.get(User, body.source_user_id)
    if not source:
        raise HTTPException(404, "Source user not found")

    target = db.get(User, body.target_user_id)
    if not target:
        raise HTTPException(404, "Target user not found")

    # Filter to sites that are actually owned by source (race-condition safe)
    sites = (
        db.query(CustomerSite)
        .filter(
            CustomerSite.id.in_(body.site_ids),
            CustomerSite.owner_id == body.source_user_id,
        )
        .all()
    )

    if not sites:
        raise HTTPException(400, "No matching sites owned by source user")

    # Enforce site cap on target user
    from sqlalchemy import func

    target_current = (
        db.query(func.count(CustomerSite.id))
        .filter(
            CustomerSite.owner_id == body.target_user_id,
            CustomerSite.is_active.is_(True),
        )
        .scalar()
        or 0
    )
    from app.routers.v13_features import SITE_CAP_PER_USER

    if target_current + len(sites) > SITE_CAP_PER_USER:
        raise HTTPException(
            409,
            f"Transfer would give {target.name} {target_current + len(sites)} sites "
            f"(cap is {SITE_CAP_PER_USER}, currently owns {target_current}). "
            "Reduce transfer count or release sites first.",
        )

    transferred_ids = {s.id for s in sites}
    skipped_ids = [sid for sid in body.site_ids if sid not in transferred_ids]

    for s in sites:
        s.owner_id = body.target_user_id
        s.ownership_cleared_at = None

    db.commit()

    logger.info(
        "Mass transfer: %d sites from %s (id=%d) to %s (id=%d) by %s",
        len(sites),
        source.name,
        source.id,
        target.name,
        target.id,
        user.email,
    )

    return {
        "ok": True,
        "transferred": len(sites),
        "skipped": len(skipped_ids),
        "skipped_ids": skipped_ids,
        "source": {"id": source.id, "name": source.name},
        "target": {"id": target.id, "name": target.name},
    }


# -- Helpers ---------------------------------------------------------------


def _upsert_config(db: Session, key: str, value: str, admin_email: str):
    """Insert or update a SystemConfig row."""
    from datetime import datetime, timezone

    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row:
        row.value = value
        row.updated_by = admin_email
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = SystemConfig(
            key=key,
            value=value,
            updated_by=admin_email,
            description=f"Teams integration: {key}",
        )
        db.add(row)
