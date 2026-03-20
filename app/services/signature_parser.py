"""Signature parser — extracts contact info from email signatures.

Uses regex for fast extraction, falls back to Claude AI for complex signatures. Caches
results in EmailSignatureExtract table to avoid re-parsing.
"""

import re
from datetime import datetime, timezone

from loguru import logger

# ── Regex patterns for signature extraction ──────────────────────────

_PHONE_RE = re.compile(
    r"(?:(?:phone|tel|ph|office|direct|main|fax|cell|mobile|m)\s*[:.#]?\s*)"
    r"([\+]?[\d\s\-\.\(\)]{7,20})",
    re.IGNORECASE,
)

_BARE_PHONE_RE = re.compile(r"(?<!\d)(\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})(?!\d)")

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_WEBSITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?([a-zA-Z0-9][\w\-]*\.[a-zA-Z]{2,}(?:\.[a-zA-Z]{2,})?)",
    re.IGNORECASE,
)

_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-]+",
    re.IGNORECASE,
)

_TITLE_KEYWORDS = {
    "president",
    "ceo",
    "cfo",
    "coo",
    "cto",
    "vp",
    "vice president",
    "director",
    "manager",
    "supervisor",
    "lead",
    "head",
    "chief",
    "engineer",
    "analyst",
    "specialist",
    "coordinator",
    "associate",
    "sales",
    "buyer",
    "purchasing",
    "procurement",
    "sourcing",
    "logistics",
    "supply chain",
    "account",
    "representative",
    "admin",
    "assistant",
    "executive",
    "partner",
    "founder",
    "owner",
}

_SIGNATURE_DELIMITERS = [
    re.compile(r"^[\-\—\_]{2,}", re.MULTILINE),
    re.compile(r"^thanks,?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^regards,?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^best,?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^sincerely,?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^cheers,?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^warm regards,?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^kind regards,?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^best regards,?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^sent from my", re.IGNORECASE | re.MULTILINE),
]


def _extract_signature_block(body: str) -> str:
    """Extract the signature portion of an email body (last ~15 lines after
    delimiter)."""
    if not body:
        return ""

    lines = body.strip().split("\n")

    # Find signature delimiter
    sig_start = None
    for i, line in enumerate(lines):
        for pattern in _SIGNATURE_DELIMITERS:
            if pattern.search(line.strip()):
                sig_start = i
                break
        if sig_start is not None:
            break

    if sig_start is not None:
        sig_lines = lines[sig_start:]
    else:
        # No delimiter found — use last 15 lines
        sig_lines = lines[-15:]

    return "\n".join(sig_lines)


def parse_signature_regex(body: str) -> dict:
    """Fast regex extraction of contact data from email signature block.

    Returns: {
        full_name, title, company_name, phone, mobile, email,
        website, linkedin_url, address, confidence
    }
    """
    sig_block = _extract_signature_block(body)
    if not sig_block:
        return {"confidence": 0.0}

    result = {
        "full_name": None,
        "title": None,
        "company_name": None,
        "phone": None,
        "mobile": None,
        "email": None,
        "website": None,
        "linkedin_url": None,
        "address": None,
        "confidence": 0.0,
    }

    lines = [line.strip() for line in sig_block.split("\n") if line.strip()]

    # Extract phone numbers
    phones = []
    for line in lines:
        for m in _PHONE_RE.finditer(line):
            phone = re.sub(r"[^\d\+\-\.\(\)\s]", "", m.group(1)).strip()
            if len(re.sub(r"\D", "", phone)) >= 7:
                label = line[: m.start()].lower().strip()
                if "mobile" in label or "cell" in label:
                    result["mobile"] = phone
                else:
                    phones.append(phone)

    if not phones:
        for line in lines:
            for m in _BARE_PHONE_RE.finditer(line):
                phones.append(m.group(1))

    if phones:
        result["phone"] = phones[0]

    # Extract email
    for line in lines:
        m = _EMAIL_RE.search(line)
        if m:
            result["email"] = m.group(0)
            break

    # Extract LinkedIn
    for line in lines:
        m = _LINKEDIN_RE.search(line)
        if m:
            url = m.group(0)
            if not url.startswith("http"):
                url = "https://" + url
            result["linkedin_url"] = url
            break

    # Extract website (skip linkedin)
    for line in lines:
        m = _WEBSITE_RE.search(line)
        if m and "linkedin.com" not in m.group(0).lower():
            domain = m.group(1)
            result["website"] = domain
            break

    # Extract name and title from first few lines
    # Name is usually the first non-empty line that's not a phone/email/url
    for line in lines[:5]:
        clean = line.strip().rstrip("|").strip()
        if not clean or len(clean) < 2:
            continue
        if _EMAIL_RE.search(clean) or _PHONE_RE.search(clean) or _BARE_PHONE_RE.search(clean):
            continue
        if _LINKEDIN_RE.search(clean) or clean.startswith("http"):
            continue
        if any(clean.lower().startswith(d) for d in ["sent from", "---", "___"]):
            continue

        # Check if this looks like a name (2-4 words, proper case)
        words = clean.split()
        if 1 <= len(words) <= 5 and all(w[0].isupper() or w in ("de", "van", "von", "di") for w in words if w):
            if not result["full_name"]:
                result["full_name"] = clean
                continue

        # Check if this looks like a title
        if any(kw in clean.lower() for kw in _TITLE_KEYWORDS):
            if not result["title"]:
                result["title"] = clean
                continue

        # Could be company name (after name and title)
        if result["full_name"] and not result["company_name"]:
            result["company_name"] = clean

    # Calculate confidence
    fields_found = sum(1 for k, v in result.items() if v and k != "confidence")
    result["confidence"] = min(0.3 + (fields_found * 0.1), 0.9)

    return result


