"""Grounded OEM enrichment: Claude reads authoritative OEM / cross-reference pages and
either (a) resolves an OEM/FRU code to the commodity MPN it relabels, or (b) extracts an
OEM-official description. All trust gates enforced in Python — the model's gate claims are
never trusted.

Called by: app.services.authoritative_enrichment_service.enrich_card.
Depends on: app.utils.claude_client, app.utils.claude_errors, app.utils.normalization,
.oem_domains.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from loguru import logger

from app.services.commodity_registry import get_all_commodities
from app.utils.claude_client import claude_json
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

from .oem_domains import is_crossref_domain, is_oem_domain

_MIN_CROSSREF_CONFIDENCE = 0.90
_MIN_OEM_CONFIDENCE = 0.90

# category routes through the F1 ladder's normalize_category (off-vocab → dropped, never
# persisted), so the OEM-description prompt constrains Claude to the canonical commodity
# vocabulary — free-text OEM-page strings ("Memory Module") would be rejected at write time.
_CATEGORY_VOCAB = ", ".join(sorted(get_all_commodities()))


def _confidence(data: dict) -> float:
    """Parse the model's ``confidence`` field, defaulting a missing/malformed value to
    0.0."""
    try:
        return float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------- cross-reference ---------------------------------


@dataclass(frozen=True)
class CrossRefResult:
    """Candidate OEM->commodity-MPN cross-reference (not yet distributor-confirmed).

    Frozen: producers build the full object in one ``return`` and consumers only read, so
    immutability is safe and lets the ``_XR_FAILED`` module singleton be shared without an
    aliasing footgun.
    """

    status: Literal["resolved", "failed"]
    resolved_mpn: str | None = None
    manufacturer: str | None = None
    linkage_source_url: str | None = None
    linkage_source_domain: str | None = None
    confidence: float = 0.0


_XR_FAILED = CrossRefResult(status="failed")

_XR_SYSTEM = (
    "You are an electronics cross-reference assistant. An OEM/system-vendor FRU, spare, or "
    "service part number relabels a commodity component. Use web search to find an "
    "AUTHORITATIVE page (the OEM's own site, or an authorized distributor/manufacturer page) "
    "that shows BOTH the OEM code AND the underlying manufacturer part number together. "
    "Return ONLY valid JSON. Never invent a part number; use null when unknown."
)
_XR_PROMPT = (
    "OEM/FRU part number: {mpn} (vendor: {vendor}). Find the commodity manufacturer part "
    'number it corresponds to. Return JSON: {{"resolved_mpn": str|null, "manufacturer": '
    'str|null, "linkage_quote": str, "confidence": float, "source_urls": [str]}}. '
    "linkage_quote must be the verbatim text from the page that shows the OEM code and the "
    "resolved_mpn together."
)


async def cross_reference_mpn(
    display_mpn: str,
    normalized_mpn: str,
    vendor: str,
    *,
    timeout: int = 90,
) -> CrossRefResult:
    """Resolve an OEM/FRU code to a CANDIDATE commodity MPN via grounded web search.

    Four Python gates: (1) >=1 source URL on a cross-ref allowlist domain; (2) both the OEM
    code and the resolved MPN appear verbatim (normalized) in the sourced ``linkage_quote``
    — this is what makes a real-but-wrong guess detectable; (3) resolved != original (no
    echo); (4) confidence threshold. Returns the candidate only — the caller independently
    re-verifies the MPN against distributors. Raises ClaudeError on backend failure.
    """
    if not normalized_mpn:
        return _XR_FAILED
    try:
        data = await claude_json(
            _XR_PROMPT.format(mpn=display_mpn, vendor=vendor),
            system=_XR_SYSTEM,
            model_tier="smart",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            timeout=timeout,
        )
    except ClaudeError:
        raise
    except Exception as e:
        logger.warning("OEM_XREF: unexpected error for {}: {}", display_mpn, type(e).__name__)
        return _XR_FAILED

    if not isinstance(data, dict):
        return _XR_FAILED

    urls = [u for u in (data.get("source_urls") or []) if isinstance(u, str) and is_crossref_domain(u)]
    if not urls:
        logger.info("OEM_XREF: {} rejected — no trusted source ({})", display_mpn, data.get("source_urls"))
        return _XR_FAILED

    resolved_raw = (data.get("resolved_mpn") or "").strip()
    resolved_key = normalize_mpn_key(resolved_raw)
    if not resolved_key:
        return _XR_FAILED

    # Linkage gate: the FRU<->MPN association is single-attestation (the model's quoted
    # text), checked as a normalized-substring containment of BOTH codes. This is the one
    # place an LLM claim is load-bearing for the *linkage*; it is defended in depth — the
    # source domain must be allowlisted (gate 1), the resolved MPN is INDEPENDENTLY
    # re-verified against a distributor by the caller, and confidence must clear 0.90.
    # A short code embedded in a longer token is an accepted residual (see review notes).
    linkage_key = normalize_mpn_key(data.get("linkage_quote"))
    if normalized_mpn not in linkage_key or resolved_key not in linkage_key:
        logger.info("OEM_XREF: {} rejected — linkage quote missing a code", display_mpn)
        return _XR_FAILED

    if resolved_key == normalized_mpn:
        logger.info("OEM_XREF: {} rejected — resolved == original", display_mpn)
        return _XR_FAILED

    conf = _confidence(data)
    if conf < _MIN_CROSSREF_CONFIDENCE:
        logger.info("OEM_XREF: {} rejected — confidence {:.2f} < {:.2f}", display_mpn, conf, _MIN_CROSSREF_CONFIDENCE)
        return _XR_FAILED

    return CrossRefResult(
        status="resolved",
        resolved_mpn=resolved_raw,
        manufacturer=(data.get("manufacturer") or "").strip() or None,
        linkage_source_url=urls[0],
        linkage_source_domain=urlparse(urls[0]).hostname or "",
        confidence=conf,
    )


# --------------------------------- OEM description ---------------------------------


@dataclass(frozen=True)
class OemExtractResult:
    """Result of an OEM-official description extraction (description/category only).

    Frozen: producers build the full object in one ``return`` and consumers only read, so
    immutability is safe and lets the ``_OEM_FAILED`` module singleton be shared without an
    aliasing footgun.
    """

    status: Literal["oem_sourced", "failed"]
    description: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    datasheet_url: str | None = None
    confidence: float = 0.0
    source_urls: list[str] = field(default_factory=list)
    source_domains: list[str] = field(default_factory=list)


_OEM_FAILED = OemExtractResult(status="failed")

_OEM_SYSTEM = (
    "You are an electronic-component data extraction assistant. Use web search to find the "
    "OFFICIAL OEM/system-vendor page (Lenovo, HPE, HP, Dell, Acer, ASUS, IBM) for the given "
    "OEM/FRU/spare part number. Return ONLY valid JSON. Never invent data; use null when unknown."
)
_OEM_PROMPT = (
    "Find the OEM/FRU part number {mpn} (vendor: {vendor}) on the vendor's official parts or "
    'support page. Return JSON: {{"description": str, "manufacturer": str, "category": str|null, '
    '"datasheet_url": str|null, "confidence": float, "exact_mpn_found": str, "source_urls": '
    "[str]}}. exact_mpn_found must be the OEM code exactly as printed on the page. "
    "category MUST be one of: " + _CATEGORY_VOCAB + " — pick the closest match, or null if none fits."
)


async def extract_oem_description(
    display_mpn: str,
    normalized_mpn: str,
    vendor: str,
    *,
    timeout: int = 90,
) -> OemExtractResult:
    """Extract an OEM-official description/category from the vendor's own page.

    Four Python gates (official OEM domain, exact code verbatim, confidence, non-trivial
    description + manufacturer). Writes description/category/datasheet only — never
    structured specs. Raises ClaudeError on backend failure.
    """
    if not normalized_mpn:
        return _OEM_FAILED
    try:
        data = await claude_json(
            _OEM_PROMPT.format(mpn=display_mpn, vendor=vendor),
            system=_OEM_SYSTEM,
            model_tier="smart",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            timeout=timeout,
        )
    except ClaudeError:
        raise
    except Exception as e:
        logger.warning("OEM_DESC: unexpected error for {}: {}", display_mpn, type(e).__name__)
        return _OEM_FAILED

    if not isinstance(data, dict):
        return _OEM_FAILED

    urls = [u for u in (data.get("source_urls") or []) if isinstance(u, str) and is_oem_domain(u)]
    if not urls:
        logger.info("OEM_DESC: {} rejected — no official OEM source ({})", display_mpn, data.get("source_urls"))
        return _OEM_FAILED

    if normalize_mpn_key(data.get("exact_mpn_found")) != normalized_mpn:
        logger.info("OEM_DESC: {} rejected — MPN mismatch (got {})", display_mpn, data.get("exact_mpn_found"))
        return _OEM_FAILED

    conf = _confidence(data)
    if conf < _MIN_OEM_CONFIDENCE:
        logger.info("OEM_DESC: {} rejected — confidence {:.2f} < {:.2f}", display_mpn, conf, _MIN_OEM_CONFIDENCE)
        return _OEM_FAILED

    desc = (data.get("description") or "").strip()
    mfr = (data.get("manufacturer") or "").strip()
    if len(desc) < 10 or not mfr:
        logger.info(
            "OEM_DESC: {} rejected — description too short ({}) or missing manufacturer", display_mpn, len(desc)
        )
        return _OEM_FAILED

    return OemExtractResult(
        status="oem_sourced",
        description=desc,
        manufacturer=mfr,
        category=(data.get("category") or "").strip() or None,
        datasheet_url=(data.get("datasheet_url") or "").strip() or None,
        confidence=conf,
        source_urls=urls,
        source_domains=sorted({urlparse(u).hostname or "" for u in urls}),
    )
