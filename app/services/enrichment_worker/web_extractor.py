"""Grounded web-search enrichment: Claude reads authoritative pages and extracts
description/manufacturer/category/datasheet. Four gates enforced in Python."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from loguru import logger

from app.utils.claude_client import claude_json
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

from .trusted_domains import is_trusted_domain

_MIN_WEB_CONFIDENCE = 0.92

_SYSTEM = (
    "You are an electronic component data extraction assistant. Use web search to find "
    "AUTHORITATIVE manufacturer or authorized-distributor pages for the given MPN. "
    "Return ONLY valid JSON. Never invent data; use null when unknown."
)
_PROMPT = (
    "Find the exact electronic component MPN {mpn} on a manufacturer or authorized distributor "
    'page. Return JSON: {{"description": str, "manufacturer": str, "category": str, '
    '"datasheet_url": str|null, "confidence": float, "exact_mpn_found": str, '
    '"source_urls": [str]}}. exact_mpn_found must be the MPN exactly as printed on the page.'
)


@dataclass
class WebExtractResult:
    """Result of a web-search enrichment attempt."""

    status: str  # "web_sourced" | "failed"
    description: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    datasheet_url: str | None = None
    confidence: float = 0.0
    source_urls: list[str] = field(default_factory=list)
    source_domains: list[str] = field(default_factory=list)


_FAILED = WebExtractResult(status="failed")


async def extract_part_from_web(
    display_mpn: str,
    normalized_mpn: str,
    *,
    timeout: int = 90,
) -> WebExtractResult:
    """Extract part data from authoritative web pages via Claude web search.

    Calls Claude with the ``web_search_20250305`` tool, then enforces four
    trust gates in Python (domain allowlist, exact-MPN match, confidence
    threshold, URL presence).  Never trusts the LLM's own gate claims.

    Returns a ``WebExtractResult`` with ``status="web_sourced"`` on success
    or ``status="failed"`` on any gate failure or Claude exception.
    """
    if not normalized_mpn:
        # Defense-in-depth: an empty key would let Gate 2 pass on a missing
        # exact_mpn_found (both normalize to ""). Non-reachable in practice
        # (normalized_mpn is NOT NULL/unique), but cheap to guard.
        return _FAILED
    try:
        data = await claude_json(
            _PROMPT.format(mpn=display_mpn),
            system=_SYSTEM,
            model_tier="smart",
            max_tokens=1200,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            timeout=timeout,
        )
    except ClaudeError:
        # Claude backend failure (auth / rate-limit / server / unreachable). Surface it so
        # the worker's circuit breaker can detect a sustained outage — and do NOT fall
        # through to infer_part (another Claude call) which would also fail. A genuine
        # "couldn't find it" reply is a parsed result, not an exception, and still falls through.
        raise
    except Exception as e:
        logger.warning("WEB_ENRICH: unexpected error for {}: {}", display_mpn, type(e).__name__)
        return _FAILED

    if not isinstance(data, dict):
        return _FAILED

    # Gate 1: trusted domains — filter to only URLs from the allowlist
    urls = [u for u in (data.get("source_urls") or []) if isinstance(u, str) and is_trusted_domain(u)]
    if not urls:
        logger.info(
            "WEB_ENRICH: {} rejected — no trusted source ({})",
            display_mpn,
            data.get("source_urls"),
        )
        return _FAILED

    # Gate 2: exact MPN verbatim — normalize and compare
    if normalize_mpn_key(data.get("exact_mpn_found")) != normalized_mpn:
        logger.info(
            "WEB_ENRICH: {} rejected — MPN mismatch (got {})",
            display_mpn,
            data.get("exact_mpn_found"),
        )
        return _FAILED

    # Gate 3: confidence threshold
    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _MIN_WEB_CONFIDENCE:
        logger.info(
            "WEB_ENRICH: {} rejected — confidence {:.2f} < {:.2f}",
            display_mpn,
            conf,
            _MIN_WEB_CONFIDENCE,
        )
        return _FAILED

    # Gate 4 (quality): non-trivial description and manufacturer must be present
    desc = (data.get("description") or "").strip()
    mfr = (data.get("manufacturer") or "").strip()
    if len(desc) < 10 or not mfr:
        logger.info(
            "WEB_ENRICH: {} rejected — description too short ({}) or missing manufacturer",
            display_mpn,
            len(desc),
        )
        return _FAILED

    return WebExtractResult(
        status="web_sourced",
        description=desc,
        manufacturer=mfr,
        category=(data.get("category") or "").strip() or None,
        datasheet_url=(data.get("datasheet_url") or "").strip() or None,
        confidence=conf,
        source_urls=urls,
        source_domains=sorted({urlparse(u).hostname or "" for u in urls}),
    )
