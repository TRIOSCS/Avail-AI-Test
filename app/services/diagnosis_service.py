"""Diagnosis service — two-stage AI classification and root-cause analysis.

Stage 1: Fast classification (category, risk tier, confidence).
Stage 2: Detailed diagnosis for low/medium risk tickets (root cause, files, fix approach).

Called by: routers/trouble_tickets.py
Depends on: utils/claude_client.py, services/file_mapper.py, services/trouble_ticket_service.py
"""

import json
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models.self_heal_log import SelfHealLog
from app.models.trouble_ticket import TroubleTicket
from app.services.file_mapper import get_relevant_files, has_stable_files, STABLE_FILES
from app.services.prompt_generator import generate_fix_prompt
from app.services.trouble_ticket_service import update_ticket
from app.utils.claude_client import claude_structured


CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["ui", "api", "data", "performance", "other"],
        },
        "risk_tier": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0 confidence in the classification",
        },
        "summary": {
            "type": "string",
            "description": "One-sentence summary of the issue",
        },
    },
    "required": ["category", "risk_tier", "confidence", "summary"],
}

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {
            "type": "string",
            "description": "Technical root cause of the issue",
        },
        "affected_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "File paths likely involved in the bug",
        },
        "fix_approach": {
            "type": "string",
            "description": "Step-by-step approach to fix the issue",
        },
        "test_strategy": {
            "type": "string",
            "description": "How to test the fix",
        },
        "estimated_complexity": {
            "type": "string",
            "enum": ["simple", "moderate", "complex"],
        },
        "requires_migration": {
            "type": "boolean",
            "description": "Whether a database migration is needed",
        },
    },
    "required": ["root_cause", "affected_files", "fix_approach", "test_strategy",
                  "estimated_complexity", "requires_migration"],
}

CLASSIFICATION_SYSTEM = """You are a bug triage specialist for AVAIL, a FastAPI + PostgreSQL electronic component sourcing platform.

Classify the reported issue into:
- category: ui (frontend JS/CSS/template), api (endpoint/service logic), data (database/query/model), performance (slow queries/timeouts), other
- risk_tier: low (cosmetic, non-critical), medium (functional but workaround exists), high (data loss, security, core functionality broken)
- confidence: 0.0-1.0 how confident you are in your classification

Be conservative: if uncertain, use a higher risk tier."""

DIAGNOSIS_SYSTEM = """You are a senior developer diagnosing bugs in AVAIL, a FastAPI + PostgreSQL electronic component sourcing platform.

The codebase structure:
- app/routers/ — FastAPI route handlers (thin, delegate to services)
- app/services/ — Business logic
- app/models/ — SQLAlchemy ORM models
- app/static/ — Vanilla JS frontend (app.js, crm.js, tickets.js)
- app/templates/ — Jinja2 templates (index.html)
- tests/ — pytest tests

Provide specific file paths, concrete root cause analysis, and actionable fix steps.
Do NOT suggest modifying these stable files: """ + ", ".join(sorted(STABLE_FILES))


async def classify_ticket(ticket: TroubleTicket) -> dict | None:
    """Stage 1: Fast classification of a trouble ticket.

    Returns: {category, risk_tier, confidence, summary} or None on failure.
    """
    context_str = ""
    if ticket.sanitized_context:
        context_str = f"\n\nAuto-captured context:\n{json.dumps(ticket.sanitized_context, indent=2)}"

    prompt = f"""Classify this bug report:

Title: {ticket.title}
Description: {ticket.description}
Page: {ticket.current_page or 'unknown'}{context_str}"""

    result = await claude_structured(
        prompt=prompt,
        schema=CLASSIFICATION_SCHEMA,
        system=CLASSIFICATION_SYSTEM,
        model_tier="smart",
        max_tokens=512,
    )

    if not result:
        logger.warning("Classification failed for ticket {}", ticket.id)
        return None

    return result


def apply_risk_overrides(
    classification: dict,
    relevant_files: list[dict],
) -> dict:
    """Apply risk tier overrides based on business rules.

    Rules:
    - confidence < 0.6 -> bump tier up
    - Any file in STABLE_FILES -> force high
    - requires_migration -> force high (checked later in diagnosis)
    - complex + low -> bump to medium
    """
    tier = classification.get("risk_tier", "medium")
    confidence = classification.get("confidence", 0.5)

    # Low confidence -> bump up
    if confidence < 0.6:
        if tier == "low":
            tier = "medium"
        elif tier == "medium":
            tier = "high"
        logger.info("Risk bumped due to low confidence ({:.2f}): {}", confidence, tier)

    # Stable files -> force high
    if has_stable_files(relevant_files):
        tier = "high"
        logger.info("Risk forced to high: stable files affected")

    classification["risk_tier"] = tier
    return classification