async def parse_signature_ai(body: str, sender_name: str = "", sender_email: str = "") -> dict:
    """Use Claude to extract structured signature data from complex email signatures.

    Returns same shape as parse_signature_regex.
    """
    from ..utils.claude_client import claude_json

    sig_block = _extract_signature_block(body)
    if not sig_block:
        return {"confidence": 0.0}

    # Limit to prevent excessive token usage
    sig_text = sig_block[:2000]

    prompt = (
        f"Extract contact information from this email signature.\n"
        f"Sender name: {sender_name or 'unknown'}\n"
        f"Sender email: {sender_email or 'unknown'}\n\n"
        f"Signature block:\n```\n{sig_text}\n```\n\n"
        f"Return a JSON object with these keys (use null for unknown):\n"
        f'{{"full_name", "title", "company_name", "phone", "mobile", '
        f'"website", "address", "linkedin_url"}}'
    )

    try:
        data = await claude_json(
            prompt,
            system="You extract contact information from email signatures. Return ONLY valid JSON.",
            model_tier="fast",
            max_tokens=512,
            timeout=10,
        )
        if not data or not isinstance(data, dict):
            return {"confidence": 0.0}

        result = {
            "full_name": data.get("full_name"),
            "title": data.get("title"),
            "company_name": data.get("company_name"),
            "phone": data.get("phone"),
            "mobile": data.get("mobile"),
            "email": sender_email or None,
            "website": data.get("website"),
            "linkedin_url": data.get("linkedin_url"),
            "address": data.get("address"),
        }

        fields_found = sum(1 for v in result.values() if v)
        result["confidence"] = min(0.5 + (fields_found * 0.08), 0.95)
        return result
    except Exception as e:
        logger.warning("AI signature parsing failed: %s", e)
        return {"confidence": 0.0}


async def parse_signature_gradient(body: str, sender_name: str = "", sender_email: str = "") -> dict:
    """Use Gradient (Sonnet tier) for fast, cheap signature extraction.

    Primary AI path — cheaper than Claude direct. Falls back gracefully.
    """
    from .gradient_service import gradient_json

    sig_block = _extract_signature_block(body)
    if not sig_block:
        return {"confidence": 0.0}

    sig_text = sig_block[:2000]

    prompt = (
        f"Extract contact information from this email signature.\n"
        f"Sender name: {sender_name or 'unknown'}\n"
        f"Sender email: {sender_email or 'unknown'}\n\n"
        f"Signature block:\n```\n{sig_text}\n```\n\n"
        f"Return a JSON object with these keys (use null for unknown):\n"
        f'{{"full_name", "title", "company_name", "phone", "mobile", '
        f'"website", "address", "linkedin_url"}}'
    )

    try:
        data = await gradient_json(
            prompt,
            system="You extract contact information from email signatures. Return ONLY valid JSON.",
            model_tier="default",
            max_tokens=512,
            timeout=10,
        )
        if not data or not isinstance(data, dict):
            return {"confidence": 0.0}

        result = {
            "full_name": data.get("full_name"),
            "title": data.get("title"),
            "company_name": data.get("company_name"),
            "phone": data.get("phone"),
            "mobile": data.get("mobile"),
            "email": sender_email or None,
            "website": data.get("website"),
            "linkedin_url": data.get("linkedin_url"),
            "address": data.get("address"),
        }

        fields_found = sum(1 for v in result.values() if v)
        result["confidence"] = min(0.5 + (fields_found * 0.08), 0.95)
        return result
    except Exception as e:
        logger.warning("Gradient signature parsing failed: %s", e)
        return {"confidence": 0.0}


