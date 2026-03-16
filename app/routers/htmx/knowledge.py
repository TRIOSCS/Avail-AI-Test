"""
routers/htmx/knowledge.py — HTMX partials for knowledge ledger CRUD and AI insights.

Provides endpoints for listing, creating, updating, and deleting knowledge entries,
plus entity-scoped AI insight retrieval and refresh.

Called by: htmx router package (htmx_views.py imports this module)
Depends on: _helpers (router, templates, _base_ctx), models (KnowledgeEntry, User),
            services/knowledge_service.py, dependencies (require_user), database (get_db)
"""


from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_user
from ...models import KnowledgeEntry, User
from ._helpers import _base_ctx, router


@router.get("/v2/partials/knowledge", response_class=HTMLResponse)
async def knowledge_list_partial(
    request: Request,
    entity_type: str | None = Query(None),
    entity_id: int | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return knowledge entries as an HTML table partial."""
    query = db.query(KnowledgeEntry)

    if entity_type and entity_id:
        filter_map = {
            "requisitions": KnowledgeEntry.requisition_id,
            "vendors": KnowledgeEntry.vendor_card_id,
            "companies": KnowledgeEntry.company_id,
        }
        col = filter_map.get(entity_type)
        if col is not None:
            query = query.filter(col == entity_id)

    entries = query.order_by(KnowledgeEntry.created_at.desc()).limit(limit).all()

    ctx = _base_ctx(request, user, "knowledge")
    ctx["entries"] = entries
    ctx["entity_type"] = entity_type
    ctx["entity_id"] = entity_id

    rows_html = ""
    for e in entries:
        created = e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else ""
        rows_html += (
            f"<tr data-entry-id='{e.id}'>"
            f"<td>{e.id}</td>"
            f"<td>{e.entry_type or ''}</td>"
            f"<td>{(e.content or '')[:80]}</td>"
            f"<td>{e.source or ''}</td>"
            f"<td>{created}</td>"
            f"<td>"
            f"<button class='btn btn-sm btn-outline-danger' "
            f"hx-delete='/v2/partials/knowledge/{e.id}' "
            f"hx-target='closest tr' hx-swap='outerHTML'>Delete</button>"
            f"</td></tr>"
        )

    html = (
        "<table class='table table-sm'><thead><tr>"
        "<th>ID</th><th>Type</th><th>Content</th><th>Source</th>"
        "<th>Created</th><th>Actions</th>"
        "</tr></thead><tbody>"
        f"{rows_html}"
        "</tbody></table>"
    )
    return HTMLResponse(html)


@router.post("/v2/partials/knowledge", response_class=HTMLResponse)
async def knowledge_create_partial(
    request: Request,
    title: str = Form(...),
    content: str = Form(...),
    entity_type: str | None = Form(None),
    entity_id: int | None = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a knowledge entry and return success HTML with HX-Trigger."""
    try:
        from ...services import knowledge_service

        kwargs = {
            "user_id": user.id,
            "entry_type": "note",
            "content": f"{title}\n\n{content}",
            "source": "manual",
        }
        if entity_type and entity_id:
            link_map = {
                "requisitions": "requisition_id",
                "vendors": "vendor_card_id",
                "companies": "company_id",
            }
            fk = link_map.get(entity_type)
            if fk:
                kwargs[fk] = entity_id

        entry = knowledge_service.create_entry(db, **kwargs)
        logger.info("Knowledge entry created: id={} by user={}", entry.id, user.id)

        html = (
            f"<div class='alert alert-success' role='alert'>"
            f"Knowledge entry #{entry.id} created successfully.</div>"
        )
        return HTMLResponse(html, headers={"HX-Trigger": "knowledgeChanged"})
    except Exception as exc:
        logger.error("Failed to create knowledge entry: {}", exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Error: {exc}</div>",
            status_code=400,
        )


@router.put("/v2/partials/knowledge/{entry_id}", response_class=HTMLResponse)
async def knowledge_update_partial(
    request: Request,
    entry_id: int,
    title: str = Form(...),
    content: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update a knowledge entry and return the updated row HTML."""
    try:
        from ...services import knowledge_service

        updated = knowledge_service.update_entry(
            db, entry_id, user.id, content=f"{title}\n\n{content}"
        )
        if not updated:
            raise HTTPException(404, "Entry not found")

        db.commit()
        created = updated.created_at.strftime("%Y-%m-%d %H:%M") if updated.created_at else ""
        html = (
            f"<tr data-entry-id='{updated.id}'>"
            f"<td>{updated.id}</td>"
            f"<td>{updated.entry_type or ''}</td>"
            f"<td>{(updated.content or '')[:80]}</td>"
            f"<td>{updated.source or ''}</td>"
            f"<td>{created}</td>"
            f"<td>"
            f"<button class='btn btn-sm btn-outline-danger' "
            f"hx-delete='/v2/partials/knowledge/{updated.id}' "
            f"hx-target='closest tr' hx-swap='outerHTML'>Delete</button>"
            f"</td></tr>"
        )
        return HTMLResponse(html)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update knowledge entry {}: {}", entry_id, exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Error: {exc}</div>",
            status_code=400,
        )


@router.delete("/v2/partials/knowledge/{entry_id}", response_class=HTMLResponse)
async def knowledge_delete_partial(
    request: Request,
    entry_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a knowledge entry and return empty response with HX-Trigger."""
    try:
        from ...services import knowledge_service

        deleted = knowledge_service.delete_entry(db, entry_id, user.id)
        if not deleted:
            raise HTTPException(404, "Entry not found")

        logger.info("Knowledge entry deleted via HTMX: id={} by user={}", entry_id, user.id)
        return HTMLResponse("", headers={"HX-Trigger": "knowledgeChanged"})
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to delete knowledge entry {}: {}", entry_id, exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Error: {exc}</div>",
            status_code=400,
        )


@router.get("/v2/partials/{entity_type}/{entity_id}/insights", response_class=HTMLResponse)
async def entity_insights_partial(
    request: Request,
    entity_type: str,
    entity_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get cached AI insights for a given entity. Returns an HTML insight card."""
    if entity_type not in ("requisitions", "vendors", "companies"):
        raise HTTPException(400, f"Unsupported entity type: {entity_type}")

    try:
        from ...services import knowledge_service

        if entity_type == "requisitions":
            insights = knowledge_service.get_cached_insights(db, requisition_id=entity_id)
        else:
            insights = (
                db.query(KnowledgeEntry)
                .filter(
                    KnowledgeEntry.entry_type == "ai_insight",
                    (
                        KnowledgeEntry.vendor_card_id == entity_id
                        if entity_type == "vendors"
                        else KnowledgeEntry.company_id == entity_id
                    ),
                )
                .order_by(KnowledgeEntry.created_at.desc())
                .all()
            )

        if not insights:
            html = (
                "<div class='card'><div class='card-body text-muted'>"
                "No insights yet. Click refresh to generate."
                "</div></div>"
            )
            return HTMLResponse(html)

        cards_html = ""
        for ins in insights:
            created = ins.created_at.strftime("%Y-%m-%d %H:%M") if ins.created_at else ""
            conf = f"{ins.confidence:.0%}" if ins.confidence else "N/A"
            cards_html += (
                f"<div class='card mb-2'><div class='card-body'>"
                f"<p>{ins.content or ''}</p>"
                f"<small class='text-muted'>Confidence: {conf} | {created}</small>"
                f"</div></div>"
            )
        return HTMLResponse(cards_html)
    except Exception as exc:
        logger.error("Failed to get insights for {}/{}: {}", entity_type, entity_id, exc)
        return HTMLResponse(
            f"<div class='alert alert-warning'>Could not load insights: {exc}</div>"
        )


@router.post(
    "/v2/partials/{entity_type}/{entity_id}/insights/refresh",
    response_class=HTMLResponse,
)
async def entity_insights_refresh_partial(
    request: Request,
    entity_type: str,
    entity_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Refresh AI insights for an entity and return updated HTML insight card."""
    if entity_type not in ("requisitions", "vendors", "companies"):
        raise HTTPException(400, f"Unsupported entity type: {entity_type}")

    try:
        from ...services import knowledge_service

        if entity_type == "requisitions":
            insights = await knowledge_service.generate_insights(db, requisition_id=entity_id)
        else:
            insights = []
            logger.info(
                "Insight generation for {} not yet implemented, returning empty",
                entity_type,
            )

        if not insights:
            html = (
                "<div class='card'><div class='card-body text-muted'>"
                "No insights could be generated for this entity."
                "</div></div>"
            )
            return HTMLResponse(html)

        cards_html = ""
        for ins in insights:
            created = ins.created_at.strftime("%Y-%m-%d %H:%M") if ins.created_at else ""
            conf = f"{ins.confidence:.0%}" if ins.confidence else "N/A"
            cards_html += (
                f"<div class='card mb-2'><div class='card-body'>"
                f"<p>{ins.content or ''}</p>"
                f"<small class='text-muted'>Confidence: {conf} | {created}</small>"
                f"</div></div>"
            )
        return HTMLResponse(cards_html)
    except Exception as exc:
        logger.error("Failed to refresh insights for {}/{}: {}", entity_type, entity_id, exc)
        return HTMLResponse(
            f"<div class='alert alert-warning'>Could not generate insights: {exc}</div>"
        )
