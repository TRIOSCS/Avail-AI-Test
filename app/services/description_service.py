"""AI-verified part description generator — DB-only 3-point cross-reference.

Generates standardized part descriptions by mining existing data already in the
database: MaterialCard enrichment, sighting raw_data from prior searches. Does
NOT call external distributor APIs — the full search background task already
fetches that data. This service only synthesizes what we already have.

Called by: background task in requirements.add_requirements, on-demand API endpoint
Depends on: claude_client, models (Sighting, MaterialCard)
"""

import asyncio
from typing import Any

from loguru import logger

from app.database import SessionLocal
from app.models import Requirement
from app.models.intelligence import MaterialCard


def _collect_db_descriptions(mpn: str, manufacturer: str) -> list[dict[str, Any]]:
    """Collect part descriptions from data already in the database.

    Sources checked (zero API calls):
    1. MaterialCard.description — AI-enriched by material_enrichment_service
    2. Sighting.raw_data.description — populated by connector searches
    3. Requirement.description — user-entered on other requisitions for same MPN
    """
    results: list[dict[str, Any]] = []
    seen_descriptions: set[str] = set()
    norm_mpn = mpn.upper().strip()

    db = SessionLocal()
    try:
        from sqlalchemy import select

        from app.models.sourcing import Sighting

        # Source 1: MaterialCard enrichment (highest quality — AI-verified)
        card = db.execute(
            select(MaterialCard.description, MaterialCard.enrichment_source)
            .where(MaterialCard.normalized_mpn == norm_mpn)
            .where(MaterialCard.description.isnot(None))
            .where(MaterialCard.description != "")
            .limit(1)
        ).first()
        if card and card[0] and len(card[0].strip()) >= 5:
            desc = card[0].strip()
            seen_descriptions.add(desc.upper())
            results.append(
                {
                    "source": f"material_card:{card[1] or 'enriched'}",
                    "description": desc,
                }
            )

        # Source 2: Sighting raw_data — descriptions from DigiKey, Mouser, etc.
        # already stored from prior search_requirement() calls
        rows = db.execute(
            select(Sighting.raw_data, Sighting.source_type)
            .where(Sighting.normalized_mpn == norm_mpn)
            .where(Sighting.raw_data.isnot(None))
            .order_by(Sighting.created_at.desc())
            .limit(20)
        ).all()
        for row in rows:
            raw = row[0] or {}
            desc = (raw.get("description") or "").strip()
            source = row[1] or "sighting"
            if desc and len(desc) >= 5 and desc.upper() not in seen_descriptions:
                seen_descriptions.add(desc.upper())
                results.append({"source": source, "description": desc})
                if len(results) >= 5:
                    break

        # Source 3: Other requirements for same MPN (user-entered descriptions)
        req_rows = db.execute(
            select(Requirement.description)
            .where(Requirement.normalized_mpn == norm_mpn)
            .where(Requirement.description.isnot(None))
            .where(Requirement.description != "")
            .order_by(Requirement.created_at.desc())
            .limit(3)
        ).all()
        for req_row in req_rows:
            desc = (req_row[0] or "").strip()
            if desc and len(desc) >= 5 and desc.upper() not in seen_descriptions:
                seen_descriptions.add(desc.upper())
                results.append({"source": "user_input", "description": desc})
                if len(results) >= 5:
                    break

    except Exception:
        logger.warning("DB description collection failed for %s", mpn, exc_info=True)
    finally:
        db.close()

    return results


