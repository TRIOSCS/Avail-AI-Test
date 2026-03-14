"""Knowledge Ledger API — CRUD, Q&A, and AI insights endpoints.

Provides endpoints for managing knowledge entries, posting Q&A questions
and answers, and generating/retrieving AI insights for requisitions.

Called by: frontend (app.js, crm.js)
Depends on: services/knowledge_service.py, dependencies.py
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_admin, require_user
from app.schemas.knowledge import (
    AnswerCreate,
    KnowledgeEntryCreate,
    KnowledgeEntryUpdate,
    QuestionCreate,
)
from app.services import knowledge_service

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _entry_to_response(entry, now=None) -> dict:
    """Convert a KnowledgeEntry to a response dict."""
    if now is None:
        now = datetime.now(timezone.utc)
    is_expired = bool(entry.expires_at and entry.expires_at < now)
    answers = []
    if hasattr(entry, "answers") and entry.answers:
        answers = [_entry_to_response(a, now) for a in entry.answers]
    return {
        "id": entry.id,
        "entry_type": entry.entry_type,
        "content": entry.content,
        "source": entry.source,
        "confidence": entry.confidence,
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        "is_expired": is_expired,
        "is_resolved": entry.is_resolved,
        "parent_id": entry.parent_id,
        "assigned_to_ids": entry.assigned_to_ids or [],
        "created_by": entry.created_by,
        "creator_name": entry.creator.display_name if entry.creator else None,
        "mpn": entry.mpn,
        "vendor_card_id": entry.vendor_card_id,
        "company_id": entry.company_id,
        "requisition_id": entry.requisition_id,
        "requirement_id": entry.requirement_id,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "answers": answers,
    }


@router.get("")
def list_entries(
    requisition_id: int | None = Query(None),
    company_id: int | None = Query(None),
    vendor_card_id: int | None = Query(None),
    mpn: str | None = Query(None),
    entry_type: str | None = Query(None),
    include_expired: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_entries(
        db,
        requisition_id=requisition_id,
        company_id=company_id,
        vendor_card_id=vendor_card_id,
        mpn=mpn,
        entry_type=entry_type,
        include_expired=include_expired,
        limit=limit,
        offset=offset,
    )
    now = datetime.now(timezone.utc)
    return [_entry_to_response(e, now) for e in entries]


@router.post("")
def create_entry_endpoint(
    payload: KnowledgeEntryCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entry = knowledge_service.create_entry(
        db,
        user_id=user.id,
        entry_type=payload.entry_type,
        content=payload.content,
        source=payload.source,
        confidence=payload.confidence,
        expires_at=payload.expires_at,
        mpn=payload.mpn,
        vendor_card_id=payload.vendor_card_id,
        company_id=payload.company_id,
        requisition_id=payload.requisition_id,
        requirement_id=payload.requirement_id,
    )
    return _entry_to_response(entry)


@router.put("/{entry_id:int}")
def update_entry_endpoint(
    entry_id: int,
    payload: KnowledgeEntryUpdate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entry = knowledge_service.update_entry(
        db,
        entry_id,
        user.id,
        content=payload.content,
        is_resolved=payload.is_resolved,
        expires_at=payload.expires_at,
    )
    if not entry:
        raise HTTPException(404, "Entry not found")
    return _entry_to_response(entry)


@router.delete("/{entry_id:int}")
def delete_entry_endpoint(
    entry_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    if not knowledge_service.delete_entry(db, entry_id, user.id):
        raise HTTPException(404, "Entry not found")
    return {"ok": True}


@router.get("/quota")
def get_quota(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Get the user's daily question quota (Teams removed — returns unlimited)."""
    return {"allowed": True, "used": 0, "limit": 999, "remaining": 999}


