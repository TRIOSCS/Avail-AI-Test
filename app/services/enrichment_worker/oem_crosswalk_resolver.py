"""Claude-grounded OEM spare→canonical-MPN resolver for the oem_crosswalk cache.

Resolves an OEM/system-vendor spare PN (Phase A: HP/HPE via PartSurfer at
https://partsurfer.hp.com) to the canonical manufacturer MPN it relabels, plus the
OEM page's part title — via Claude ``web_search`` grounded extraction over the
allowlisted OEM/cross-ref domains. There is NO direct HTTP to PartSurfer/PSREF, ever:
HTML drift is absorbed by the LLM; correctness is enforced by the five Python trust
gates below, never the LLM's claims. A gate failure IS ``no_match`` (cached 90 days by
the caller); a ``ClaudeError`` propagates (transient — the caller writes NO row). An
unparseable/empty model response is ALSO transient (raised as ``ClaudeError``), never a
90-day ``no_match`` — a truncated reply is not evidence the OEM doesn't catalogue the
spare.

Unlike the ephemeral ``oem_extractor.cross_reference_mpn`` (whose output is
independently re-verified against distributors by ``enrich_card``), a resolution here
is minted into a PERMANENT cache with no downstream re-verification — so the verbatim
gate matches on token boundaries (never substrings of the collapsed quote) and rejects
short/containment-shaped canonicals outright.

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
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

from .oem_domains import is_crossref_domain, is_oem_domain
from .oem_extractor import _MIN_CROSSREF_CONFIDENCE

# Per-vendor official lookup surface, used only to steer the search prompt — the
# domain GATE below is the allowlist (oem_domains), never this hint.
_VENDOR_LOOKUP_HINT: dict[str, str] = {
    "hpe": "HP PartSurfer (https://partsurfer.hp.com)",
    "lenovo": "Lenovo PSREF (https://psref.lenovo.com)",
}

# Canonical-shape guards: every canonical MPN in scope (Intel tray codes, storage and
# DRAM model numbers) normalizes to 8+ chars, so anything under 6 is hallucination-
# shaped (a 2-5 char "canonical" trivially appears inside real page text); anything
# over the 64-char column width of oem_crosswalk.canonical_mpn_raw is garbage.
_MIN_CANONICAL_NORM_LEN = 6
_MAX_CANONICAL_RAW_LEN = 64

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
    '"title": str|null, "quote": str, "confidence": float, "source_url": str}}. '
    "title must be the part title/description verbatim as printed on the OEM page. "
    "quote must be the verbatim text from the page that shows the OEM spare number and "
    "the canonical_mpn together. source_url must be the URL of the exact page the "
    "quote was taken from."
)


@dataclass(frozen=True)
class OemResolveResult:
    """Outcome of one grounded spare→canonical resolution attempt.

    Frozen: built in one ``return``, consumers only read. ``status='no_match'`` covers
    both "the OEM genuinely doesn't catalogue it" and "a trust gate failed" — the
    caller caches either for 90 days. ``payload`` carries the full raw extraction for
    forensics (persisted on the row for both outcomes). ``__post_init__`` rejects
    illegal shapes: ``resolved`` REQUIRES a canonical MPN, a source URL and
    confidence ≥ 0.90 (the resolver can never mint a resolved result the writers'
    nullability invariant — ck_oem_crosswalk_status_canonical — would violate).
    """

    status: Literal["resolved", "no_match"]
    canonical_mpn: str | None = None
    manufacturer: str | None = None
    title: str | None = None
    source_url: str | None = None
    source_domain: str | None = None
    confidence: float = 0.0
    payload: dict | None = None

    def __post_init__(self) -> None:
        if self.status == "resolved" and (
            not self.canonical_mpn or not self.source_url or self.confidence < _MIN_CROSSREF_CONFIDENCE
        ):
            raise ValueError(
                "resolved OemResolveResult requires canonical_mpn, source_url and "
                f"confidence >= {_MIN_CROSSREF_CONFIDENCE}"
            )


def _quote_token_keys(quote: object) -> set[str]:
    """``normalize_mpn_key`` of each whitespace-delimited token of the verbatim quote.

    Token-boundary matching: gate 2 requires the spare and canonical norms to be
    MEMBERS of this set, never substrings of the collapsed quote — collapsed-blob
    containment admits cross-token spans ('125W FIO' → '125wfio'), truncated echoes of
    the spare, and title fragments ('Gold 6130'). PNs print as contiguous tokens on
    OEM pages; a legitimate resolution lost to this strictness is a recoverable
    no_match, while a fabricated canonical minted into the permanent cache is not.
    Non-string/missing quotes yield an empty set (gate 2 then fails → no_match).
    """
    if not isinstance(quote, str):
        return set()
    return {key for tok in quote.split() if (key := normalize_mpn_key(tok))}


async def resolve_oem_spare(
    display_mpn: str,
    normalized_mpn: str,
    vendor: str,
    *,
    timeout: int = 90,
) -> OemResolveResult:
    """Resolve an OEM spare PN to its canonical manufacturer MPN via grounded search.

    Five Python gates (ALL must pass, in order; any fail → ``no_match``):
    1. domain — the single ``source_url`` (the page the quote was taken from) passes
       ``is_oem_domain`` or ``is_crossref_domain`` — the quote's provenance is gated,
       not merely *some* URL the model visited;
    2. verbatim — ``normalize_mpn_key`` of BOTH the spare AND the canonical MPN appear
       as whole TOKENS of the returned ``quote`` (token-boundary membership, never
       substring of the collapsed quote), and the canonical normalizes to ≥ 6 chars
       (≤ 64 raw) — a real-but-wrong guess is detectable;
    3. no-echo — neither norm contains the other (rejects the spare itself AND
       truncations/extensions of it masquerading as the canonical);
    4. confidence ≥ 0.90 (``_MIN_CROSSREF_CONFIDENCE``);
    5. null/malformed FIELDS degrade gracefully to ``no_match`` (never raises on a
       parsed dict's shape).

    Raises ``ClaudeError`` on backend failure AND on an unparseable/empty response
    (max_tokens truncation mid-JSON, tool-only output) — both are transient, so the
    caller writes NO row and the spare is retried next batch for free. Only a parsed
    dict is ever cached as ``no_match``.
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
        cost_bucket="enrichment",
    )

    if not isinstance(data, dict):
        # Empty/truncated/unparseable output is TRANSIENT — not evidence the OEM
        # doesn't catalogue the spare. Raising keeps the negative cache honest: the
        # caller writes NO row (free retry next batch) instead of locking a possibly
        # catalogued spare out for 90 days with payload=None forensics.
        raise ClaudeError(f"unparseable OEM resolution response for {display_mpn} ({type(data).__name__})")
    no_match = OemResolveResult(status="no_match", payload=data)

    # Gate 1 — domain: the page the quote was taken from is on the OEM/cross-ref
    # allowlist. The contract is a SINGLE source_url so the persisted provenance
    # (row.source_url/source_domain + the enrichment_provenance audit entry) is the
    # quote's actual origin — a list would let an untrusted-page quote ride along
    # with one unrelated allowlisted URL.
    src = data.get("source_url")
    if not isinstance(src, str) or not (is_oem_domain(src) or is_crossref_domain(src)):
        logger.info("OEM_RESOLVE: {} no_match — quote source not on the trusted allowlist ({})", display_mpn, src)
        return no_match

    canonical_raw = str(data.get("canonical_mpn") or "").strip()
    canonical_norm = normalize_mpn_key(canonical_raw)
    if len(canonical_norm) < _MIN_CANONICAL_NORM_LEN or len(canonical_raw) > _MAX_CANONICAL_RAW_LEN:
        if canonical_raw:
            logger.info("OEM_RESOLVE: {} no_match — canonical {!r} fails shape guard", display_mpn, canonical_raw)
        return no_match

    # Gate 2 — verbatim: BOTH codes appear as whole tokens of the sourced quote. This
    # is the load-bearing hallucination defense for a PERMANENT cache: token-boundary
    # membership (never substring of the collapsed quote) rejects title fragments,
    # truncated codes and cross-token spans that separator-stripping would fabricate.
    quote_keys = _quote_token_keys(data.get("quote"))
    if normalized_mpn not in quote_keys or canonical_norm not in quote_keys:
        logger.info("OEM_RESOLVE: {} no_match — quote missing a code as a whole token", display_mpn)
        return no_match

    # Gate 3 — no-echo: the "canonical" must not be the spare itself NOR a
    # truncation/extension of it (containment either way).
    if canonical_norm in normalized_mpn or normalized_mpn in canonical_norm:
        logger.info("OEM_RESOLVE: {} no_match — canonical echoes the spare", display_mpn)
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
        source_url=src,
        source_domain=urlparse(src).hostname or "",
        confidence=conf,
        payload=data,
    )