async def diagnose_ticket(ticket: TroubleTicket, classification: dict) -> dict | None:
    """Stage 2: Detailed diagnosis for low/medium risk tickets.

    Returns: {root_cause, affected_files, fix_approach, test_strategy,
              estimated_complexity, requires_migration} or None.
    """
    # Get relevant files from file mapper
    relevant_files = get_relevant_files(
        route_pattern=ticket.current_page,
        error_context=ticket.description,
    )
    file_context = ""
    if relevant_files:
        file_list = "\n".join(
            f"  - {f['path']} ({f['role']}, confidence: {f['confidence']})"
            for f in relevant_files
        )
        file_context = f"\n\nRelevant files identified:\n{file_list}"

    context_str = ""
    if ticket.sanitized_context:
        context_str = f"\n\nAuto-captured context:\n{json.dumps(ticket.sanitized_context, indent=2)}"

    prompt = f"""Diagnose this bug:

Title: {ticket.title}
Description: {ticket.description}
Category: {classification.get('category', 'unknown')}
Risk Tier: {classification.get('risk_tier', 'unknown')}
Classification Summary: {classification.get('summary', '')}{file_context}{context_str}

Provide a detailed technical diagnosis with specific file paths and fix approach."""

    result = await claude_structured(
        prompt=prompt,
        schema=DIAGNOSIS_SCHEMA,
        system=DIAGNOSIS_SYSTEM,
        model_tier="smart",
        max_tokens=2048,
    )

    if not result:
        logger.warning("Detailed diagnosis failed for ticket {}", ticket.id)
        return None

    return result


async def diagnose_full(ticket_id: int, db: Session) -> dict:
    """Full diagnosis pipeline: classify -> override -> diagnose -> persist.

    Returns: {classification, diagnosis, risk_tier, status} or {error}.
    """
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    # Stage 1: Classify
    classification = await classify_ticket(ticket)
    if not classification:
        return {"error": "Classification failed"}

    # Get relevant files for risk override check
    relevant_files = get_relevant_files(
        route_pattern=ticket.current_page,
        error_context=ticket.description,
    )

    # Apply risk overrides
    classification = apply_risk_overrides(classification, relevant_files)
    risk_tier = classification["risk_tier"]

    # Stage 2: Detailed diagnosis (skip for high risk — needs human review)
    diagnosis = None
    if risk_tier != "high":
        diagnosis = await diagnose_ticket(ticket, classification)
        # Check if diagnosis indicates migration needed -> force high
        if diagnosis and diagnosis.get("requires_migration"):
            risk_tier = "high"
            classification["risk_tier"] = "high"
            logger.info("Risk forced to high: migration required for ticket {}", ticket_id)
        # Check complexity override: complex + low -> medium
        if diagnosis and risk_tier == "low" and diagnosis.get("estimated_complexity") == "complex":
            risk_tier = "medium"
            classification["risk_tier"] = "medium"
            logger.info("Risk bumped to medium: complex issue for ticket {}", ticket_id)

    # Build combined diagnosis result
    full_diagnosis = {
        "classification": classification,
        "detailed": diagnosis,
        "relevant_files": [f["path"] for f in relevant_files],
    }

    # Generate fix prompt
    generated_prompt = None
    if diagnosis and risk_tier != "high":
        generated_prompt = generate_fix_prompt(
            ticket_id=ticket_id,
            title=ticket.title,
            description=ticket.description or "",
            category=classification.get("category", "other"),
            diagnosis=diagnosis,
            relevant_files=relevant_files,
        )

    # Update ticket
    update_kwargs = {
        "status": "diagnosed",
        "risk_tier": risk_tier,
        "category": classification.get("category"),
        "diagnosis": full_diagnosis,
    }
    if diagnosis:
        update_kwargs["file_mapping"] = diagnosis.get("affected_files", [])
    if generated_prompt:
        update_kwargs["generated_prompt"] = generated_prompt

    update_ticket(db, ticket_id, **update_kwargs)

    # Log to SelfHealLog
    log_entry = SelfHealLog(
        ticket_id=ticket_id,
        category=classification.get("category"),
        risk_tier=risk_tier,
    )
    db.add(log_entry)
    db.commit()

    logger.info(
        "Ticket {} diagnosed: category={}, risk={}",
        ticket_id, classification.get("category"), risk_tier,
    )

    return {
        "classification": classification,
        "diagnosis": diagnosis,
        "risk_tier": risk_tier,
        "status": "diagnosed",
    }
