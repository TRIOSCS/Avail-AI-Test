"""
Email Pipeline — the full closed loop:
  1. SEND:    Draft → capture thread ID → send via Graph API
  2. MONITOR: Poll inbox for replies matching tracked conversations
  3. PARSE:   AI extracts structured quotes from reply text
  4. LEARN:   Create sightings + update vendor reliability scores
"""
import re
import json
import httpx
import structlog
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
from sqlalchemy import select, and_, or_, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Vendor, OutreachLog, VendorResponse, Sighting, User
from app.scoring import normalize_part_number
from app.config import get_settings

logger = structlog.get_logger()
GRAPH = "https://graph.microsoft.com/v1.0"


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SEND RFQs
# ═══════════════════════════════════════════════════════════════════════════════

def generate_rfq_draft(part_numbers: list[str], quantities=None, sender_name="") -> tuple[str, str]:
    """Generate a professional RFQ email."""
    subject = f"RFQ: {', '.join(part_numbers)}"

    lines = []
    for pn in part_numbers:
        qty = quantities.get(pn) if quantities else None
        lines.append(f"  • {pn}  —  Qty: {qty:,}" if qty else f"  • {pn}")

    body = f"""Hi,

We're interested in the following component(s):

{chr(10).join(lines)}

Could you please provide:
  1. Current availability / stock quantity
  2. Unit pricing (USD preferred)
  3. Lead time
  4. Date code (if applicable)
  5. Condition (new/refurb)

Please reply at your earliest convenience.

Best regards,
{sender_name}"""

    return subject, body


async def send_rfq(
    db: AsyncSession, access_token: str, user_id: UUID,
    vendor_ids: list[UUID], part_numbers: list[str],
    subject: str, body: str, bcc_email: str = None,
) -> list[dict]:
    """Send RFQ emails and log outreach with thread tracking."""
    results = []
    normalized_pns = [normalize_part_number(pn) for pn in part_numbers]

    vr = await db.execute(select(Vendor).where(Vendor.id.in_(vendor_ids)))
    vendors = vr.scalars().all()

    for vendor in vendors:
        if not vendor.email:
            results.append({"vendor_id": str(vendor.id), "vendor_name": vendor.name,
                            "status": "skipped", "reason": "No email address"})
            continue

        try:
            msg_id, conv_id = await _create_and_send_draft(
                access_token, vendor.email, subject, body, bcc_email
            )

            now = datetime.now(timezone.utc)
            for pn_norm in normalized_pns:
                db.add(OutreachLog(
                    user_id=user_id, vendor_id=vendor.id,
                    part_number_normalized=pn_norm,
                    email_subject=subject, email_body=body, sent_at=now,
                    graph_message_id=msg_id, graph_conversation_id=conv_id,
                    recipient_email=vendor.email,
                ))

            vendor.total_outreach = (vendor.total_outreach or 0) + 1
            results.append({"vendor_id": str(vendor.id), "vendor_name": vendor.name, "status": "sent"})
            logger.info("rfq_sent", vendor=vendor.name, to=vendor.email)

        except Exception as e:
            logger.error("rfq_failed", vendor=vendor.name, error=str(e))
            results.append({"vendor_id": str(vendor.id), "vendor_name": vendor.name,
                            "status": "failed", "reason": str(e)})

    await db.commit()
    return results


async def _create_and_send_draft(token, to_email, subject, body, bcc=None) -> tuple[str, str]:
    """Create a draft email (captures thread ID), then send it."""
    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }
    if bcc:
        message["bccRecipients"] = [{"emailAddress": {"address": bcc}}]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        # Create draft — gives us the message ID and conversation ID
        r = await client.post(f"{GRAPH}/me/messages", headers=headers, json=message)
        if r.status_code not in (200, 201):
            raise Exception(f"Draft failed: {r.status_code} {r.text[:200]}")

        draft = r.json()
        msg_id = draft.get("id", "")
        conv_id = draft.get("conversationId", "")

        # Send the draft
        r2 = await client.post(f"{GRAPH}/me/messages/{msg_id}/send", headers=headers)
        if r2.status_code not in (200, 202):
            raise Exception(f"Send failed: {r2.status_code} {r2.text[:200]}")

    return msg_id, conv_id


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MONITOR INBOX FOR REPLIES
# ═══════════════════════════════════════════════════════════════════════════════

