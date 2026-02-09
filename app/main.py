"""
AvailAI — Supplier Sourcing Engine
Everything runs from this one file. All routes are here.
"""
import asyncio
import structlog
from contextlib import asynccontextmanager
from uuid import UUID
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from authlib.integrations.starlette_client import OAuth
from sqlalchemy import select, and_, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import get_settings
from app.database import get_db, create_tables, get_db_session
from app.models import User, Vendor, OutreachLog, VendorResponse, Sighting, Upload
from app.scoring import normalize_part_number
from app import search_service, email_service

structlog.configure(processors=[
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.add_log_level,
    structlog.dev.ConsoleRenderer(),
])
logger = structlog.get_logger()
settings = get_settings()


# --- OAuth Setup ---
oauth = OAuth()
oauth.register(
    name="microsoft",
    client_id=settings.azure_client_id,
    client_secret=settings.azure_client_secret,
    server_metadata_url=f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile User.Read Mail.Send Mail.Read"},
)


# --- Background polling for vendor replies ---
_bg_token: dict = {}  # stores the latest access token for background polling

async def _poll_loop():
    interval = settings.poll_interval_minutes * 60
    logger.info("poll_loop_started", interval_min=settings.poll_interval_minutes)
    while True:
        await asyncio.sleep(interval)
        token = _bg_token.get("token")
        email = _bg_token.get("email")
        if not token or not email:
            continue
        try:
            async with get_db_session() as db:
                stats = await email_service.poll_for_replies(db, token, email)
                if stats["new_replies"]:
                    logger.info("bg_poll", **stats)
        except Exception as e:
            logger.error("bg_poll_error", error=str(e))


# --- App Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    logger.info("tables_ready")
    task = asyncio.create_task(_poll_loop())
    yield
    task.cancel()