async def extract_signature(body: str, sender_name: str = "", sender_email: str = "") -> dict:
    """Try regex first, fall back to Gradient then Claude if confidence < 0.7."""
    regex_result = parse_signature_regex(body)

    if regex_result.get("confidence", 0) >= 0.7:
        regex_result["extraction_method"] = "regex"
        return regex_result

    # Try Gradient (primary AI path — fast + cheap)
    try:
        from ..config import settings

        if settings.do_gradient_api_key:
            gradient_result = await parse_signature_gradient(body, sender_name, sender_email)
            if gradient_result.get("confidence", 0) > regex_result.get("confidence", 0):
                gradient_result["extraction_method"] = "gradient_ai"
                return gradient_result
    except Exception as e:
        logger.debug("Gradient signature parse failed: %s", e)

    # Fallback to Claude (secondary AI path)
    try:
        ai_result = await parse_signature_ai(body, sender_name, sender_email)
        if ai_result.get("confidence", 0) > regex_result.get("confidence", 0):
            ai_result["extraction_method"] = "claude_ai"
            return ai_result
    except Exception as e:
        logger.debug("Claude signature parse failed: %s", e)

    regex_result["extraction_method"] = "regex"
    return regex_result


def cache_signature_extract(db, sender_email: str, extract: dict) -> None:
    """Upsert an EmailSignatureExtract record."""
    from ..models import EmailSignatureExtract

    existing = (
        db.query(EmailSignatureExtract).filter(EmailSignatureExtract.sender_email == sender_email.lower()).first()
    )

    if existing:
        existing.seen_count = (existing.seen_count or 0) + 1
        existing.updated_at = datetime.now(timezone.utc)
        # Only overwrite if new extract has higher confidence
        if extract.get("confidence", 0) > (existing.confidence or 0):
            for field in (
                "full_name",
                "title",
                "company_name",
                "phone",
                "mobile",
                "website",
                "address",
                "linkedin_url",
            ):
                val = extract.get(field)
                if val:
                    setattr(existing, field, val)
            existing.confidence = extract.get("confidence", 0)
            existing.extraction_method = extract.get("extraction_method", "regex")
    else:
        record = EmailSignatureExtract(
            sender_email=sender_email.lower(),
            sender_name=extract.get("full_name"),
            full_name=extract.get("full_name"),
            title=extract.get("title"),
            company_name=extract.get("company_name"),
            phone=extract.get("phone"),
            mobile=extract.get("mobile"),
            website=extract.get("website"),
            address=extract.get("address"),
            linkedin_url=extract.get("linkedin_url"),
            extraction_method=extract.get("extraction_method", "regex"),
            confidence=extract.get("confidence", 0),
        )
        db.add(record)

    try:
        db.flush()
    except Exception as e:
        logger.debug("Signature cache flush error: %s", e)
        db.rollback()


# ── Batch API signature re-parsing ───────────────────────────────────

from ..cache.intel_cache import _get_redis  # noqa: E402
from ..services.batch_queue import BatchQueue  # noqa: E402
from ..utils.claude_client import claude_batch_results, claude_batch_submit  # noqa: E402

_REDIS_KEY = "batch:signature_parse:current"
_BATCH_LIMIT = 200

_SIGNATURE_SCHEMA = {
    "type": "object",
    "properties": {
        "full_name": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "company_name": {"type": ["string", "null"]},
        "phone": {"type": ["string", "null"]},
        "mobile": {"type": ["string", "null"]},
        "website": {"type": ["string", "null"]},
        "address": {"type": ["string", "null"]},
        "linkedin_url": {"type": ["string", "null"]},
    },
    "required": ["full_name", "title", "company_name", "phone", "mobile", "website", "address", "linkedin_url"],
}

_BATCH_SYSTEM_PROMPT = "You extract contact information from email signatures. Return ONLY valid JSON."


def _build_signature_prompt(record) -> str:
    """Build a validation/improvement prompt from existing extracted fields.

    Since EmailSignatureExtract does not store the raw email body, we ask Claude to
    validate and improve the partial fields already extracted by regex.
    """
    return (
        f"Given this partial contact information extracted from an email signature:\n"
        f"Name: {record.full_name or 'unknown'}\n"
        f"Email: {record.sender_email}\n"
        f"Title: {record.title or 'unknown'}\n"
        f"Company: {record.company_name or 'unknown'}\n"
        f"Phone: {record.phone or 'unknown'}\n\n"
        f"Please validate these fields and provide your best assessment. "
        f"Return a JSON object with the corrected/validated fields."
    )


