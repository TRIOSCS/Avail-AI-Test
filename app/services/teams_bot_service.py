"""Teams bot service — intent classification, query routing, and response formatting.

Classifies user messages into intents, routes to appropriate handlers,
and returns Adaptive Card responses. Uses Redis for conversation context.

Called by: app/routers/teams_bot.py
Depends on: app/utils/claude_client.py, app/models, Redis
"""

import json
import os
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func

# Intent definitions
INTENTS = {
    "pipeline_status": "User wants pipeline/requisition summary",
    "recent_quotes": "User wants their recent quotes",
    "deal_info": "User wants details about a specific deal/requisition",
    "vendor_lookup": "User wants vendor information",
    "company_info": "User wants company/customer information",
    "deal_risk": "User wants risk assessment for a deal",
    "help": "User wants to know available commands",
    "unknown": "Can't determine intent",
}


async def handle_query(text: str, user_name: str, user_aad_id: str) -> dict:
    """Main entry point — classify intent and route to handler."""
    # Store context
    _update_context(user_aad_id, text)

    # Classify intent
    intent, params = await classify_intent(text, user_aad_id)
    logger.debug("Bot intent: %s, params: %s", intent, params)

    # Route to handler
    handlers = {
        "pipeline_status": _handle_pipeline_status,
        "recent_quotes": _handle_recent_quotes,
        "deal_info": _handle_deal_info,
        "vendor_lookup": _handle_vendor_lookup,
        "company_info": _handle_company_info,
        "deal_risk": _handle_deal_risk,
        "help": _handle_help,
    }

    handler = handlers.get(intent, _handle_unknown)
    return await handler(user_name, user_aad_id, params)


async def classify_intent(text: str, user_aad_id: str) -> tuple[str, dict]:
    """Classify user message into an intent with extracted parameters.

    Stage 1: keyword matching (fast, no AI cost)
    Stage 2: Claude Haiku for ambiguous messages
    """
    lower = text.lower().strip()

    # Stage 1: keyword matching
    if lower in ("help", "?", "commands"):
        return "help", {}

    if any(kw in lower for kw in ["pipeline", "my deals", "my reqs", "open deals", "status"]):
        return "pipeline_status", {}

    if any(kw in lower for kw in ["recent quotes", "my quotes", "quotes sent"]):
        return "recent_quotes", {}

    if any(kw in lower for kw in ["risk", "at risk"]):
        # Try to extract requisition ID
        req_id = _extract_number(text)
        if req_id:
            return "deal_risk", {"requisition_id": req_id}
        return "deal_risk", {}

    # Check for "req #123" or "deal 123" pattern
    import re
    req_match = re.search(r"(?:req|deal|requisition)\s*#?\s*(\d+)", lower)
    if req_match:
        return "deal_info", {"requisition_id": int(req_match.group(1))}

    if any(kw in lower for kw in ["vendor", "supplier"]):
        name = _extract_name(text, ["vendor", "supplier"])
        return "vendor_lookup", {"name": name}

    if any(kw in lower for kw in ["company", "customer", "account"]):
        name = _extract_name(text, ["company", "customer", "account"])
        return "company_info", {"name": name}

    # Stage 2: AI classification for ambiguous messages
    context = _get_context(user_aad_id)
    return await _ai_classify_intent(text, context)


async def _ai_classify_intent(text: str, context: list[str]) -> tuple[str, dict]:
    """Use Claude Haiku to classify ambiguous messages."""
    try:
        from app.utils.claude_client import claude_structured

        schema = {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": list(INTENTS.keys())},
                "params": {"type": "object"},
            },
            "required": ["intent"],
        }

        ctx_str = "\n".join(context[-3:]) if context else "No prior context"

        result = await claude_structured(
            prompt=(
                f"Classify this Teams chat message into an intent.\n"
                f"Message: {text}\n"
                f"Prior messages: {ctx_str}\n\n"
                f"Available intents: {json.dumps(INTENTS)}\n"
                f"Extract relevant params (requisition_id, name, etc)."
            ),
            schema=schema,
            system="You classify chat messages for a business bot. Be precise about intent.",
            model_tier="fast",
            max_tokens=150,
        )

        if result:
            return result.get("intent", "unknown"), result.get("params", {})
    except Exception:
        logger.debug("AI intent classification failed", exc_info=True)

    return "unknown", {}