async def poll_for_replies(db: AsyncSession, access_token: str, user_email: str) -> dict:
    """Check inbox for replies to all pending RFQs. Returns stats."""
    stats = {"checked": 0, "new_replies": 0, "parsed_quotes": 0,
             "sightings_created": 0, "vendors_updated": 0, "errors": 0}

    # Get all unreplied outreach with thread IDs
    r = await db.execute(
        select(OutreachLog).where(
            and_(
                OutreachLog.graph_conversation_id.isnot(None),
                OutreachLog.graph_conversation_id != "",
                or_(OutreachLog.responded.is_(None), OutreachLog.responded == False),
            )
        ).order_by(OutreachLog.sent_at.desc())
    )
    pending = list(r.scalars().all())
    if not pending:
        return stats

    # Group by conversation ID (one RFQ email = one conversation)
    conv_map: dict[str, list[OutreachLog]] = {}
    for log in pending:
        conv_map.setdefault(log.graph_conversation_id, []).append(log)

    # Get already-processed reply IDs to avoid duplicates
    r = await db.execute(
        select(VendorResponse.graph_reply_id).where(VendorResponse.graph_reply_id.isnot(None))
    )
    processed = {row[0] for row in r.fetchall()}

    # Check each conversation for replies
    for conv_id, logs in list(conv_map.items())[:50]:
        stats["checked"] += 1
        try:
            earliest = min(log.sent_at for log in logs)
            replies = await _fetch_replies(access_token, conv_id, earliest)

            for reply in replies:
                reply_id = reply.get("id", "")
                if reply_id in processed:
                    continue

                # Skip messages from ourselves
                from_addr = reply.get("from", {}).get("emailAddress", {}).get("address", "").lower()
                if from_addr == user_email.lower():
                    continue

                stats["new_replies"] += 1

                # Match to outreach logs
                matching = [l for l in logs if l.recipient_email and l.recipient_email.lower() == from_addr]
                if not matching:
                    matching = logs  # fallback: same thread

                # Extract text from email
                body_text = _html_to_text(reply.get("body", {}).get("content", ""),
                                          reply.get("bodyPreview", ""))

                received_at = _parse_datetime(reply.get("receivedDateTime", ""))
                from_name = reply.get("from", {}).get("emailAddress", {}).get("name", "")
                part_numbers = list({l.part_number_normalized for l in matching})

                # AI parse the reply
                try:
                    quotes = await parse_vendor_reply(body_text, part_numbers,
                                                      matching[0].vendor.name if matching else "")
                except Exception as e:
                    logger.error("parse_error", error=str(e))
                    quotes = []

                settings = get_settings()

                # Save each parsed quote
                for q in quotes:
                    stats["parsed_quotes"] += 1
                    vr = VendorResponse(
                        outreach_log_id=matching[0].id, vendor_id=matching[0].vendor_id,
                        graph_reply_id=reply_id, reply_received_at=received_at,
                        reply_from_email=from_addr, reply_from_name=from_name,
                        reply_body_text=body_text[:5000],
                        part_number=q.get("part_number", ""),
                        part_number_normalized=normalize_part_number(q.get("part_number", "")),
                        has_stock=q.get("has_stock"),
                        quoted_price=q.get("price"),
                        quoted_currency=q.get("currency", "USD"),
                        quoted_quantity=q.get("quantity"),
                        quoted_moq=q.get("moq"),
                        quoted_lead_time_days=q.get("lead_time_days"),
                        quoted_lead_time_text=q.get("lead_time_text"),
                        quoted_condition=q.get("condition"),
                        quoted_date_code=q.get("date_code"),
                        quoted_manufacturer=q.get("manufacturer"),
                        parse_confidence=q.get("confidence", 0.0),
                        parse_model=q.get("model", ""),
                        parse_raw=q.get("raw_output"),
                        parse_notes=q.get("notes", ""),
                        status="parsed",
                    )
                    db.add(vr)

                    # Auto-create sighting for high-confidence positive quotes
                    if q.get("confidence", 0) >= settings.auto_sighting_confidence and q.get("has_stock"):
                        s = Sighting(
                            vendor_id=matching[0].vendor_id,
                            part_number=q.get("part_number", ""),
                            part_number_normalized=normalize_part_number(q.get("part_number", "")),
                            manufacturer=q.get("manufacturer"),
                            quantity=q.get("quantity"),
                            price=q.get("price"),
                            currency=q.get("currency", "USD"),
                            lead_time_days=q.get("lead_time_days"),
                            lead_time_text=q.get("lead_time_text"),
                            condition=q.get("condition"),
                            date_code=q.get("date_code"),
                            source_type="email_reply", confidence=5,
                            evidence_type="direct_offer", is_exact_match=True,
                            seen_at=received_at,
                        )
                        db.add(s)
                        await db.flush()
                        vr.sighting_id = s.id
                        vr.status = "sighting_created"
                        stats["sightings_created"] += 1

                # Mark outreach as responded
                for log in matching:
                    if not log.responded:
                        log.responded = True
                        log.responded_at = received_at
                        log.response_hours = round((received_at - log.sent_at).total_seconds() / 3600, 1)
                        log.response_was_positive = any(q.get("has_stock") for q in quotes)

                # Update vendor stats
                await _update_vendor_stats(db, matching[0].vendor_id, received_at, matching[0].sent_at)
                stats["vendors_updated"] += 1
                processed.add(reply_id)

        except Exception as e:
            stats["errors"] += 1
            logger.error("poll_error", conv=conv_id, error=str(e))

    await db.commit()
    logger.info("poll_complete", **stats)
    return stats