async def generate_verified_description(
    mpn: str,
    manufacturer: str,
    existing_description: str = "",
) -> dict[str, Any]:
    """Generate a verified part description using DB-only 3-point cross-referencing.

    Does NOT call external APIs. Only uses data already in the database from
    prior searches and enrichment jobs.

    Returns:
        {
            "description": "IC MCU 32-BIT 168MHZ 1MB FLASH LQFP-100",
            "confidence": 0.98,
            "sources_used": 3,
            "sources": ["material_card:claude_haiku", "digikey", "mouser"],
            "verified": True
        }
    """
    sources = await asyncio.to_thread(_collect_db_descriptions, mpn, manufacturer)
    source_names = [s["source"] for s in sources]
    num_sources = len(sources)

    # Include existing description as an additional source if provided
    if existing_description and len(existing_description.strip()) >= 5:
        already_seen = {s["description"].upper() for s in sources}
        if existing_description.upper().strip() not in already_seen:
            sources.append({"source": "user_input", "description": existing_description})
            source_names.append("user_input")
            num_sources = len(sources)

    if num_sources == 0:
        return {
            "description": "",
            "confidence": 0.0,
            "sources_used": 0,
            "sources": [],
            "verified": False,
        }

    # Determine confidence based on distinct source count
    if num_sources >= 3:
        base_confidence = 0.98
    elif num_sources == 2:
        base_confidence = 0.90
    elif num_sources == 1:
        base_confidence = 0.75
    else:
        base_confidence = 0.50

    # Build the AI prompt for cross-referencing and standardization
    from app.utils.claude_client import claude_text

    source_block = ""
    for s in sources:
        source_block += f"  - [{s['source']}]: {s['description']}\n"

    prompt = (
        f"You are verifying an electronic component description by cross-referencing "
        f"multiple data sources already in our database.\n\n"
        f"MPN: {mpn}\n"
        f"Manufacturer: {manufacturer}\n\n"
        f"Descriptions from {num_sources} source(s):\n{source_block}\n"
        f"TASK: Produce ONE standardized description in this exact format:\n"
        f"  ALL CAPS, max 60 chars\n"
        f"  Category → Subcategory → Key Specs → Package\n"
        f"  Example: IC MCU 32-BIT 168MHZ 1MB FLASH LQFP-100\n\n"
        f"RULES:\n"
        f"- ONLY include facts that appear in at least "
        f"{'2 of the sources above' if num_sources >= 2 else 'the source above'}\n"
        f"- If sources conflict on a spec, OMIT that spec rather than guess\n"
        f"- Do NOT hallucinate specs not present in any source\n"
        f"- Category first (IC, CONNECTOR, RESISTOR, CAPACITOR, DIODE, etc.)\n"
        f"- Then subcategory (MCU, OPAMP, USB, MLCC, SCHOTTKY, etc.)\n"
        f"- Then key specs (voltage, current, freq, memory, bits, etc.)\n"
        f"- Then package if known (QFP-100, 0402, SOIC-8, etc.)\n\n"
        f"Return ONLY the standardized description, nothing else."
    )

    result = await claude_text(prompt, model_tier="fast", max_tokens=100)
    if result:
        result = result.strip().strip('"').strip("'").upper()

    description = result if result else (existing_description.upper() if existing_description else "")

    return {
        "description": description,
        "confidence": base_confidence,
        "sources_used": num_sources,
        "sources": source_names,
        "verified": num_sources >= 3,
    }


def backfill_descriptions(requirement_ids: list[int]) -> None:
    """Background task: generate descriptions for requirements missing them.

    Called as a BackgroundTask after requirement creation. Runs AFTER the full
    search completes so sighting raw_data is available. Only uses DB data —
    no external API calls.
    """
    import os

    if os.environ.get("TESTING"):
        return

    db = SessionLocal()
    try:
        for rid in requirement_ids:
            try:
                req = db.get(Requirement, rid)
                if not req:
                    continue
                # Skip if already has a meaningful description
                if req.description and len(req.description.strip()) >= 5:
                    continue

                result = asyncio.run(
                    generate_verified_description(
                        req.primary_mpn,
                        req.manufacturer or "",
                        req.description or "",
                    )
                )

                if result["description"] and result["confidence"] >= 0.75:
                    req.description = result["description"]
                    logger.info(
                        "Auto-generated description for requirement %s: %s (confidence=%.2f, sources=%d)",
                        rid,
                        result["description"],
                        result["confidence"],
                        result["sources_used"],
                    )

                    # Also update MaterialCard if linked and empty
                    if req.material_card_id:
                        card = db.get(MaterialCard, req.material_card_id)
                        if card and not card.description:
                            card.description = result["description"]

                    db.commit()
                else:
                    logger.debug(
                        "Skipped description for requirement %s: confidence=%.2f, sources=%d",
                        rid,
                        result.get("confidence", 0),
                        result.get("sources_used", 0),
                    )
            except Exception:
                logger.warning(
                    "Description generation failed for requirement %s",
                    rid,
                    exc_info=True,
                )
                db.rollback()
    finally:
        db.close()