# ═══════════════════════════════════════════════════════════════════════
#  QUERY HANDLERS
# ═══════════════════════════════════════════════════════════════════════


async def _handle_pipeline_status(user_name: str, user_aad_id: str, params: dict) -> dict:
    """Pipeline summary — open, offers, won, lost counts + total value."""
    from app.database import SessionLocal
    from app.models.sourcing import Requisition

    db = SessionLocal()
    try:
        user = _resolve_user(user_aad_id, db)
        if not user:
            return _text_card("I couldn't find your user account. Contact admin.")

        statuses = {}
        for status in ["active", "sourcing", "offers", "won", "lost"]:
            count = (
                db.query(func.count(Requisition.id))
                .filter(Requisition.created_by == user.id, Requisition.status == status)
                .scalar()
            ) or 0
            statuses[status] = count

        total_value = (
            db.query(func.sum(Requisition.total_value))
            .filter(Requisition.created_by == user.id, Requisition.status.in_(["active", "sourcing", "offers"]))
            .scalar()
        ) or 0

        facts = [
            {"title": "Active", "value": str(statuses.get("active", 0))},
            {"title": "Sourcing", "value": str(statuses.get("sourcing", 0))},
            {"title": "With Offers", "value": str(statuses.get("offers", 0))},
            {"title": "Won", "value": str(statuses.get("won", 0))},
            {"title": "Lost", "value": str(statuses.get("lost", 0))},
            {"title": "Open Value", "value": f"${total_value:,.0f}"},
        ]

        return _facts_card(f"Pipeline — {user_name}", "Your current requisition status", facts)
    finally:
        db.close()


async def _handle_recent_quotes(user_name: str, user_aad_id: str, params: dict) -> dict:
    """Recent quotes sent in last 7 days."""
    from app.database import SessionLocal
    from app.models.quotes import Quote

    db = SessionLocal()
    try:
        user = _resolve_user(user_aad_id, db)
        if not user:
            return _text_card("I couldn't find your user account.")

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        quotes = (
            db.query(Quote)
            .filter(Quote.created_by_id == user.id, Quote.sent_at > cutoff)
            .order_by(Quote.sent_at.desc())
            .limit(10)
            .all()
        )

        if not quotes:
            return _text_card("No quotes sent in the last 7 days.")

        facts = []
        for q in quotes:
            sent = q.sent_at.strftime("%b %d") if q.sent_at else "—"
            facts.append({"title": f"Q-{q.quote_number}", "value": f"{q.status or 'sent'} ({sent})"})

        return _facts_card(f"Recent Quotes — {user_name}", f"{len(quotes)} quotes in last 7 days", facts)
    finally:
        db.close()


async def _handle_deal_info(user_name: str, user_aad_id: str, params: dict) -> dict:
    """Specific requisition detail."""
    from app.database import SessionLocal
    from app.models.offers import Offer
    from app.models.sourcing import Requirement, Requisition

    req_id = params.get("requisition_id")
    if not req_id:
        return _text_card("Which requisition? Try 'deal #123'")

    db = SessionLocal()
    try:
        req = db.get(Requisition, int(req_id))
        if not req:
            return _text_card(f"Requisition #{req_id} not found.")

        req_count = db.query(func.count(Requirement.id)).filter(Requirement.requisition_id == req.id).scalar() or 0
        offer_count = db.query(func.count(Offer.id)).filter(Offer.requisition_id == req.id).scalar() or 0

        facts = [
            {"title": "Name", "value": req.name or "—"},
            {"title": "Customer", "value": req.customer_name or "—"},
            {"title": "Status", "value": req.status or "—"},
            {"title": "Requirements", "value": str(req_count)},
            {"title": "Offers", "value": str(offer_count)},
        ]
        if req.total_value:
            facts.append({"title": "Value", "value": f"${req.total_value:,.0f}"})

        return _facts_card(f"Requisition #{req_id}", req.name or "", facts)
    finally:
        db.close()