@router.get("/config")
def get_knowledge_config(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Get knowledge config values."""
    from app.models.knowledge import KnowledgeConfig

    rows = db.query(KnowledgeConfig).all()
    return {row.key: row.value for row in rows}


@router.put("/config")
def update_knowledge_config(
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(require_admin),
):
    """Update knowledge config (admin only). Body: {key: value, ...}."""
    from app.models.knowledge import KnowledgeConfig

    for key, value in payload.items():
        row = db.query(KnowledgeConfig).filter(KnowledgeConfig.key == key).first()
        if row:
            row.value = str(value)
        else:
            db.add(KnowledgeConfig(key=key, value=str(value)))
    db.commit()
    return {"ok": True}


@router.post("/question")
def post_question(
    payload: QuestionCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    try:
        entry = knowledge_service.post_question(
            db,
            user_id=user.id,
            content=payload.content,
            assigned_to_ids=payload.assigned_to_ids,
            mpn=payload.mpn,
            vendor_card_id=payload.vendor_card_id,
            company_id=payload.company_id,
            requisition_id=payload.requisition_id,
            requirement_id=payload.requirement_id,
        )
        return _entry_to_response(entry)
    except ValueError as e:
        raise HTTPException(429, str(e))


@router.post("/{entry_id}/answer")
def post_answer(
    entry_id: int,
    payload: AnswerCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    answer = knowledge_service.post_answer(db, user_id=user.id, question_id=entry_id, content=payload.content)
    if not answer:
        raise HTTPException(404, "Question not found")
    return _entry_to_response(answer)


# --- AI Insights (on requisition) ---

insights_router = APIRouter(prefix="/api/requisitions", tags=["knowledge"])


@insights_router.get("/{req_id}/insights")
def get_insights(
    req_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    try:
        entries = knowledge_service.get_cached_insights(db, req_id)
        now = datetime.now(timezone.utc)
        return {
            "requisition_id": req_id,
            "insights": [_entry_to_response(e, now) for e in entries],
            "generated_at": entries[0].created_at.isoformat() if entries else None,
            "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
        }
    except Exception as e:
        logger.warning("Failed to load insights for req {}: {}", req_id, e)
        return {
            "requisition_id": req_id,
            "insights": [],
            "generated_at": None,
            "has_expired": False,
        }


@insights_router.post("/{req_id}/insights/refresh")
async def refresh_insights(
    req_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    try:
        entries = await knowledge_service.generate_insights(db, req_id)
        now = datetime.now(timezone.utc)
        return {
            "requisition_id": req_id,
            "insights": [_entry_to_response(e, now) for e in entries],
            "generated_at": entries[0].created_at.isoformat() if entries else None,
            "has_expired": False,
        }
    except Exception as e:
        logger.warning("Failed to generate insights for req {}: {}", req_id, e)
        return {
            "requisition_id": req_id,
            "insights": [],
            "generated_at": None,
            "has_expired": False,
        }


# --- Phase 3: AI Sprinkles endpoints ---

sprinkles_router = APIRouter(prefix="/api", tags=["ai-sprinkles"])


@sprinkles_router.get("/vendors/{vendor_id}/insights")
def get_vendor_insights(
    vendor_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_cached_vendor_insights(db, vendor_id)
    now = datetime.now(timezone.utc)
    return {
        "vendor_card_id": vendor_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@sprinkles_router.post("/vendors/{vendor_id}/insights/refresh")
async def refresh_vendor_insights(
    vendor_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_vendor_insights(db, vendor_id)
    now = datetime.now(timezone.utc)
    return {
        "vendor_card_id": vendor_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }


@sprinkles_router.get("/companies/{company_id}/insights")
def get_company_insights(
    company_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_cached_company_insights(db, company_id)
    now = datetime.now(timezone.utc)
    return {
        "company_id": company_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@sprinkles_router.post("/companies/{company_id}/insights/refresh")
async def refresh_company_insights(
    company_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_company_insights(db, company_id)
    now = datetime.now(timezone.utc)
    return {
        "company_id": company_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }


@sprinkles_router.get("/dashboard/pipeline-insights")
def get_pipeline_insights(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_cached_pipeline_insights(db)
    now = datetime.now(timezone.utc)
    return {
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@sprinkles_router.post("/dashboard/pipeline-insights/refresh")
async def refresh_pipeline_insights(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_pipeline_insights(db)
    now = datetime.now(timezone.utc)
    return {
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }


@sprinkles_router.get("/materials/insights")
def get_mpn_insights(
    mpn: str = Query(...),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = knowledge_service.get_cached_mpn_insights(db, mpn)
    now = datetime.now(timezone.utc)
    return {
        "mpn": mpn,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@sprinkles_router.post("/materials/insights/refresh")
async def refresh_mpn_insights(
    mpn: str = Query(...),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_mpn_insights(db, mpn)
    now = datetime.now(timezone.utc)
    return {
        "mpn": mpn,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }
