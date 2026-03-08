"""Tag query endpoints — list tags, entity tags, material card tags.

Called by: app.main (router registration)
Depends on: app.models.tags, app.schemas.tags
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user
from app.models.tags import EntityTag, MaterialTag, Tag
from app.schemas.tags import EntityTagResponse, MaterialTagResponse, TagResponse

router = APIRouter(prefix="/api/tags", tags=["tags"])


@router.get("/")
async def list_tags(
    tag_type: str | None = Query(None, description="Filter by 'brand' or 'commodity'"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user=Depends(require_user),
):
    """List all tags, optionally filtered by type."""
    q = db.query(Tag)
    if tag_type:
        q = q.filter(Tag.tag_type == tag_type)

    total = q.count()
    tags = q.order_by(Tag.name).offset(offset).limit(limit).all()

    return {
        "items": [
            TagResponse(id=t.id, name=t.name, tag_type=t.tag_type, parent_id=t.parent_id).model_dump() for t in tags
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{tag_id}/entities")
async def get_tag_entities(
    tag_id: int,
    entity_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user=Depends(require_user),
):
    """Get entities with this tag (visible only)."""
    q = db.query(EntityTag).filter(EntityTag.tag_id == tag_id, EntityTag.is_visible.is_(True))
    if entity_type:
        q = q.filter(EntityTag.entity_type == entity_type)  # pragma: no cover

    total = q.count()
    entity_tags = q.order_by(EntityTag.interaction_count.desc()).offset(offset).limit(limit).all()

    return {
        "items": [
            EntityTagResponse(
                tag=TagResponse(id=et.tag.id, name=et.tag.name, tag_type=et.tag.tag_type, parent_id=et.tag.parent_id),
                interaction_count=et.interaction_count,
                total_entity_interactions=et.total_entity_interactions,
                is_visible=et.is_visible,
                first_seen_at=et.first_seen_at,
                last_seen_at=et.last_seen_at,
                entity_type=et.entity_type,
                entity_id=et.entity_id,
            ).model_dump()
            for et in entity_tags
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/entities/{entity_type}/{entity_id}")
async def get_entity_tags(
    entity_type: str,
    entity_id: int,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
):
    """Get visible tags for an entity, sorted by interaction count desc."""
    entity_tags = (
        db.query(EntityTag)
        .filter(
            EntityTag.entity_type == entity_type,
            EntityTag.entity_id == entity_id,
            EntityTag.is_visible.is_(True),
        )
        .order_by(EntityTag.interaction_count.desc())
        .all()
    )

    return [
        EntityTagResponse(
            tag=TagResponse(id=et.tag.id, name=et.tag.name, tag_type=et.tag.tag_type, parent_id=et.tag.parent_id),
            interaction_count=et.interaction_count,
            total_entity_interactions=et.total_entity_interactions,
            is_visible=et.is_visible,
            first_seen_at=et.first_seen_at,
            last_seen_at=et.last_seen_at,
        ).model_dump()
        for et in entity_tags
    ]


@router.get("/material-cards/{material_card_id}")
async def get_material_card_tags(
    material_card_id: int,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
):
    """Get tags for a specific material card (confidence >= 0.7 only)."""
    material_tags = (
        db.query(MaterialTag)
        .filter(MaterialTag.material_card_id == material_card_id, MaterialTag.confidence >= 0.7)
        .all()
    )

    return [
        MaterialTagResponse(
            tag=TagResponse(id=mt.tag.id, name=mt.tag.name, tag_type=mt.tag.tag_type, parent_id=mt.tag.parent_id),
            confidence=mt.confidence,
            source=mt.source,
            classified_at=mt.classified_at,
        ).model_dump()
        for mt in material_tags
    ]