# --- FastAPI App ---
app = FastAPI(title="AvailAI", version="0.3.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# --- Auth Helpers ---
def require_login(request: Request) -> str:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Not logged in")
    return uid

def require_token(request: Request) -> str:
    token = request.session.get("access_token")
    if not token:
        raise HTTPException(401, "No access token")
    return token


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user_id = request.session.get("user_id")
    user_name = request.session.get("user_name", "")
    # Keep token fresh for background poller
    token = request.session.get("access_token")
    email = request.session.get("user_email")
    if token and email:
        _bg_token["token"] = token
        _bg_token["email"] = email
    return templates.TemplateResponse("index.html", {
        "request": request, "logged_in": user_id is not None, "user_name": user_name,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/auth/login")
async def login(request: Request):
    return await oauth.microsoft.authorize_redirect(request, f"{settings.app_url}/auth/callback")

@app.get("/auth/callback")
async def callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        token = await oauth.microsoft.authorize_access_token(request)
    except Exception as e:
        raise HTTPException(400, f"Login failed: {e}")

    info = token.get("userinfo", {})
    r = await db.execute(select(User).where(User.microsoft_id == info["sub"]))
    user = r.scalar_one_or_none()

    if not user:
        user = User(email=info.get("email", ""), display_name=info.get("name", ""),
                     microsoft_id=info["sub"])
        db.add(user)

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    request.session["user_id"] = str(user.id)
    request.session["user_name"] = user.display_name
    request.session["user_email"] = user.email
    request.session["access_token"] = token["access_token"]
    return RedirectResponse("/")

@app.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH
# ═══════════════════════════════════════════════════════════════════════════════

class SearchRequest(BaseModel):
    part_numbers: list[str]
    include_historical: bool = True
    target_qty: Optional[int] = None

@app.post("/api/search")
async def search(body: SearchRequest, db: AsyncSession = Depends(get_db),
                 user_id: str = Depends(require_login)):
    return await search_service.search_parts(
        db, body.part_numbers, UUID(user_id), body.target_qty, body.include_historical
    )


# ═══════════════════════════════════════════════════════════════════════════════
# UPLOAD
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/uploads")
async def upload_file(file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                      user_id: str = Depends(require_login)):
    data = await file.read()
    if len(data) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(400, f"File too large (max {settings.max_upload_size_mb} MB)")
    return await search_service.process_upload(db, data, file.filename, UUID(user_id))

@app.get("/api/stats/uploads")
async def upload_stats(db: AsyncSession = Depends(get_db), _=Depends(require_login)):
    r = await db.execute(
        select(
            User.display_name,
            func.count(Upload.id).label("upload_count"),
            func.coalesce(func.sum(Upload.sighting_count), 0).label("sighting_count"),
        )
        .join(Upload, Upload.user_id == User.id)
        .group_by(User.id)
        .order_by(func.sum(Upload.sighting_count).desc())
        .limit(10)
    )
    return {"users": [{"display_name": row[0], "upload_count": row[1],
                        "sighting_count": row[2]} for row in r.fetchall()]}


# ═══════════════════════════════════════════════════════════════════════════════
# OUTREACH (SEND RFQ)
# ═══════════════════════════════════════════════════════════════════════════════

class PreviewRequest(BaseModel):
    vendor_ids: list[str]
    part_numbers: list[str]
    quantities: Optional[dict[str, int]] = None

class SendRequest(BaseModel):
    vendor_ids: list[str]
    part_numbers: list[str]
    subject: str
    body: str
    bcc_email: Optional[str] = None

@app.post("/api/outreach/preview")
async def preview(body: PreviewRequest, request: Request,
                  db: AsyncSession = Depends(get_db), _=Depends(require_login)):
    vendor_uuids = [UUID(v) for v in body.vendor_ids]
    normalized = [normalize_part_number(pn) for pn in body.part_numbers]

    r = await db.execute(select(Vendor).where(Vendor.id.in_(vendor_uuids)))
    vendors = r.scalars().all()

    # Check cooldown exclusions
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.outreach_cooldown_days)
    r = await db.execute(
        select(OutreachLog).where(and_(
            OutreachLog.vendor_id.in_(vendor_uuids),
            OutreachLog.part_number_normalized.in_(normalized),
            OutreachLog.sent_at >= cutoff,
        ))
    )
    excluded = {(log.vendor_id, log.part_number_normalized) for log in r.scalars()}

    vendor_list = []
    for v in vendors:
        is_excluded = any((v.id, pn) in excluded for pn in normalized)
        vendor_list.append({
            "id": str(v.id), "name": v.name, "email": v.email,
            "excluded": is_excluded,
            "exclusion_reason": "Recently contacted" if is_excluded else None,
        })

    subject, draft = email_service.generate_rfq_draft(
        body.part_numbers, body.quantities, request.session.get("user_name", "")
    )
    return {"vendors": vendor_list, "draft_subject": subject, "draft_body": draft}

@app.post("/api/outreach/send")
async def send(body: SendRequest, db: AsyncSession = Depends(get_db),
               user_id: str = Depends(require_login), token: str = Depends(require_token)):
    results = await email_service.send_rfq(
        db, token, UUID(user_id), [UUID(v) for v in body.vendor_ids],
        body.part_numbers, body.subject, body.body, body.bcc_email
    )
    return {"sent_count": sum(1 for r in results if r["status"] == "sent"), "results": results}


# ═══════════════════════════════════════════════════════════════════════════════
# RESPONSE MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/monitor/poll")
async def poll(request: Request, db: AsyncSession = Depends(get_db),
               user_id: str = Depends(require_login), token: str = Depends(require_token)):
    r = await db.execute(select(User).where(User.id == user_id))
    user = r.scalar_one_or_none()
    if not user:
        raise HTTPException(404)
    return await email_service.poll_for_replies(db, token, user.email)

@app.get("/api/monitor/responses")
async def list_responses(status: Optional[str] = None, db: AsyncSession = Depends(get_db),
                         _=Depends(require_login)):
    q = (select(VendorResponse)
         .options(joinedload(VendorResponse.vendor), joinedload(VendorResponse.outreach_log))
         .order_by(VendorResponse.reply_received_at.desc()).limit(50))
    if status:
        q = q.where(VendorResponse.status == status)
    r = await db.execute(q)
    responses = r.scalars().unique().all()

    return {"count": len(responses), "responses": [{
        "id": str(vr.id),
        "vendor_name": vr.vendor.name if vr.vendor else "Unknown",
        "vendor_id": str(vr.vendor_id),
        "part_number": vr.part_number,
        "received_at": vr.reply_received_at.isoformat() if vr.reply_received_at else None,
        "from_email": vr.reply_from_email,
        "from_name": vr.reply_from_name,
        "has_stock": vr.has_stock,
        "quoted_price": float(vr.quoted_price) if vr.quoted_price else None,
        "quoted_currency": vr.quoted_currency,
        "quoted_quantity": vr.quoted_quantity,
        "quoted_moq": vr.quoted_moq,
        "quoted_lead_time_days": vr.quoted_lead_time_days,
        "quoted_lead_time_text": vr.quoted_lead_time_text,
        "quoted_condition": vr.quoted_condition,
        "quoted_date_code": vr.quoted_date_code,
        "quoted_manufacturer": vr.quoted_manufacturer,
        "confidence": round(vr.parse_confidence or 0, 2),
        "parse_notes": vr.parse_notes,
        "status": vr.status,
        "sighting_id": str(vr.sighting_id) if vr.sighting_id else None,
        "email_preview": (vr.reply_body_text or "")[:300],
        "original_subject": vr.outreach_log.email_subject if vr.outreach_log else None,
    } for vr in responses]}

@app.post("/api/monitor/responses/{response_id}/approve")
async def approve(response_id: str, db: AsyncSession = Depends(get_db), _=Depends(require_login)):
    r = await db.execute(select(VendorResponse).where(VendorResponse.id == response_id))
    vr = r.scalar_one_or_none()
    if not vr:
        raise HTTPException(404)
    if vr.status in ("sighting_created", "approved") and vr.sighting_id:
        return {"status": "already_created"}

    s = Sighting(
        vendor_id=vr.vendor_id, part_number=vr.part_number or "",
        part_number_normalized=vr.part_number_normalized or "",
        manufacturer=vr.quoted_manufacturer, quantity=vr.quoted_quantity,
        price=vr.quoted_price, currency=vr.quoted_currency or "USD",
        lead_time_days=vr.quoted_lead_time_days, lead_time_text=vr.quoted_lead_time_text,
        condition=vr.quoted_condition, date_code=vr.quoted_date_code,
        source_type="email_reply", confidence=5, evidence_type="direct_offer",
        is_exact_match=True, seen_at=vr.reply_received_at or datetime.now(timezone.utc),
    )
    db.add(s)
    await db.flush()
    vr.sighting_id = s.id
    vr.status = "approved"
    vr.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "approved", "sighting_id": str(s.id)}

@app.post("/api/monitor/responses/{response_id}/reject")
async def reject(response_id: str, db: AsyncSession = Depends(get_db), _=Depends(require_login)):
    r = await db.execute(select(VendorResponse).where(VendorResponse.id == response_id))
    vr = r.scalar_one_or_none()
    if not vr:
        raise HTTPException(404)
    vr.status = "rejected"
    vr.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "rejected"}

@app.get("/api/monitor/stats")
async def monitor_stats(db: AsyncSession = Depends(get_db), user_id: str = Depends(require_login)):
    return await email_service.get_monitor_stats(db, user_id)

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.3.0"}
