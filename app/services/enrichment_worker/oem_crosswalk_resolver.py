"""Claude-grounded OEM spare→canonical-MPN resolver for the oem_crosswalk cache.

Resolves an OEM/system-vendor spare PN (Phase A: HP/HPE via PartSurfer at
https://partsurfer.hp.com) to the canonical manufacturer MPN it relabels, plus the
OEM page's part title — via Claude ``web_search`` grounded extraction over the
allowlisted OEM/cross-ref domains. There is NO direct HTTP to PartSurfer/PSREF, ever:
HTML drift is absorbed by the LLM; correctness is enforced by the five Python trust
gates below, never the LLM's claims. A gate failure IS ``no_match`` (cached 90 days by
the caller); a ``ClaudeError`` propagates (transient — the caller writes NO row).

Called by: app/services/enrichment_worker/worker.py (Pass A resolution),
app/management/backfill_oem_crosswalk.py (paced drain CLI).
Depends on: app.utils.claude_client.claude_json, app.utils.claude_errors.ClaudeError,
app.utils.normalization.normalize_mpn_key, .oem_domains, .oem_extractor
(_MIN_CROSSREF_CONFIDENCE — same 0.90 bar as the ephemeral cross-ref path).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from loguru import logger

from app.utils.claude_client import claude_json
from app.utils.normalization import normalize_mpn_key

from .oem_domains import is_crossref_domain, is_oem_domain
from .oem_extractor import _MIN_CROSSREF_CONFIDENCE

# Per-vendor official lookup surface, used only to steer the search prompt — the
# domain GATE below is the allowlist (oem_domains), never this hint.
_VENDOR_LOOKUP_HINT: dict[str, str] = {
    "hpe": "HP PartSurfer (https://partsurfer.hp.com)",
    "lenovo": "Lenovo PSREF (https://psref.lenovo.com)",
}

_RESOLVE_SYSTEM = (
    "You are an electronics cross-reference assistant. An OEM/system-vendor spare or "
    "service part number relabels a commodity component. Use web search to find an "
    "AUTHORITATIVE page (the OEM's own parts lookup, or an authorized distributor/"
    "manufacturer page) that shows BOTH the OEM spare number AND the underlying "
    "canonical manufacturer part number together. Return ONLY valid JSON. Never invent "
    "a part number; use null when unknown."
)
_RESOLVE_PROMPT = (
    "OEM spare part number: {mpn} (vendor: {vendor}). Look it up on {lookup} or another "
    "authoritative page and find the canonical manufacturer part number it corresponds "
    'to. Return JSON: {{"canonical_mpn": str|null, "manufacturer": str|null, '
    '"title": str|null, "quote": str, "confidence": float, "source_urls": [str]}}. '
    "title must be the part title/description verbatim as printed on the OEM page. "
    "quote must be the verbatim text from the page that shows the OEM spare number and "
    "the canonical_mpn together."
)


@dataclass(frozen=True)
class OemResolveResult:
    """Outcome of one grounded spare→canonical resolution attempt.

    Frozen: built in one ``return``, consumers only read. ``status='no_match'`` covers
    both "the OEM genuinely doesn't catalogue it" and "a trust gate failed" — the
    caller caches either for 90 days. ``payload`` carries the full raw extraction for
    forensics (persisted on the row for both outcomes).
    """

    status: Literal["resolved", "no_match"]
    canonical_mpn: str | None = None
    manufacturer: str | None = None
    title: str | None = None
    source_url: str | None = None
    source_domain: str | None = None
    confidence: float = 0.0
    payload: dict | None = None


async def resolve_oem_spare(
    display_mpn: str,
    normalized_mpn: str,
    vendor: str,
    *,
    timeout: int = 90,
) -> OemResolveResult:
    """Resolve an OEM spare PN to its canonical manufacturer MPN via grounded search.

    Five Python gates (ALL must pass, in order; any fail → ``no_match``):
    1. domain — ≥1 source URL passes ``is_oem_domain`` or ``is_crossref_domain``;
    2. verbatim — ``normalize_mpn_key`` of BOTH the spare AND the canonical MPN appear
       in the returned ``quote`` text (a real-but-wrong guess is detectable);
    3. no-echo — the canonical norm differs from the spare norm;
    4. confidence ≥ 0.90 (``_MIN_CROSSREF_CONFIDENCE``);
    5. null/malformed fields degrade gracefully to ``no_match`` (never raises on shape).

    Raises ``ClaudeError`` on backend failure (the caller writes no row — the spare is
    retried next batch for free).
    """
    if not normalized_mpn:
        return OemResolveResult(status="no_match")

    data = await claude_json(
        _RESOLVE_PROMPT.format(
            mpn=display_mpn,
            vendor=vendor,
            lookup=_VENDOR_LOOKUP_HINT.get(vendor, "the vendor's official parts lookup"),
        ),
        system=_RESOLVE_SYSTEM,
        model_tier="smart",
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
        timeout=timeout,
    )

    if not isinstance(data, dict):
        return OemResolveResult(status="no_match")
    no_match = OemResolveResult(status="no_match", payload=data)

    # Gate 1 — domain: at least one source URL on the OEM/cross-ref allowlist.
    urls = [
        u for u in (data.get("source_urls") or []) if isinstance(u, str) and (is_oem_domain(u) or is_crossref_domain(u))
    ]
    if not urls:
        logger.info("OEM_RESOLVE: {} no_match — no trusted source ({})", display_mpn, data.get("source_urls"))
        return no_match

    canonical_raw = str(data.get("canonical_mpn") or "").strip()
    canonical_norm = normalize_mpn_key(canonical_raw)
    if not canonical_norm:
        return no_match

    # Gate 2 — verbatim: BOTH codes appear (normalized) in the sourced quote. This is
    # the one place an LLM claim is load-bearing for the linkage; it is defended in
    # depth by the allowlisted domain (gate 1) and the 0.90 confidence bar (gate 4).
    quote_key = normalize_mpn_key(data.get("quote"))
    if normalized_mpn not in quote_key or canonical_norm not in quote_key:
        logger.info("OEM_RESOLVE: {} no_match — quote missing a code", display_mpn)
        return no_match

    # Gate 3 — no-echo: the "canonical" MPN must not be the spare itself.
    if canonical_norm == normalized_mpn:
        logger.info("OEM_RESOLVE: {} no_match — canonical == spare", display_mpn)
        return no_match

    # Gate 4 — confidence threshold (shape-defensive: gate 5 applies here too).
    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _MIN_CROSSREF_CONFIDENCE:
        logger.info(
            "OEM_RESOLVE: {} no_match — confidence {:.2f} < {:.2f}", display_mpn, conf, _MIN_CROSSREF_CONFIDENCE
        )
        return no_match

    return OemResolveResult(
        status="resolved",
        canonical_mpn=canonical_raw,
        manufacturer=str(data.get("manufacturer") or "").strip() or None,
        title=str(data.get("title") or "").strip() or None,
        source_url=urls[0],
        source_domain=urlparse(urls[0]).hostname or "",
        confidence=conf,
        payload=data,
    )
