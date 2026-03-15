"""On-Demand Enrichment Orchestrator — fires all enrichment sources in parallel, uses
Claude to merge conflicting data, and applies high-confidence fields.

Called by: enrichment router (POST /api/enrich/{entity_type}/{entity_id})
Depends on: enrichment_service.py provider functions, claude_client, SQLAlchemy models
"""

import asyncio
import json
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.utils.claude_client import claude_json

# ---------------------------------------------------------------------------
# Source registry — maps entity types to their enrichment source functions
# ---------------------------------------------------------------------------

COMPANY_SOURCES = {
    "apollo": "_safe_apollo_company",
    "clearbit": "_safe_clearbit",
    "gradient": "_safe_gradient",
    "explorium": "_safe_explorium",
}

CONTACT_SOURCES = {
    "apollo": "_safe_apollo_contacts",
    "lusha": "_safe_lusha",
    "hunter": "_safe_hunter",
    "rocketreach": "_safe_rocketreach",
}


# ---------------------------------------------------------------------------
# Source wrappers — each catches its own exceptions, returns dict | None
# ---------------------------------------------------------------------------


async def _safe_apollo_company(identifier: str) -> dict | None:
    """Apollo company enrichment by domain."""
    try:
        from app.connectors.apollo_client import enrich_company

        return await enrich_company(identifier)
    except Exception as e:
        logger.debug("Apollo company enrichment failed: {}", e)
        return None


async def _safe_clearbit(identifier: str) -> dict | None:
    """Clearbit company enrichment by domain."""
    try:
        from app.connectors.clearbit_client import enrich_company

        return await enrich_company(identifier)
    except Exception as e:
        logger.debug("Clearbit enrichment failed: {}", e)
        return None


async def _safe_gradient(identifier: str) -> dict | None:
    """Gradient AI company enrichment by domain."""
    try:
        from app.enrichment_service import _gradient_find_company

        return await _gradient_find_company(identifier)
    except Exception as e:
        logger.debug("Gradient enrichment failed: {}", e)
        return None


async def _safe_explorium(identifier: str) -> dict | None:
    """Explorium company enrichment by domain."""
    try:
        from app.enrichment_service import _explorium_find_company

        return await _explorium_find_company(identifier)
    except Exception as e:
        logger.debug("Explorium enrichment failed: {}", e)
        return None


async def _safe_apollo_contacts(identifier: str) -> dict | None:
    """Apollo contact search by email/name."""
    try:
        from app.connectors.apollo_client import search_contacts

        results = await search_contacts(company_name=identifier, limit=1)
        return results[0] if results else None
    except Exception as e:
        logger.debug("Apollo contact enrichment failed: {}", e)
        return None


async def _safe_lusha(identifier: str) -> dict | None:
    """Lusha contact lookup by email."""
    try:
        from app.connectors.lusha_client import find_person

        return await find_person(email=identifier)
    except Exception as e:
        logger.debug("Lusha contact enrichment failed: {}", e)
        return None


async def _safe_hunter(identifier: str) -> dict | None:
    """Hunter.io email lookup."""
    try:
        from app.connectors.hunter_client import find_domain_emails

        results = await find_domain_emails(identifier, limit=1)
        return results[0] if results else None
    except Exception as e:
        logger.debug("Hunter contact enrichment failed: {}", e)
        return None


async def _safe_rocketreach(identifier: str) -> dict | None:
    """RocketReach contact lookup."""
    try:
        from app.connectors.rocketreach_client import search_company_contacts

        results = await search_company_contacts(company=identifier, limit=1)
        return results[0] if results else None
    except Exception as e:
        logger.debug("RocketReach contact enrichment failed: {}", e)
        return None


# ---------------------------------------------------------------------------
# Resolver: function name -> actual async callable
# ---------------------------------------------------------------------------

_SOURCE_FUNCS = {
    "_safe_apollo_company": _safe_apollo_company,
    "_safe_clearbit": _safe_clearbit,
    "_safe_gradient": _safe_gradient,
    "_safe_explorium": _safe_explorium,
    "_safe_apollo_contacts": _safe_apollo_contacts,
    "_safe_lusha": _safe_lusha,
    "_safe_hunter": _safe_hunter,
    "_safe_rocketreach": _safe_rocketreach,
}


# ---------------------------------------------------------------------------
# Core orchestrator functions
# ---------------------------------------------------------------------------


