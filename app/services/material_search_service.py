"""AI-powered material search routing.

Classifies queries as MPN (local search) or natural language (Haiku interpretation).
Called by: htmx_views materials_list_partial route.
Depends on: Anthropic API (Haiku), MaterialCard model.
"""

import anthropic
from loguru import logger
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.intelligence import MaterialCard


def classify_query(query: str) -> str:
    """Classify a search query as 'mpn' or 'natural_language'.

    Rule: 3+ whitespace-separated words = natural language, otherwise MPN.
    """
    words = query.strip().split()
    if len(words) >= 3:
        return "natural_language"
    return "mpn"


def search_materials_local(
    db: Session,
    query: str,
    lifecycle: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """Search MaterialCards using local trigram + full-text search."""
    q = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))

    if query:
        pattern = f"%{query}%"
        q = q.filter(
            or_(
                MaterialCard.normalized_mpn.ilike(pattern),
                MaterialCard.display_mpn.ilike(pattern),
                MaterialCard.manufacturer.ilike(pattern),
                MaterialCard.description.ilike(pattern),
            )
        )

    if lifecycle:
        q = q.filter(MaterialCard.lifecycle_status == lifecycle)

    total = q.count()
    materials = (
        q.order_by(MaterialCard.search_count.desc(), MaterialCard.created_at.desc()).offset(offset).limit(limit).all()
    )
    return materials, total


async def interpret_with_haiku(query: str) -> dict:
    """Send natural language query to Claude Haiku for interpretation.

    Returns dict with keys: keywords, category, description_terms.
    """
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Interpret this as an electronic component search query. "
                        "Extract search terms I can use to find matching parts in a database.\n\n"
                        f'Query: "{query}"\n\n'
                        "Reply with ONLY a JSON object (no markdown, no explanation):\n"
                        '{"keywords": ["term1", "term2"], "category": "category or empty string", '
                        '"description_terms": ["phrase1", "phrase2"]}'
                    ),
                }
            ],
        )
        import json

        text = response.content[0].text.strip()
        return json.loads(text)
    except Exception as e:
        logger.warning(f"Haiku interpretation failed: {e}")
        return {}


async def search_materials_ai(
    db: Session,
    query: str,
    lifecycle: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """Search MaterialCards using Haiku-interpreted natural language query."""
    interpretation = await interpret_with_haiku(query)

    if not interpretation:
        # Fallback to local search
        return search_materials_local(db, query, lifecycle, limit, offset), query

    # Build search from interpretation
    all_terms = interpretation.get("keywords", []) + interpretation.get("description_terms", [])
    category = interpretation.get("category", "")
    interpreted_label = ", ".join(all_terms)
    if category:
        interpreted_label = f"{category}: {interpreted_label}"

    q = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))

    if all_terms:
        conditions = []
        for term in all_terms:
            pattern = f"%{term}%"
            conditions.append(MaterialCard.description.ilike(pattern))
            conditions.append(MaterialCard.specs_summary.ilike(pattern))
            conditions.append(MaterialCard.category.ilike(pattern))
            conditions.append(MaterialCard.normalized_mpn.ilike(pattern))
        q = q.filter(or_(*conditions))

    if category:
        q = q.filter(MaterialCard.category.ilike(f"%{category}%"))

    if lifecycle:
        q = q.filter(MaterialCard.lifecycle_status == lifecycle)

    total = q.count()
    materials = q.order_by(MaterialCard.search_count.desc()).offset(offset).limit(limit).all()
    return (materials, total), interpreted_label
