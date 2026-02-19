"""Admin API — User management, system config, health, data import, Teams."""

import csv
import io
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_admin, require_settings_access
from ..models import (
    ApiSource,
    SystemConfig,
    User,
    Company,
    CustomerSite,
    SiteContact,
    VendorCard,
    VendorContact,
)
from ..services.admin_service import (
    list_users,
    update_user,
    get_all_config,
    set_config_value,
    get_system_health,
    VALID_ROLES,
)
from ..services.credential_service import encrypt_value, decrypt_value, mask_value

router = APIRouter(tags=["admin"])
log = logging.getLogger(__name__)


# ── Schemas ──────────────────────────────────────────────────────────


class CreateUserRequest(BaseModel):
    name: str
    email: str
    role: str = "buyer"


class UserUpdateRequest(BaseModel):
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None


class ConfigUpdateRequest(BaseModel):
    value: str = Field(..., min_length=1, max_length=500)


# ── User Management (admin only) ─────────────────────────────────────


@router.get("/api/admin/users")
def api_list_users(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return list_users(db)


@router.post("/api/admin/users")
def api_create_user(
    body: CreateUserRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"Role must be one of: {', '.join(VALID_ROLES)}")
    existing = db.query(User).filter(User.email == body.email.lower().strip()).first()
    if existing:
        raise HTTPException(409, "User with this email already exists")
    new_user = User(
        name=body.name.strip(),
        email=body.email.lower().strip(),
        role=body.role,
    )
    db.add(new_user)
    db.commit()
    return {
        "id": new_user.id,
        "name": new_user.name,
        "email": new_user.email,
        "role": new_user.role,
    }


@router.put("/api/admin/users/{user_id}")
def api_update_user(
    user_id: int,
    body: UserUpdateRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    result = update_user(db, user_id, body.model_dump(exclude_none=False), user)
    if "error" in result:
        raise HTTPException(result.get("status", 400), result["error"])
    return result


@router.delete("/api/admin/users/{user_id}")
def api_delete_user(
    user_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == user.id:
        raise HTTPException(400, "Cannot delete yourself")
    db.delete(target)
    db.commit()
    return {"status": "deleted"}


# ── System Config (admin for writes, settings_access for reads) ──────


@router.get("/api/admin/config")
def api_get_config(
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    return get_all_config(db)


@router.put("/api/admin/config/{key}")
def api_set_config(
    key: str,
    body: ConfigUpdateRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    result = set_config_value(db, key, body.value, user.email)
    if "error" in result:
        raise HTTPException(result.get("status", 400), result["error"])
    return result


# ── System Health (settings_access) ──────────────────────────────────


@router.get("/api/admin/health")
def api_health(
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    return get_system_health(db)


# ── Credential Management (admin + dev_assistant) ─────────────────────


@router.get("/api/admin/sources/{source_id}/credentials")
def api_get_credentials(
    source_id: int,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Return masked credential values for a source."""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    result = {}
    for var_name in src.env_vars or []:
        encrypted = (src.credentials or {}).get(var_name)
        if encrypted:
            try:
                plain = decrypt_value(encrypted)
                result[var_name] = {
                    "status": "set",
                    "masked": mask_value(plain),
                    "source": "db",
                }
            except Exception:
                result[var_name] = {"status": "error", "masked": "", "source": "db"}
        elif os.getenv(var_name):
            result[var_name] = {
                "status": "set",
                "masked": mask_value(os.getenv(var_name)),
                "source": "env",
            }
        else:
            result[var_name] = {"status": "empty", "masked": "", "source": "none"}
    return {"source_id": src.id, "source_name": src.name, "credentials": result}


@router.put("/api/admin/sources/{source_id}/credentials")
def api_set_credentials(
    source_id: int,
    body: dict,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Set credential values for a source. Body: {VAR_NAME: "plaintext_value", ...}"""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    valid_vars = set(src.env_vars or [])
    creds = dict(src.credentials or {})
    updated = []
    for var_name, value in body.items():
        if var_name not in valid_vars:
            continue
        value = (value or "").strip()
        if value:
            creds[var_name] = encrypt_value(value)
            updated.append(var_name)
        else:
            creds.pop(var_name, None)
            updated.append(var_name)
    src.credentials = creds
    db.commit()
    log.info(f"Credentials updated for {src.name} by {user.email}: {updated}")
    return {"status": "ok", "updated": updated}


@router.delete("/api/admin/sources/{source_id}/credentials/{var_name}")
def api_delete_credential(
    source_id: int,
    var_name: str,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Remove a single credential from a source."""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    creds = dict(src.credentials or {})
    removed = creds.pop(var_name, None)
    src.credentials = creds
    db.commit()
    log.info(f"Credential {var_name} removed from {src.name} by {user.email}")
    return {"status": "removed" if removed else "not_found"}


# ── Vendor Dedup Suggestions (admin) ──────────────────────────────────


@router.get("/api/admin/vendor-dedup-suggestions")
def api_vendor_dedup_suggestions(
    threshold: int = 85,
    limit: int = 50,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Find potential duplicate vendor cards using fuzzy name matching."""
    from ..vendor_utils import find_vendor_dedup_candidates

    candidates = find_vendor_dedup_candidates(db, threshold=max(70, min(threshold, 100)), limit=min(limit, 200))
    return {"candidates": candidates, "count": len(candidates)}


# ── Data Import (admin only) ─────────────────────────────────────────


@router.post("/api/admin/import/customers")
async def import_customers(
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
async def import_vendors(
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
            vc = (
                db.query(VendorCard)
                .filter(VendorCard.normalized_name == normalized)
                .first()
            )
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


# ── Teams Integration (admin only) ──────────────────────────────────


class TeamsConfigRequest(BaseModel):
    team_id: str = Field(..., min_length=1)
    channel_id: str = Field(..., min_length=1)
    channel_name: Optional[str] = None
    enabled: bool = True
    hot_threshold: Optional[float] = None


@router.get("/api/admin/teams/config")
def api_get_teams_config(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get current Teams integration configuration."""
    from ..config import settings

    config = {
        "team_id": settings.teams_team_id,
        "channel_id": settings.teams_channel_id,
        "channel_name": "",
        "hot_threshold": settings.teams_hot_threshold,
        "enabled": False,
    }

    has_enabled_row = False

    # Runtime overrides from SystemConfig
    for row in db.query(SystemConfig).filter(
        SystemConfig.key.in_([
            "teams_team_id", "teams_channel_id", "teams_enabled",
            "teams_channel_name", "teams_hot_threshold",
        ])
    ).all():
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
def api_set_teams_config(
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
    log.info(f"Teams config updated by {user.email}: team={body.team_id}, channel={body.channel_id}, enabled={body.enabled}")
    return {"status": "saved"}


@router.get("/api/admin/teams/channels")
async def api_list_teams_channels(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List Teams channels the user has access to via Graph API."""
    from ..scheduler import get_valid_token
    from ..utils.graph_client import GraphClient

    token = await get_valid_token(user, db)
    if not token:
        raise HTTPException(400, "No valid Microsoft 365 token. Please reconnect M365.")

    gc = GraphClient(token)

    # Get teams the user is a member of
    teams_result = await gc.get_json("/me/joinedTeams", params={"$select": "id,displayName"})
    if "error" in teams_result:
        raise HTTPException(502, f"Graph API error: {teams_result.get('error', {}).get('message', 'Unknown')}")

    teams_list = teams_result.get("value", [])
    result = []

    for team in teams_list:
        channels_result = await gc.get_json(
            f"/teams/{team['id']}/channels",
            params={"$select": "id,displayName,membershipType"},
        )
        channels = channels_result.get("value", [])
        for ch in channels:
            result.append({
                "team_id": team["id"],
                "team_name": team.get("displayName", ""),
                "channel_id": ch["id"],
                "channel_name": ch.get("displayName", ""),
                "membership_type": ch.get("membershipType", ""),
            })

    return {"channels": result}


@router.post("/api/admin/teams/test")
async def api_test_teams_post(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Send a test Adaptive Card to the configured Teams channel."""
    from ..services.teams import _get_teams_config, _make_card, post_to_channel
    from ..scheduler import get_valid_token

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
            key=key, value=value, updated_by=admin_email,
            description=f"Teams integration: {key}",
        )
        db.add(row)
