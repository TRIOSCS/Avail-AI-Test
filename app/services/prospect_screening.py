"""prospect_screening.py — AI account screening for the prospecting queue (SP3).

Calls Claude (claude_structured) with grounded context assembled from SP1-enriched
ProspectAccount fields and returns a validated verdict schema. Persists
trio_match_score + opportunity_score as indexed Integer columns; full verdict in
enrichment_data['ai_screen'] (JSONB).

Called by: prospect_free_enrichment.run_enrichment_job (final step).
Depends on: app.utils.claude_client.claude_structured, app.cache.intel_cache,
            app.config.settings, app.models.prospect_account.ProspectAccount.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.cache import intel_cache
from app.config import settings
from app.models.prospect_account import ProspectAccount

# ── Daily-cap key ────────────────────────────────────────────────────────────

_CAP_KEY_PREFIX = "ai_screen:daily:"


def _cap_key() -> str:
    return _CAP_KEY_PREFIX + datetime.now(UTC).strftime("%Y-%m-%d")


# ── JSON Schema for claude_structured ───────────────────────────────────────

_SCREEN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "trio_match_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0-100: How strongly does this account need electronic components TRIO supplies? Score procurement fit, not size alone.",
        },
        "opportunity_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0-100: Estimated opportunity size/value (company spend potential from size + industry; secondary excess inventory volume).",
        },
        "excess_likelihood": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0-100: Likelihood this account has surplus electronic inventory TRIO could purchase. Secondary signal.",
        },
        "verdict": {
            "type": "string",
            "enum": ["pass", "screened_out", "insufficient_data"],
            "description": "pass = pursue; screened_out = low match; insufficient_data = grounding too thin to judge reliably.",
        },
        "rationale": {
            "type": "string",
            "description": "1-2 sentences grounded in the evidence provided. Must cite specific fields. Never fabricate.",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of evidence items used, e.g. ['industry=Aerospace & Defense', 'naics=336412', 'contacts=1 verified VP'].",
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "0-100: Confidence in this verdict given the grounding quality.",
        },
    },
    "required": [
        "trio_match_score",
        "opportunity_score",
        "excess_likelihood",
        "verdict",
        "rationale",
        "evidence",
        "confidence",
    ],
}

# ── System prompt ────────────────────────────────────────────────────────────

_SCREEN_SYSTEM = (
    "You are a procurement intelligence analyst for TRIO Supply Chain Solutions, "
    "an electronic component broker. Your task is to screen a prospective B2B account "
    "and determine whether they are a genuine target for TRIO.\n\n"
    "TRIO's ideal customers: companies that design, manufacture, or repair products "
    "containing electronic components (ICs, passives, semiconductors, connectors, memory, "
    "storage, displays, PCB assemblies). They need a spot-market / broker channel for "
    "hard-to-find, obsolete, or allocation-constrained parts.\n\n"
    "SCORING RULES:\n"
    "- trio_match_score (0-100): procurement-first fit. How likely is this account to "
    "source electronic components through a broker? Score 80+ only with strong sector "
    "evidence (aerospace/defense, EMS, medical devices, automotive electronics, industrial "
    "controls). Score 40-79 for plausible but uncertain fits. Score <40 for retail, "
    "software-only, staffing, consulting, or companies with no evident BOM.\n"
    "- opportunity_score (0-100): estimated spend potential from company size + industry "
    "context. A 500-person aerospace OEM is 80+; a 10-person IT consultancy is 10.\n"
    "- excess_likelihood (0-100): secondary — does this account likely hold surplus "
    "electronic inventory TRIO could buy? Relevant for OEMs with large inventory.\n"
    "- verdict: use 'insufficient_data' when the grounding fields are too sparse to judge "
    "reliably (no industry, no NAICS, no description, no contacts, no history). "
    "NEVER GUESS OR FABRICATE. Use only the data provided in the context.\n"
    "- rationale: cite the specific evidence fields you used. 1-2 sentences.\n"
    "- evidence: list the grounding fields that drove your verdict, e.g. "
    "['industry=Aerospace & Defense', 'naics=336412', 'size=501-1000', 'contacts=1 verified VP Procurement'].\n"
    "Return ONLY the JSON object conforming to the schema. Do not add prose outside JSON."
)


# ── Context assembly ─────────────────────────────────────────────────────────


def _assemble_context(prospect: ProspectAccount) -> str:
    """Build the grounding prompt from the prospect's enriched fields.

    Uses only fields that already exist on the ProspectAccount — never guesses. Returns
    a plain-text context block Claude will reason over.
    """
    ed = prospect.enrichment_data or {}
    signals = prospect.readiness_signals or {}
    contacts = prospect.contacts_preview or []
    history = prospect.historical_context or {}

    lines: list[str] = [
        f"Company: {prospect.name or 'Unknown'} ({prospect.domain or 'no domain'})",
    ]

    if prospect.industry:
        lines.append(f"Industry: {prospect.industry}")
    if prospect.naics_code:
        lines.append(f"NAICS: {prospect.naics_code}")
    if prospect.employee_count_range:
        lines.append(f"Employees: {prospect.employee_count_range}")
    if prospect.revenue_range:
        lines.append(f"Revenue: {prospect.revenue_range}")
    if prospect.hq_location:
        lines.append(f"HQ: {prospect.hq_location}")
    if prospect.description:
        lines.append(f"Description: {prospect.description[:400]}")

    # SP1 firmographics in enrichment_data
    sam_gov = ed.get("sam_gov") or {}
    if sam_gov.get("purpose"):
        lines.append(f"SAM.gov purpose: {sam_gov['purpose']}")
    if sam_gov.get("naics_codes"):
        primary = next(
            (n for n in sam_gov["naics_codes"] if n.get("primary")),
            sam_gov["naics_codes"][0],
        )
        lines.append(f"SAM.gov primary NAICS: {primary.get('code', '')} — {primary.get('description', '')}")

    # Contacts
    if contacts:
        verified = [c for c in contacts if isinstance(c, dict) and c.get("verified")]
        dms = [c for c in verified if c.get("seniority") == "decision_maker"]
        summary_parts = []
        if dms:
            summary_parts.append(
                f"{len(dms)} verified decision-maker(s): "
                + ", ".join(f"{c.get('name', '?')} ({c.get('title', '?')})" for c in dms[:2])
            )
        elif verified:
            summary_parts.append(f"{len(verified)} verified contact(s)")
        elif contacts:
            summary_parts.append(f"{len(contacts)} unverified contact(s)")
        if summary_parts:
            lines.append("Contacts: " + "; ".join(summary_parts))

    # News signals
    news = ed.get("recent_news") or []
    if news:
        headlines = [n.get("title", "")[:80] for n in news[:3] if n.get("title")]
        if headlines:
            lines.append("Recent news: " + " | ".join(headlines))

    # Hiring/events signals
    hiring = signals.get("hiring") or {}
    if hiring.get("type") and hiring["type"] != "none":
        lines.append(f"Hiring signal: {hiring['type']}")
    events = signals.get("events") or []
    if events:
        event_types = list({e.get("type", "") for e in events[:3] if isinstance(e, dict) and e.get("type")})
        if event_types:
            lines.append(f"Recent events: {', '.join(event_types)}")

    # TRIO history
    if history.get("quote_count"):
        lines.append(f"TRIO history: {history['quote_count']} quotes")
    if history.get("bought_before"):
        lines.append("TRIO history: prior customer (bought before)")
    if history.get("last_activity"):
        lines.append(f"TRIO history: last activity {history['last_activity']}")

    # Historical context freeform
    if prospect.historical_context and not any(
        k in prospect.historical_context for k in ("quote_count", "bought_before", "last_activity")
    ):
        lines.append(f"Historical context: {str(prospect.historical_context)[:200]}")

    return "\n".join(lines)


def _grounding_is_sufficient(prospect: ProspectAccount) -> bool:
    """Return True if we have at least minimal data to make a non-random judgment.

    Minimum bar: at least one of (industry, naics_code, description, or SAM.gov data).
    """
    ed = prospect.enrichment_data or {}
    return bool(prospect.industry or prospect.naics_code or prospect.description or ed.get("sam_gov"))


def _grounding_fingerprint(prospect: ProspectAccount) -> str:
    """Stable hash of the exact grounding the screen reasons over.

    Built from the assembled context string so it captures every field
    ``_assemble_context`` consumes (firmographics, contacts, news, events, history) and
    nothing else — any material new enrichment changes the fingerprint, while unrelated
    column writes do not. Used to invalidate a cached verdict when the grounding has
    materially changed (a buyer re-triggering enrichment must get a fresh screen).
    """
    return hashlib.sha256(_assemble_context(prospect).encode("utf-8")).hexdigest()


# ── LLM call (isolated for mocking) ─────────────────────────────────────────


async def _call_screen_llm(context: str) -> dict:
    """Call Claude with the screening schema. Returns the verdict dict.

    Isolated into its own function so tests can patch it without touching the full
    claude_structured call chain. ``claude_structured`` forces ``tool_choice`` to the
    structured-output tool, so the server-side ``web_search`` tool cannot be attached
    here — web-search grounding rescue is intentionally not wired (see ``screen_prospect``).
    """
    from app.utils.claude_client import claude_structured

    result = await claude_structured(
        context,
        schema=_SCREEN_SCHEMA,
        system=_SCREEN_SYSTEM,
        model_tier="smart",
        max_tokens=512,
        cache_system=True,
        timeout=45,
        cost_bucket="ai_screen",
    )
    return result or {}


# ── Public API ───────────────────────────────────────────────────────────────


async def screen_prospect(prospect: ProspectAccount, db: Session) -> dict:
    """Run the AI screen for one prospect and persist the verdict.

    Returns a dict with at minimum {"verdict": str}. Never raises — fire-and-forget safe.

    Verdict lifecycle:
      "disabled"         — ai_screen_enabled=False (no-op)
      "cap_reached"      — daily cap exhausted; retry tomorrow
      "pass"             — trio_match_score >= min_match; account stays in queue
      "screened_out"     — trio_match_score < min_match; account moves to low-fit bucket
      "insufficient_data"— grounding too thin; sets needs_more_enrichment=True for SP4
      "error"            — LLM/network error; scores not written; logged
    """
    if not settings.ai_screen_enabled:
        return {"verdict": "disabled"}

    # ── Cache hit: already screened on the SAME grounding, return stored verdict ──
    # A stored pass/screened_out is only reused when the grounding fingerprint matches;
    # a buyer re-triggering enrichment with new contacts/firmographics/news changes the
    # fingerprint and forces a fresh screen (spec: re-screen only on material new data).
    ed = dict(prospect.enrichment_data or {})
    existing = ed.get("ai_screen") or {}
    fingerprint = _grounding_fingerprint(prospect)
    if existing.get("verdict") in ("pass", "screened_out") and existing.get("grounding_fingerprint") == fingerprint:
        return existing

    # ── Daily cap gate ──
    # Cap is approximate: get_count → incr_count is not atomic, so concurrent screens can
    # modestly overshoot. Acceptable given the single enrichment-worker drain + soft budget.
    today_count = intel_cache.get_count(_cap_key())
    if today_count >= settings.ai_screen_daily_cap:
        logger.debug(
            "AI screen daily cap reached ({}/{}) — skipping prospect {}",
            today_count,
            settings.ai_screen_daily_cap,
            prospect.id,
        )
        return {"verdict": "cap_reached"}

    try:
        # ── Grounding check — prefer to fetch more enrichment than guess ──
        # ai_screen_web_search_enabled lets the LLM screen thin-grounding accounts anyway
        # (it reasons over whatever context exists); otherwise we short-circuit to
        # insufficient_data and let SP4 enrich first rather than have the model guess.
        if not _grounding_is_sufficient(prospect) and not settings.ai_screen_web_search_enabled:
            verdict_dict: dict = {
                "trio_match_score": 0,
                "opportunity_score": 0,
                "excess_likelihood": 0,
                "verdict": "insufficient_data",
                "rationale": "Insufficient firmographic data to make a reliable judgment.",
                "evidence": [],
                "confidence": 0,
                "model": "none",
                "screened_at": datetime.now(UTC).isoformat(),
                "needs_more_enrichment": True,
            }
            ed["ai_screen"] = verdict_dict
            prospect.enrichment_data = ed
            flag_modified(prospect, "enrichment_data")
            db.commit()
            return verdict_dict

        # ── LLM call ──
        context = _assemble_context(prospect)
        raw = await _call_screen_llm(context)

        if not raw or "verdict" not in raw:
            logger.warning("AI screen returned empty/invalid response for prospect {}", prospect.id)
            return {"verdict": "error", "rationale": "Empty LLM response"}

        # ── Post-process: enforce screened_out if score below threshold ──
        trio_score = int(raw.get("trio_match_score") or 0)
        opp_score = int(raw.get("opportunity_score") or 0)
        verdict = raw.get("verdict", "insufficient_data")

        if verdict == "pass" and trio_score < settings.ai_screen_min_match:
            verdict = "screened_out"

        now_iso = datetime.now(UTC).isoformat()
        verdict_dict = {
            "trio_match_score": trio_score,
            "opportunity_score": opp_score,
            "excess_likelihood": int(raw.get("excess_likelihood") or 0),
            "verdict": verdict,
            "rationale": raw.get("rationale", ""),
            "evidence": raw.get("evidence") or [],
            "confidence": int(raw.get("confidence") or 0),
            "model": raw.get("model", settings.anthropic_model),
            "screened_at": now_iso,
            # Grounding hash drives cache invalidation: a re-screen with materially new
            # enrichment data produces a different fingerprint and bypasses the cache hit.
            "grounding_fingerprint": fingerprint,
        }

        # ── Persist ──
        if verdict == "insufficient_data":
            verdict_dict["needs_more_enrichment"] = True
        else:
            # Write scores only for pass/screened_out (not insufficient_data or error)
            prospect.trio_match_score = trio_score
            prospect.opportunity_score = opp_score

        ed["ai_screen"] = verdict_dict
        prospect.enrichment_data = ed
        flag_modified(prospect, "enrichment_data")
        db.commit()

        # ── Meter daily usage ──
        intel_cache.incr_count(_cap_key(), ttl_days=1.0)

        logger.info(
            "AI screen for prospect {}: verdict={} match={} opp={} confidence={}",
            prospect.id,
            verdict,
            trio_score,
            opp_score,
            verdict_dict["confidence"],
        )
        return verdict_dict

    except Exception as exc:
        logger.warning("AI screen failed for prospect {}: {}", prospect.id, exc)
        return {"verdict": "error", "rationale": str(exc)}