async def fire_all_sources(entity_type: str, identifier: str) -> dict[str, dict | None]:
    """Fire all enrichment sources for an entity type in parallel.

    Args:
        entity_type: "company", "vendor", or "contact"
        identifier: domain for company/vendor, email or name for contact

    Returns:
        {source_name: result_dict | None} for each source
    """
    if entity_type in ("company", "vendor"):
        sources = COMPANY_SOURCES
    elif entity_type == "contact":
        sources = CONTACT_SOURCES
    else:
        logger.warning("Unknown entity type for enrichment: {}", entity_type)
        return {}

    source_names = list(sources.keys())
    func_names = list(sources.values())
    funcs = [_SOURCE_FUNCS[fn] for fn in func_names]

    logger.info(
        "Firing {} enrichment sources for {} ({})",
        len(funcs),
        entity_type,
        identifier,
    )

    raw_results = await asyncio.gather(
        *(fn(identifier) for fn in funcs),
        return_exceptions=True,
    )

    results: dict[str, dict | None] = {}
    for name, raw in zip(source_names, raw_results):
        if isinstance(raw, Exception):
            logger.warning("Source {} raised exception: {}", name, raw)
            results[name] = None
        else:
            results[name] = raw

    successful = sum(1 for v in results.values() if v is not None)
    logger.info(
        "Enrichment sources complete: {}/{} returned data",
        successful,
        len(results),
    )

    return results


MERGE_SYSTEM_PROMPT = (
    "You are a data quality expert for an electronic component sourcing platform. "
    "You will receive enrichment data from multiple sources about the same entity. "
    "For each field, pick the most reliable value from the sources provided. "
    "Assign confidence 0.0-1.0. Explain your reasoning briefly."
)

MERGE_USER_PROMPT = (
    "I have enrichment data from multiple sources for a {entity_type}. "
    "Please analyze the data and for each field, pick the best value.\n\n"
    "Source data:\n{source_data}\n\n"
    "Return a JSON array where each element has:\n"
    '{{"field": "<field_name>", "value": "<best_value>", '
    '"confidence": <0.0-1.0>, "source": "<source_name>", '
    '"reasoning": "<brief explanation>"}}\n\n'
    "Include all fields that have at least one non-null value across sources. "
    "Return ONLY the JSON array, no other text."
)


async def claude_merge(
    raw_results: dict[str, dict | None],
    entity_type: str,
) -> list[dict]:
    """Send all non-None results to Claude to merge conflicting data.

    Args:
        raw_results: {source_name: result_dict | None} from fire_all_sources
        entity_type: "company", "vendor", or "contact"

    Returns:
        [{field, value, confidence, source, reasoning}, ...]
    """
    # Filter out None results
    valid = {k: v for k, v in raw_results.items() if v is not None}

    if not valid:
        logger.info("No valid source data to merge")
        return []

    # If only one source, skip Claude call — use it directly
    if len(valid) == 1:
        source_name, data = next(iter(valid.items()))
        return [
            {
                "field": field,
                "value": value,
                "confidence": 0.85,
                "source": source_name,
                "reasoning": "Only source available",
            }
            for field, value in data.items()
            if value is not None and field != "source"
        ]

    # Format source data for Claude
    source_data = json.dumps(valid, indent=2, default=str)
    prompt = MERGE_USER_PROMPT.format(
        entity_type=entity_type,
        source_data=source_data,
    )

    logger.info("Calling Claude to merge {} sources for {}", len(valid), entity_type)

    result = await claude_json(
        prompt,
        system=MERGE_SYSTEM_PROMPT,
        model_tier="smart",
        max_tokens=2048,
        timeout=30,
    )

    if not result:
        logger.warning("Claude merge returned no data — falling back to first source")
        source_name, data = next(iter(valid.items()))
        return [
            {
                "field": field,
                "value": value,
                "confidence": 0.70,
                "source": source_name,
                "reasoning": "Fallback — Claude merge failed",
            }
            for field, value in data.items()
            if value is not None and field != "source"
        ]

    if isinstance(result, dict):
        result = [result]

    # Validate each entry has required keys
    validated = []
    for entry in result:
        if not isinstance(entry, dict):
            continue
        if "field" in entry and "value" in entry:
            validated.append(
                {
                    "field": entry["field"],
                    "value": entry["value"],
                    "confidence": float(entry.get("confidence", 0.5)),
                    "source": entry.get("source", "unknown"),
                    "reasoning": entry.get("reasoning", ""),
                }
            )

    logger.info("Claude merge produced {} field decisions", len(validated))
    return validated