async def _fetch_replies(token: str, conv_id: str, since: datetime) -> list[dict]:
    """Get messages in a conversation thread from Graph API."""
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "$filter": f"conversationId eq '{conv_id}' and isDraft eq false and receivedDateTime ge {since_str}",
        "$select": "id,from,receivedDateTime,subject,body,bodyPreview",
        "$orderby": "receivedDateTime asc",
        "$top": "25",
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{GRAPH}/me/messages", headers=headers, params=params)
            if r.status_code != 200:
                logger.warning("graph_error", status=r.status_code)
                return []
            return r.json().get("value", [])
    except Exception as e:
        logger.error("graph_fetch_error", error=str(e))
        return []


async def _update_vendor_stats(db, vendor_id, responded_at, sent_at):
    """Update vendor reliability scores with new response data."""
    r = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = r.scalar_one_or_none()
    if not vendor:
        return

    vendor.total_responses = (vendor.total_responses or 0) + 1
    hours = (responded_at - sent_at).total_seconds() / 3600

    if vendor.avg_response_hours is not None:
        n = vendor.total_responses
        vendor.avg_response_hours = round(((vendor.avg_response_hours * (n - 1)) + hours) / n, 1)
    else:
        vendor.avg_response_hours = round(hours, 1)

    # Auto-flag/unflag slow responders
    flags = vendor.red_flags or []
    if vendor.avg_response_hours and vendor.avg_response_hours > 72:
        if "slow_responder" not in flags:
            vendor.red_flags = flags + ["slow_responder"]
    elif "slow_responder" in flags:
        flags.remove("slow_responder")
        vendor.red_flags = flags


# ═══════════════════════════════════════════════════════════════════════════════
# 3. AI RESPONSE PARSER
# ═══════════════════════════════════════════════════════════════════════════════

EXTRACTION_PROMPT = """You extract structured electronic component quotes from vendor emails.

We asked about these parts: {part_numbers}
Vendor: {vendor_name}

Rules:
- Return a JSON array with one object per part number quoted
- If vendor says NO stock, set has_stock=false
- If email is out-of-office or irrelevant, return []
- Parse lead times: "stock"=0 days, "2 weeks"=14, "45 days ARO"=45
- K=thousands, M=millions
- Set confidence 0.0-1.0 based on clarity of data
- Null for any field not mentioned

Return ONLY a JSON array, no markdown. Example:
[{{"part_number":"LM317T","has_stock":true,"price":0.45,"currency":"USD","quantity":5000,"moq":1000,"lead_time_days":0,"lead_time_text":"stock","condition":"new","date_code":"2024+","manufacturer":"TI","confidence":0.95,"notes":"5K in stock"}}]

--- VENDOR REPLY ---
{email_body}
--- END ---"""


