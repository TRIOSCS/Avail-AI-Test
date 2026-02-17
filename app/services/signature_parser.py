"""Signature parser — extracts contact info from email signatures.

Uses regex for fast extraction, falls back to Claude AI for complex signatures.
Caches results in EmailSignatureExtract table to avoid re-parsing.
"""

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger("avail.signature_parser")

# ── Regex patterns for signature extraction ──────────────────────────

_PHONE_RE = re.compile(
    r"(?:(?:phone|tel|ph|office|direct|main|fax|cell|mobile|m)\s*[:.#]?\s*)"
    r"([\+]?[\d\s\-\.\(\)]{7,20})",
    re.IGNORECASE,
)

_BARE_PHONE_RE = re.compile(
    r"(?<!\d)(\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})(?!\d)"
)

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

_WEBSITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?([a-zA-Z0-9][\w\-]*\.[a-zA-Z]{2,}(?:\.[a-zA-Z]{2,})?)",
    re.IGNORECASE,
)

_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-]+",
    re.IGNORECASE,
)

_TITLE_KEYWORDS = {
    "president", "ceo", "cfo", "coo", "cto", "vp", "vice president",
    "director", "manager", "supervisor", "lead", "head", "chief",
    "engineer", "analyst", "specialist", "coordinator", "associate",
    "sales", "buyer", "purchasing", "procurement", "sourcing",
    "logistics", "supply chain", "account", "representative",
    "admin", "assistant", "executive", "partner", "founder", "owner",
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
    """Extract the signature portion of an email body (last ~15 lines after delimiter)."""
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

    lines = [l.strip() for l in sig_block.split("\n") if l.strip()]

    # Extract phone numbers
    phones = []
    for line in lines:
        for m in _PHONE_RE.finditer(line):
            phone = re.sub(r"[^\d\+\-\.\(\)\s]", "", m.group(1)).strip()
            if len(re.sub(r"\D", "", phone)) >= 7:
                label = line[:m.start()].lower().strip()
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
        log.warning("AI signature parsing failed: %s", e)
        return {"confidence": 0.0}


async def extract_signature(body: str, sender_name: str = "", sender_email: str = "") -> dict:
    """Try regex first, fall back to AI if confidence < 0.7."""
    regex_result = parse_signature_regex(body)

    if regex_result.get("confidence", 0) >= 0.7:
        regex_result["extraction_method"] = "regex"
        return regex_result

    # Try AI for better results
    try:
        ai_result = await parse_signature_ai(body, sender_name, sender_email)
        if ai_result.get("confidence", 0) > regex_result.get("confidence", 0):
            ai_result["extraction_method"] = "claude_ai"
            return ai_result
    except Exception:
        pass

    regex_result["extraction_method"] = "regex"
    return regex_result


def cache_signature_extract(db, sender_email: str, extract: dict) -> None:
    """Upsert an EmailSignatureExtract record."""
    from ..models import EmailSignatureExtract

    existing = (
        db.query(EmailSignatureExtract)
        .filter(EmailSignatureExtract.sender_email == sender_email.lower())
        .first()
    )

    if existing:
        existing.seen_count = (existing.seen_count or 0) + 1
        existing.updated_at = datetime.now(timezone.utc)
        # Only overwrite if new extract has higher confidence
        if extract.get("confidence", 0) > (existing.confidence or 0):
            for field in ("full_name", "title", "company_name", "phone", "mobile",
                          "website", "address", "linkedin_url"):
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
        log.debug("Signature cache flush error: %s", e)
        db.rollback()
