"""Knowledge Ledger API — CRUD, Q&A, and AI insights endpoints.

Provides endpoints for managing knowledge entries, posting Q&A questions
and answers, and generating/retrieving AI insights for requisitions.

Called by: frontend (app.js, crm.js)
Depends on: services/knowledge_service.py, dependencies.py
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user
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


@router.put("/{entry_id}")
def update_entry_endpoint(
    entry_id: int,
    payload: KnowledgeEntryUpdate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entry = knowledge_service.update_entry(
        db, entry_id, user.id,
        content=payload.content,
        is_resolved=payload.is_resolved,
        expires_at=payload.expires_at,
    )
    if not entry:
        raise HTTPException(404, "Entry not found")
    return _entry_to_response(entry)


@router.delete("/{entry_id}")
def delete_entry_endpoint(
    entry_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    if not knowledge_service.delete_entry(db, entry_id, user.id):
        raise HTTPException(404, "Entry not found")
    return {"ok": True}


@router.post("/question")
def post_question(
    payload: QuestionCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
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


@router.post("/{entry_id}/answer")
def post_answer(
    entry_id: int,
    payload: AnswerCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    answer = knowledge_service.post_answer(
        db, user_id=user.id, question_id=entry_id, content=payload.content
    )
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
    entries = knowledge_service.get_cached_insights(db, req_id)
    now = datetime.now(timezone.utc)
    return {
        "requisition_id": req_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": any(e.expires_at and e.expires_at < now for e in entries),
    }


@insights_router.post("/{req_id}/insights/refresh")
async def refresh_insights(
    req_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    entries = await knowledge_service.generate_insights(db, req_id)
    now = datetime.now(timezone.utc)
    return {
        "requisition_id": req_id,
        "insights": [_entry_to_response(e, now) for e in entries],
        "generated_at": entries[0].created_at.isoformat() if entries else None,
        "has_expired": False,
    }
