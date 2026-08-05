"""ai.py — AI Intelligence Layer Router.

AI-powered features: RFQ email parsing, part number normalization,
and part description standardization/generation.

Business Rules:
- AI features gated by settings.ai_features_enabled (off/mike_only/all)
- Response parsing confidence thresholds: 80%+ auto, 50-80% review, <50% raw

Called by: main.py (router mount)
Depends on: services/ai_email_parser.py, services/ai_part_normalizer.py,
services/description_service.py
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel as PydanticBaseModel

from ..config import settings
from ..dependencies import require_user
from ..models import User
from ..schemas.ai import (
    NormalizePartsRequest,
    ParseEmailRequest,
)
from ..schemas.responses import (
    AiNormalizePartsResponse,
    AiParseEmailResponse,
    AiStandardizeResponse,
)

router = APIRouter(tags=["ai"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _ai_enabled(user: User) -> bool:
    """Check if AI features are enabled for this user."""
    flag = settings.ai_features_enabled
    if flag == "off":
        return False
    if flag == "all":
        return True
    if flag == "mike_only":
        allowed = {str(e).strip().lower() for e in (settings.admin_emails or []) if str(e).strip()}
        if not allowed:
            logger.warning("ai_features_enabled='mike_only' but admin_emails is empty — denying access")
            return False
        return (user.email or "").strip().lower() in allowed
    return False


from ..rate_limit import limiter

# ── Feature 2a: Parse RFQ Email (Claude) ──────────────────────────────────


@router.post("/api/ai/parse-email", response_model=AiParseEmailResponse)
@limiter.limit("10/minute")
async def ai_parse_email(
    payload: ParseEmailRequest,
    request: Request,
    user: User = Depends(require_user),
):
    """Parse a vendor email reply into structured quotes using Claude."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")

    from app.services.ai_email_parser import parse_email, should_auto_apply, should_flag_review

    result = await parse_email(
        email_body=payload.email_body,
        email_subject=payload.email_subject,
        vendor_name=payload.vendor_name,
    )

    if not result:
        return {"parsed": False, "quotes": [], "reason": "Parser returned no result"}

    return {
        "parsed": True,
        "quotes": result.get("quotes", []),
        "overall_confidence": result.get("overall_confidence", 0),
        "email_type": result.get("email_type", "unclear"),
        "vendor_notes": result.get("vendor_notes"),
        "auto_apply": should_auto_apply(result),
        "needs_review": should_flag_review(result),
    }


# ── Feature 2c: Part Number Normalization ──────────────────────────────


@router.post("/api/ai/normalize-parts", response_model=AiNormalizePartsResponse)
@limiter.limit("10/minute")
async def ai_normalize_parts(
    payload: NormalizePartsRequest,
    request: Request,
    user: User = Depends(require_user),
):
    """Normalize part numbers using AI — infer manufacturer, package, base part."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")

    from app.services.ai_part_normalizer import normalize_parts

    results = await normalize_parts(payload.parts)
    return {"parts": results, "count": len(results)}


class StandardizeDescriptionRequest(PydanticBaseModel):
    """Standardize a free-text part description into a uniform format."""

    description: str
    mpn: str = ""
    manufacturer: str = ""


@router.post("/api/ai/standardize-description", response_model=AiStandardizeResponse)
@limiter.limit("30/minute")
async def ai_standardize_description(
    payload: StandardizeDescriptionRequest,
    request: Request,
    user: User = Depends(require_user),
):
    """Use AI to clean a part description into Trio Avail standard format.

    Standard format: CATEGORY SUBCATEGORY KEY-SPECS PACKAGE
    Example: IC MCU 32-BIT 168MHZ 1MB FLASH LQFP-100
    """
    if not payload.description.strip():
        return {"description": ""}

    from app.utils.claude_client import claude_text

    prompt = (
        f"Standardize this electronic component description into a short, "
        f"uppercase, distributor-style format.\n\n"
        f"Rules:\n"
        f"- ALL CAPS\n"
        f"- Category first (IC, CONNECTOR, RESISTOR, CAPACITOR, etc.)\n"
        f"- Then subcategory (MCU, OPAMP, USB, MLCC, etc.)\n"
        f"- Then key specs (voltage, current, freq, memory, bits, etc.)\n"
        f"- Then package if known (QFP-100, 0402, SOIC-8, etc.)\n"
        f"- No sentences — just abbreviated spec tokens\n"
        f"- Max ~60 characters\n"
        f"- If the input is too vague, clean it up as best you can\n\n"
        f"MPN: {payload.mpn}\n"
        f"Manufacturer: {payload.manufacturer}\n"
        f"Raw description: {payload.description}\n\n"
        f"Return ONLY the standardized description, nothing else."
    )
    result = await claude_text(prompt, model_tier="fast", max_tokens=100)
    if result:
        result = result.strip().strip('"').strip("'")
    return {"description": result or payload.description.upper()}


class GenerateDescriptionRequest(PydanticBaseModel):
    """Generate a verified part description from distributor cross-referencing."""

    mpn: str
    manufacturer: str = ""
    existing_description: str = ""


@router.post("/api/ai/generate-description")
@limiter.limit("20/minute")
async def ai_generate_description(
    payload: GenerateDescriptionRequest,
    request: Request,
    user: User = Depends(require_user),
):
    """Generate a verified part description using 3-point cross-referencing.

    Queries DigiKey, Mouser, Element14, OEMSecrets, and existing sightings DB, then uses
    AI to synthesize a standardized description from verified data. Returns the
    description plus confidence score and source count.
    """
    if not payload.mpn.strip():
        raise HTTPException(400, "MPN is required")

    from ..services.description_service import generate_verified_description

    result = await generate_verified_description(
        payload.mpn.strip(),
        payload.manufacturer.strip(),
        payload.existing_description.strip(),
    )
    return result