async def parse_vendor_reply(email_body: str, part_numbers: list[str], vendor_name: str = "") -> list[dict]:
    """Use Claude to extract structured quote data from vendor email text."""
    if not email_body or not email_body.strip():
        return []

    settings = get_settings()
    if not settings.anthropic_api_key:
        logger.warning("no_anthropic_key", msg="Cannot parse replies without ANTHROPIC_API_KEY")
        return []

    prompt = EXTRACTION_PROMPT.format(
        part_numbers=", ".join(part_numbers),
        vendor_name=vendor_name or "Unknown",
        email_body=email_body[:4000],
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if r.status_code != 200:
                logger.warning("anthropic_error", status=r.status_code)
                return []

            text = ""
            for block in r.json().get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")

            return _parse_and_enrich(text, part_numbers)

    except Exception as e:
        logger.error("anthropic_error", error=str(e))
        return []


def _parse_and_enrich(text: str, part_numbers: list[str]) -> list[dict]:
    """Parse AI JSON response and normalize types."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            return []
    except json.JSONDecodeError:
        return []

    enriched = []
    for q in result:
        if not isinstance(q, dict):
            continue

        q["model"] = "claude-sonnet-4-20250514"
        q["raw_output"] = q.copy()

        # Normalize types
        for f in ("price", "confidence"):
            if f in q and q[f] is not None:
                try:
                    q[f] = float(q[f])
                except (ValueError, TypeError):
                    q[f] = None
        for f in ("quantity", "moq", "lead_time_days"):
            if f in q and q[f] is not None:
                try:
                    q[f] = int(q[f])
                except (ValueError, TypeError):
                    q[f] = None

        if not q.get("confidence"):
            q["confidence"] = 0.5
        if not q.get("part_number") and len(part_numbers) == 1:
            q["part_number"] = part_numbers[0]

        enriched.append(q)

    return enriched


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _html_to_text(html: str, preview: str) -> str:
    """Strip HTML tags from email body."""
    if not html:
        return preview or ""
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_datetime(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════════
# MONITORING STATS (for the dashboard)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_monitor_stats(db: AsyncSession, user_id) -> dict:
    """Dashboard stats for the Responses tab."""
    r = await db.execute(
        select(
            func.count(OutreachLog.id).label("sent"),
            func.count(case((OutreachLog.responded == True, OutreachLog.id))).label("responded"),
            func.count(case((OutreachLog.response_was_positive == True, OutreachLog.id))).label("positive"),
            func.avg(OutreachLog.response_hours).label("avg_hours"),
        ).where(OutreachLog.user_id == user_id)
    )
    o = r.one()

    r2 = await db.execute(
        select(
            func.count(VendorResponse.id).label("total"),
            func.count(case((VendorResponse.status == "sighting_created", VendorResponse.id))).label("auto"),
            func.count(case((VendorResponse.status == "approved", VendorResponse.id))).label("approved"),
            func.count(case((VendorResponse.status == "rejected", VendorResponse.id))).label("rejected"),
            func.count(case((VendorResponse.status == "parsed", VendorResponse.id))).label("pending"),
            func.avg(VendorResponse.parse_confidence).label("avg_conf"),
        )
    )
    p = r2.one()

    total_sent = o.sent or 0
    total_responded = o.responded or 0

    return {
        "outreach": {
            "total_sent": total_sent,
            "total_responded": total_responded,
            "total_positive": o.positive or 0,
            "response_rate": round((total_responded / total_sent * 100) if total_sent else 0, 1),
            "avg_response_hours": round(o.avg_hours or 0, 1),
        },
        "parsing": {
            "total_parsed": p.total or 0,
            "auto_sightings": p.auto or 0,
            "manual_approved": p.approved or 0,
            "rejected": p.rejected or 0,
            "pending_review": p.pending or 0,
            "avg_confidence": round(p.avg_conf or 0, 2),
        },
    }