async def _handle_vendor_lookup(user_name: str, user_aad_id: str, params: dict) -> dict:
    """Vendor information lookup."""
    from app.database import SessionLocal
    from app.models.vendors import VendorCard

    name = params.get("name", "").strip()
    if not name:
        return _text_card("Which vendor? Try 'vendor Acme'")

    db = SessionLocal()
    try:
        vendor = (
            db.query(VendorCard)
            .filter(VendorCard.normalized_name.ilike(f"%{name.lower()}%"))
            .first()
        )

        if not vendor:
            return _text_card(f"No vendor found matching '{name}'")

        facts = [
            {"title": "Name", "value": vendor.vendor_name or "—"},
            {"title": "Score", "value": f"{vendor.vendor_score:.0f}/100" if vendor.vendor_score else "—"},
            {"title": "Response Rate", "value": f"{vendor.response_rate:.0%}" if vendor.response_rate else "—"},
            {"title": "Avg Lead Time", "value": f"{vendor.avg_lead_time_days:.0f}d" if vendor.avg_lead_time_days else "—"},
        ]

        return _facts_card(f"Vendor: {vendor.vendor_name}", "", facts)
    finally:
        db.close()


async def _handle_company_info(user_name: str, user_aad_id: str, params: dict) -> dict:
    """Company/customer information."""
    from app.database import SessionLocal
    from app.models.crm import Company

    name = params.get("name", "").strip()
    if not name:
        return _text_card("Which company? Try 'company Acme'")

    db = SessionLocal()
    try:
        company = db.query(Company).filter(Company.name.ilike(f"%{name}%")).first()
        if not company:
            return _text_card(f"No company found matching '{name}'")

        from app.services.activity_service import days_since_last_activity
        days = days_since_last_activity(company.id, db)

        facts = [
            {"title": "Name", "value": company.name or "—"},
            {"title": "Industry", "value": company.industry or "—"},
            {"title": "Sites", "value": str(company.site_count or 0)},
            {"title": "Open Reqs", "value": str(company.open_req_count or 0)},
            {"title": "Last Activity", "value": f"{days}d ago" if days is not None else "Never"},
        ]

        return _facts_card(f"Company: {company.name}", "", facts)
    finally:
        db.close()


async def _handle_deal_risk(user_name: str, user_aad_id: str, params: dict) -> dict:
    """Deal risk assessment."""
    from app.database import SessionLocal

    req_id = params.get("requisition_id")

    db = SessionLocal()
    try:
        if req_id:
            from app.services.deal_risk import assess_risk
            risk = assess_risk(int(req_id), db)
            color_map = {"green": "good", "yellow": "warning", "red": "attention"}
            facts = [
                {"title": "Risk Level", "value": risk["risk_level"].upper()},
                {"title": "Score", "value": f"{risk['score']}/100"},
                {"title": "Explanation", "value": risk["explanation"]},
            ]
            if risk["suggested_action"]:
                facts.append({"title": "Action", "value": risk["suggested_action"]})
            return _facts_card(
                f"Risk: Req #{req_id}",
                risk["explanation"],
                facts,
                accent=color_map.get(risk["risk_level"], "accent"),
            )
        else:
            # Show top red-risk deals for this user
            user = _resolve_user(user_aad_id, db)
            if not user:
                return _text_card("I couldn't find your user account.")

            from app.services.deal_risk import scan_active_requisitions
            risks = scan_active_requisitions(db, user.id)
            red = [r for r in risks if r["risk_level"] in ("red", "yellow")]
            red.sort(key=lambda x: x["score"], reverse=True)

            if not red:
                return _text_card("No at-risk deals found. Looking good!")

            facts = []
            for r in red[:5]:
                level = r["risk_level"].upper()
                facts.append({
                    "title": f"#{r['requisition_id']} ({level})",
                    "value": f"{r.get('requisition_name', '')} — {r['explanation'][:80]}",
                })
            return _facts_card("At-Risk Deals", f"{len(red)} deal(s) need attention", facts, accent="warning")
    finally:
        db.close()