def apply_confident_data(
    entity,
    merged: list[dict],
    db: Session,
    threshold: float = 0.90,
) -> dict:
    """Apply merged enrichment fields to entity if confidence >= threshold.

    Args:
        entity: SQLAlchemy model instance (Company, VendorCard, or Contact-like)
        merged: [{field, value, confidence, source, reasoning}, ...]
        db: SQLAlchemy session
        threshold: minimum confidence to apply (default 0.90)

    Returns:
        {applied: [...], rejected: [...], sources_used: [...]}
    """
    applied = []
    rejected = []
    sources_used = set()

    for item in merged:
        field = item["field"]
        value = item["value"]
        confidence = item["confidence"]
        source = item["source"]

        # Skip fields that don't exist on the entity
        if not hasattr(entity, field):
            logger.debug("Skipping field {} — not on entity {}", field, type(entity).__name__)
            continue

        if confidence >= threshold:
            setattr(entity, field, value)
            applied.append(
                {
                    "field": field,
                    "value": value,
                    "confidence": confidence,
                    "source": source,
                }
            )
            sources_used.add(source)
            logger.debug("Applied {}: {} (confidence={:.0%}, source={})", field, value, confidence, source)
        else:
            rejected.append(
                {
                    "field": field,
                    "value": value,
                    "confidence": confidence,
                    "source": source,
                    "reason": f"Below threshold ({confidence:.0%} < {threshold:.0%})",
                }
            )
            logger.debug("Rejected {}: {} (confidence={:.0%} < {:.0%})", field, value, confidence, threshold)

    # Update enrichment metadata if anything was applied
    if applied:
        if hasattr(entity, "last_enriched_at"):
            entity.last_enriched_at = datetime.now(timezone.utc)
        if hasattr(entity, "enrichment_source"):
            entity.enrichment_source = "orchestrator:" + "+".join(sorted(sources_used))
        db.commit()

    return {
        "applied": applied,
        "rejected": rejected,
        "sources_used": sorted(sources_used),
    }


async def enrich_on_demand(
    entity_type: str,
    entity_id: int,
    db: Session,
) -> dict:
    """Top-level orchestrator: load entity, fire sources, merge, apply.

    Args:
        entity_type: "company", "vendor", or "contact"
        entity_id: primary key of the entity
        db: SQLAlchemy session

    Returns:
        {entity_type, entity_id, identifier, sources_fired, merge_results,
         applied, rejected, sources_used}
    """
    # Load entity
    entity = _load_entity(entity_type, entity_id, db)
    if entity is None:
        logger.warning("Entity not found: {} #{}", entity_type, entity_id)
        return {"error": f"{entity_type} #{entity_id} not found"}

    # Get identifier (domain for company/vendor, email for contact)
    identifier = _get_identifier(entity, entity_type)
    if not identifier:
        logger.warning("No identifier available for {} #{}", entity_type, entity_id)
        return {"error": f"No identifier (domain/email) for {entity_type} #{entity_id}"}

    logger.info("Starting on-demand enrichment for {} #{} ({})", entity_type, entity_id, identifier)

    # Fire all sources in parallel
    raw_results = await fire_all_sources(entity_type, identifier)

    # Claude merge
    merged = await claude_merge(raw_results, entity_type)

    # Apply confident data
    result = apply_confident_data(entity, merged, db)

    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "identifier": identifier,
        "sources_fired": list(raw_results.keys()),
        "sources_returned_data": [k for k, v in raw_results.items() if v is not None],
        "merge_results": merged,
        **result,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_entity(entity_type: str, entity_id: int, db: Session):
    """Load entity from DB by type and ID."""
    if entity_type == "company":
        from app.models.crm import Company

        return db.query(Company).filter(Company.id == entity_id).first()
    elif entity_type == "vendor":
        from app.models.vendors import VendorCard

        return db.query(VendorCard).filter(VendorCard.id == entity_id).first()
    else:
        logger.warning("Unsupported entity type for loading: {}", entity_type)
        return None


def _get_identifier(entity, entity_type: str) -> str | None:
    """Extract the best identifier from an entity for enrichment lookups."""
    if entity_type in ("company", "vendor"):
        return getattr(entity, "domain", None) or getattr(entity, "website", None)
    return None