async def batch_parse_signatures(db) -> str | None:
    """Submit low-confidence regex-parsed signatures to Claude Batch API.

    Queries EmailSignatureExtract records where extraction_method = 'regex' AND
    confidence < 0.7. Uses existing extracted fields as context for AI validation (raw
    email body is not stored on the extract). Submits up to 100 records.

    Returns the batch_id or None if no records to process or submit failed.
    """
    from ..models import EmailSignatureExtract  # noqa: F811

    # ── Inflight guard ──────────────────────────────────────────────
    r = _get_redis()
    if r and r.get(_REDIS_KEY):
        logger.info("batch_parse_signatures: batch already pending, skipping submit")
        return None

    records = (
        db.query(EmailSignatureExtract)
        .filter(
            EmailSignatureExtract.extraction_method == "regex",
            EmailSignatureExtract.confidence < 0.7,
        )
        .limit(_BATCH_LIMIT)
        .all()
    )

    if not records:
        logger.debug("batch_parse_signatures: no low-confidence records found")
        return None

    bq = BatchQueue(prefix="sig_parse")
    for extract in records:
        prompt = _build_signature_prompt(extract)
        bq.enqueue(
            str(extract.id),
            {
                "prompt": prompt,
                "schema": _SIGNATURE_SCHEMA,
                "system": _BATCH_SYSTEM_PROMPT,
                "model_tier": "fast",
                "max_tokens": 512,
            },
        )

    requests = bq.build_batch()
    if not requests:
        return None

    batch_id = await claude_batch_submit(requests)
    if not batch_id:
        logger.warning("batch_parse_signatures: claude_batch_submit returned None")
        return None

    if r:
        r.set(_REDIS_KEY, batch_id)

    logger.info("batch_parse_signatures: submitted %d records, batch_id=%s", len(records), batch_id)
    return batch_id


async def process_signature_batch_results(db) -> dict | None:
    """Poll and apply batch signature parsing results.

    Loads the batch_id from Redis, checks for results via claude_batch_results(). If
    results are available, applies parsed data to EmailSignatureExtract records, sets
    extraction_method = 'batch_api' and recalculates confidence.

    Returns {"applied": int, "errors": int} when complete, or None if no batch pending /
    still processing / Redis unavailable.

    On commit failure, returns stats WITHOUT clearing the Redis key so the batch can be
    retried.
    """
    from ..models import EmailSignatureExtract  # noqa: F811

    r = _get_redis()
    if not r:
        return None

    raw = r.get(_REDIS_KEY)
    if not raw:
        return None

    batch_id = raw.decode() if isinstance(raw, bytes) else raw

    results = await claude_batch_results(batch_id)
    if results is None:
        logger.debug("process_signature_batch_results: batch %s still processing", batch_id)
        return None

    stats = {"applied": 0, "errors": 0}
    _FIELDS = ("full_name", "title", "company_name", "phone", "mobile", "website", "address", "linkedin_url")

    for custom_id, parsed in results.items():
        if parsed is None:
            logger.debug("Batch signature result error for %s — skipping", custom_id)
            stats["errors"] += 1
            continue

        # custom_id format: "sig_parse-<record_id>"
        id_parts = custom_id.split("-", 1)
        if len(id_parts) < 2:
            stats["errors"] += 1
            continue
        try:
            record_id = int(id_parts[1])
        except (ValueError, IndexError):
            stats["errors"] += 1
            continue

        record = db.get(EmailSignatureExtract, record_id)
        if not record:
            stats["errors"] += 1
            continue

        try:
            for field in _FIELDS:
                val = parsed.get(field)
                if val:
                    setattr(record, field, val)

            # Calculate confidence — same formula as parse_signature_ai
            fields_found = sum(1 for f in _FIELDS if getattr(record, f, None))
            record.confidence = min(0.5 + (fields_found * 0.08), 0.95)
            record.extraction_method = "batch_api"
            record.updated_at = datetime.now(timezone.utc)
            stats["applied"] += 1
        except Exception as e:
            logger.warning("Failed to apply batch signature for record %d: %s", record_id, e)
            stats["errors"] += 1

    try:
        db.commit()
    except Exception as e:
        logger.error("process_signature_batch_results commit failed: %s", e)
        db.rollback()
        return stats

    r.delete(_REDIS_KEY)
    logger.info(
        "process_signature_batch_results: %d applied, %d errors from batch %s",
        stats["applied"],
        stats["errors"],
        batch_id,
    )
    return stats
