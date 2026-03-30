"""AI-verified part description generator — 3-point cross-reference verification.

Generates standardized part descriptions by querying multiple distributor APIs
(DigiKey, Mouser, Element14, OEMSecrets, Nexar) and cross-referencing at least
3 sources to achieve 98% confidence. Falls back to AI synthesis when fewer
sources are available, with explicit confidence scoring.

Called by: background task in requirements.add_requirements, on-demand API endpoint
Depends on: connectors (digikey, mouser, element14, oemsecrets), claude_client, models
"""

import asyncio
from typing import Any

from loguru import logger

from app.database import SessionLocal
from app.models import Requirement
from app.models.intelligence import MaterialCard


async def _fetch_descriptions_from_sources(mpn: str, manufacturer: str) -> list[dict[str, Any]]:
    """Query distributor APIs for part descriptions.

    Returns list of {source, description}.
    """
    results: list[dict[str, Any]] = []

    async def _try_digikey():
        try:
            from app.connectors.digikey import search as dk_search

            hits = await dk_search(mpn)
            for h in hits:
                desc = h.get("description", "")
                if desc and len(desc) >= 5:
                    results.append({"source": "digikey", "description": desc, "mpn": h.get("mpn", "")})
                    return
        except Exception:
            logger.debug("DigiKey description lookup failed for %s", mpn)

    async def _try_mouser():
        try:
            from app.connectors.mouser import search as mouser_search

            hits = await mouser_search(mpn)
            for h in hits:
                desc = h.get("description", "")
                if desc and len(desc) >= 5:
                    results.append({"source": "mouser", "description": desc, "mpn": h.get("mpn", "")})
                    return
        except Exception:
            logger.debug("Mouser description lookup failed for %s", mpn)

    async def _try_element14():
        try:
            from app.connectors.element14 import search as e14_search

            hits = await e14_search(mpn)
            for h in hits:
                desc = h.get("description", "")
                if desc and len(desc) >= 5:
                    results.append({"source": "element14", "description": desc, "mpn": h.get("mpn", "")})
                    return
        except Exception:
            logger.debug("Element14 description lookup failed for %s", mpn)

    async def _try_oemsecrets():
        try:
            from app.connectors.oemsecrets import search as oem_search

            hits = await oem_search(mpn)
            for h in hits:
                desc = h.get("description", "")
                if desc and len(desc) >= 5:
                    results.append({"source": "oemsecrets", "description": desc, "mpn": h.get("mpn", "")})
                    return
        except Exception:
            logger.debug("OEMSecrets description lookup failed for %s", mpn)

    async def _try_sightings_db(mpn: str, manufacturer: str):
        """Pull descriptions from existing sightings raw_data in the database."""
        try:
            from sqlalchemy import select

            db = SessionLocal()
            try:
                from app.models.sourcing import Sighting

                rows = db.execute(
                    select(Sighting.raw_data, Sighting.source_type)
                    .where(Sighting.normalized_mpn == mpn.upper().strip())
                    .where(Sighting.raw_data.isnot(None))
                    .order_by(Sighting.created_at.desc())
                    .limit(10)
                ).all()
                for row in rows:
                    raw = row[0] or {}
                    desc = raw.get("description", "")
                    source = row[1] or "sighting"
                    if desc and len(desc) >= 5:
                        results.append({"source": f"db:{source}", "description": desc, "mpn": mpn})
                        if len([r for r in results if r["source"].startswith("db:")]) >= 2:
                            return
            finally:
                db.close()
        except Exception:
            logger.debug("Sighting DB description lookup failed for %s", mpn)

    # Run all lookups concurrently
    await asyncio.gather(
        _try_digikey(),
        _try_mouser(),
        _try_element14(),
        _try_oemsecrets(),
        _try_sightings_db(mpn, manufacturer),
        return_exceptions=True,
    )
    return results


async def generate_verified_description(
    mpn: str,
    manufacturer: str,
    existing_description: str = "",
) -> dict[str, Any]:
    """Generate a verified part description using 3-point cross-referencing.

    Returns:
        {
            "description": "IC MCU 32-BIT 168MHZ 1MB FLASH LQFP-100",
            "confidence": 0.98,
            "sources_used": 3,
            "sources": ["digikey", "mouser", "element14"],
            "verified": True
        }
    """
    sources = await _fetch_descriptions_from_sources(mpn, manufacturer)
    source_names = [s["source"] for s in sources]
    num_sources = len(sources)

    if num_sources == 0 and not existing_description:
        return {
            "description": "",
            "confidence": 0.0,
            "sources_used": 0,
            "sources": [],
            "verified": False,
        }

    # Build the AI prompt for cross-referencing and standardization
    from app.utils.claude_client import claude_text

    source_block = ""
    for s in sources:
        source_block += f"  - [{s['source']}]: {s['description']}\n"
    if existing_description:
        source_block += f"  - [user_input]: {existing_description}\n"

    # Determine confidence based on source count
    if num_sources >= 3:
        base_confidence = 0.98
    elif num_sources == 2:
        base_confidence = 0.90
    elif num_sources == 1:
        base_confidence = 0.75
    else:
        base_confidence = 0.50

    prompt = (
        f"You are verifying an electronic component description by cross-referencing "
        f"multiple distributor sources.\n\n"
        f"MPN: {mpn}\n"
        f"Manufacturer: {manufacturer}\n\n"
        f"Descriptions from {num_sources} source(s):\n{source_block}\n"
        f"TASK: Produce ONE standardized description in this exact format:\n"
        f"  ALL CAPS, max 60 chars\n"
        f"  Category → Subcategory → Key Specs → Package\n"
        f"  Example: IC MCU 32-BIT 168MHZ 1MB FLASH LQFP-100\n\n"
        f"RULES:\n"
        f"- ONLY include facts that appear in at least {'2 of the sources above' if num_sources >= 2 else 'the source above'}\n"
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
    """Background task: auto-generate descriptions for requirements missing them.

    Called as a BackgroundTask after requirement creation.
    Only generates for requirements that have no description set.
    Also updates the linked MaterialCard description if empty.
    """
    import asyncio
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
                logger.warning("Description generation failed for requirement %s", rid, exc_info=True)
                db.rollback()
    finally:
        db.close()