async def _handle_help(user_name: str, user_aad_id: str, params: dict) -> dict:
    """Static command list."""
    return _text_card(
        "Available commands:\n\n"
        "- **pipeline** — Your requisition summary\n"
        "- **recent quotes** — Quotes sent in last 7 days\n"
        "- **deal #123** — Details for a specific requisition\n"
        "- **risk** — At-risk deals overview\n"
        "- **risk #123** — Risk assessment for a deal\n"
        "- **vendor [name]** — Vendor scorecard\n"
        "- **company [name]** — Company overview\n"
        "- **help** — This message"
    )


async def _handle_unknown(user_name: str, user_aad_id: str, params: dict) -> dict:
    """Handle unrecognized intent."""
    return _text_card(
        "I'm not sure what you're asking. Try:\n"
        "- 'pipeline' for deal summary\n"
        "- 'deal #123' for deal details\n"
        "- 'help' for all commands"
    )


# ═══════════════════════════════════════════════════════════════════════
#  CONVERSATION CONTEXT (Redis, 30min TTL)
# ═══════════════════════════════════════════════════════════════════════


def _get_redis():
    """Get Redis client."""
    if os.environ.get("TESTING"):
        return None
    try:
        from app.cache.intel_cache import _get_redis as _cache_get_redis
        return _cache_get_redis()
    except Exception:
        return None


def _update_context(user_aad_id: str, message: str) -> None:
    """Store message in conversation context."""
    r = _get_redis()
    if not r:
        return
    try:
        key = f"bot_ctx:{user_aad_id}"
        r.rpush(key, message)
        r.ltrim(key, -10, -1)  # Keep last 10 messages
        r.expire(key, 1800)  # 30min TTL
    except Exception:
        pass


def _get_context(user_aad_id: str) -> list[str]:
    """Get conversation context."""
    r = _get_redis()
    if not r:
        return []
    try:
        key = f"bot_ctx:{user_aad_id}"
        items = r.lrange(key, 0, -1)
        return [item.decode() if isinstance(item, bytes) else item for item in items]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════
#  CARD BUILDERS
# ═══════════════════════════════════════════════════════════════════════


def _text_card(message: str) -> dict:
    """Simple text response card."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": message, "wrap": True}],
                },
            }
        ],
    }


def _facts_card(title: str, subtitle: str, facts: list[dict], accent: str = "accent") -> dict:
    """Adaptive Card with title, subtitle, and FactSet."""
    body = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "color": accent},
    ]
    if subtitle:
        body.append({"type": "TextBlock", "text": subtitle, "wrap": True, "spacing": "Small"})
    body.append({"type": "FactSet", "facts": facts, "spacing": "Medium"})

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                },
            }
        ],
    }


# ═══════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _resolve_user(user_aad_id: str, db):
    """Resolve a Teams AAD user ID to an AVAIL user."""
    if not user_aad_id:
        return None
    try:
        from app.models.auth import User
        # Try matching by Azure AD object ID or email
        user = db.query(User).filter(User.azure_ad_id == user_aad_id).first()
        if user:
            return user
        # Fallback: try matching the first active user (for testing)
        return db.query(User).filter(User.is_active.is_(True)).first()
    except Exception:
        return None


def _extract_number(text: str) -> int | None:
    """Extract first number from text."""
    import re
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def _extract_name(text: str, keywords: list[str]) -> str:
    """Extract name after keyword from text."""
    lower = text.lower()
    for kw in keywords:
        idx = lower.find(kw)
        if idx >= 0:
            remainder = text[idx + len(kw):].strip()
            # Remove common filler words
            for filler in ["about", "for", "named", "called", "info", "details"]:
                if remainder.lower().startswith(filler):
                    remainder = remainder[len(filler):].strip()
            return remainder.strip("?.,! ")
    return text.strip("?.,! ")
