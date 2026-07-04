"""Phase 4 — Signal gap-fill, similar customer matching, and AI writeups.

Enriches prospect accounts with:
- Missing firmographics (industry/size/HQ/NAICS/revenue) via the verified Explorium
  connector — backfill for email-mined and manually-added prospects
- Similar existing Trio customers (industry/size/region matching)
- AI-generated sales writeups (Claude Haiku with template fallback)

All functions are idempotent — calling twice with same data doesn't duplicate.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models.crm import Company
from app.models.prospect_account import ProspectAccount
from app.services.prospect_scoring import (
    ICP_SEGMENTS,
    calculate_fit_score,
)

# ── Missing Signal Backfill ──────────────────────────────────────────


async def enrich_missing_signals(prospect_id: int, db: Session) -> bool:
    """Backfill a prospect's missing firmographics via the verified Explorium connector.

    Primarily serves email-mined and manually-added prospects that lack firmographic
    detail (industry / employee size / HQ / NAICS / revenue). Calls Explorium's real
    ``match → firmographics/enrich`` pipeline (``explorium.enrich_company``) and fills ONLY
    the fields that are currently empty, then recomputes the ICP fit score.

    Explorium returns firmographics only — NOT intent/hiring/events — so this is a
    firmographic backfill, not a readiness-signal backfill. Self-gating: returns False when
    Explorium is disabled, its circuit is open, or the credential is missing. Skips the paid
    call entirely when the prospect has no domain or its firmographics are already complete.

    Returns True if any field was backfilled, False otherwise.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        logger.warning("enrich_missing_signals: prospect {} not found", prospect_id)
        return False

    if not prospect.domain:
        return False

    # Firmographics already complete — skip the paid call.
    if prospect.industry and prospect.employee_count_range and prospect.region:
        return False

    # Lazy imports to avoid circular dependencies.
    from app.config import settings
    from app.connectors import explorium
    from app.services import enrichment_credit_guard as cg
    from app.services.credential_service import get_credential_cached
    from app.services.prospect_discovery_explorium import _detect_region

    if not settings.explorium_enrichment_enabled or cg.circuit_open("explorium"):
        return False

    api_key = get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
    if not api_key:
        logger.warning("Explorium credential not configured — skipping firmographic backfill")
        return False

    try:
        company = await explorium.enrich_company(prospect.domain, prospect.name or "", api_key)
    except cg.ProviderQuotaError:
        cg.trip_circuit("explorium", settings.explorium_cooldown_minutes)
        return False
    except Exception as e:
        logger.error("Firmographic backfill error for prospect {}: {}", prospect_id, e)
        return False

    if not company:
        return False

    added = False

    if not prospect.industry and company.get("industry"):
        prospect.industry = company["industry"]
        added = True
    if not prospect.employee_count_range and company.get("employee_size"):
        prospect.employee_count_range = company["employee_size"]
        added = True
    if not prospect.naics_code and company.get("naics"):
        prospect.naics_code = company["naics"]
        added = True
    if not prospect.revenue_range and company.get("revenue_range"):
        prospect.revenue_range = company["revenue_range"]
        added = True
    if not prospect.website and company.get("website"):
        prospect.website = company["website"]
        added = True

    if not prospect.hq_location:
        hq_location = ", ".join(
            part for part in (company.get("hq_city"), company.get("hq_state"), company.get("hq_country")) if part
        )
        if hq_location:
            prospect.hq_location = hq_location
            added = True

    if not prospect.region:
        region = _detect_region({"hq_country": company.get("hq_country")})
        if region:
            prospect.region = region
            added = True

    if added:
        prospect_data = {
            "name": prospect.name,
            "industry": prospect.industry,
            "naics_code": prospect.naics_code,
            "employee_count_range": prospect.employee_count_range,
            "region": prospect.region,
        }
        prospect.fit_score, prospect.fit_reasoning = calculate_fit_score(prospect_data)
        prospect.last_enriched_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("Backfilled firmographics for prospect {} ({})", prospect_id, prospect.domain)

    return added


# ── Similar Customer Matching ────────────────────────────────────────

# Region → set of recognized HQ country codes/names (all uppercase).
REGION_COUNTRIES = {
    "US": {"US", "USA", "UNITED STATES"},
    "EU": {
        "DE",
        "GB",
        "FR",
        "NL",
        "SE",
        "IT",
        "ES",
        "CH",
        "AT",
        "BE",
        "GERMANY",
        "UNITED KINGDOM",
        "FRANCE",
        "NETHERLANDS",
    },
    "ASIA": {
        "CN",
        "JP",
        "KR",
        "TW",
        "SG",
        "IN",
        "CHINA",
        "JAPAN",
        "SOUTH KOREA",
        "TAIWAN",
        "SINGAPORE",
        "INDIA",
    },
}


def _load_owned_companies(db: Session) -> list:
    """Fetch the owned-company rows find_similar_customers matches against.

    Selects only the five columns the matcher reads (name/domain/industry/employee_size/
    hq_country) as lightweight ``Row``s — immune to the expire-on-commit that would
    otherwise re-SELECT full ORM objects each loop iteration. Load ONCE per batch and hand
    to find_similar_customers to kill the O(P×C) N+1 (audit M9).
    """
    return (
        db.query(
            Company.name,
            Company.domain,
            Company.industry,
            Company.employee_size,
            Company.hq_country,
        )
        .filter(
            Company.account_owner_id.isnot(None),
            Company.is_active.is_(True),
        )
        .all()
    )


def find_similar_customers(prospect: ProspectAccount, db: Session, owned_companies: list | None = None) -> list[dict]:
    """Compare prospect against existing owned Company records.

    Matching logic:
      - Same NAICS 4-digit prefix = strong match
      - Same industry keyword + similar size = moderate match
      - Same region + similar size = weak match

    Returns top 3 most similar existing Trio customers.
    Stores results in prospect.similar_customers JSONB.

    ``owned_companies`` lets the monthly batch pre-load the owned-company set ONCE and reuse
    it across every prospect (audit M9 — otherwise each prospect re-scans all owned
    companies, an O(P×C) full-table sweep). ``None`` self-loads (standalone callers).
    """
    # Owned companies (have an account_owner_id) — reuse the caller's set or load our own.
    companies = owned_companies if owned_companies is not None else _load_owned_companies(db)

    if not companies:
        logger.debug("No owned companies to match against")
        prospect.similar_customers = []
        db.commit()
        return []

    matches = []

    for company in companies:
        match_reasons = []
        strength = "none"
        score = 0

        # NAICS 4-digit prefix match (strong)
        if prospect.naics_code and len(prospect.naics_code) >= 4:
            prospect_naics_4 = prospect.naics_code[:4]
            # Company doesn't have naics_code directly, but check industry overlap
            # through ICP segments
            for seg in ICP_SEGMENTS.values():
                seg_naics_4 = {c[:4] for c in seg["naics_codes"]}
                if prospect_naics_4 in seg_naics_4:
                    # Check if company industry matches same segment keywords
                    if company.industry and any(kw in company.industry.lower() for kw in seg["keywords"]):
                        match_reasons.append(f"Same industry segment: {seg['name']}")
                        score += 30
                        break

        # Industry keyword match (moderate)
        if prospect.industry and company.industry:
            p_words = set(prospect.industry.lower().split())
            c_words = set(company.industry.lower().split())
            overlap = p_words & c_words - {"and", "the", "of", "for", "in", "a", "an"}
            if overlap:
                match_reasons.append(f"Industry overlap: {', '.join(sorted(overlap))}")
                score += 15

        # Size similarity
        size_similar = _compare_sizes(prospect.employee_count_range, company.employee_size)
        if size_similar:
            match_reasons.append("Similar company size")
            score += 10

        # Region match
        if prospect.region and company.hq_country:
            countries = REGION_COUNTRIES.get(prospect.region.upper(), set())
            if company.hq_country.upper() in countries:
                match_reasons.append(f"Same region: {prospect.region}")
                score += 5

        if score > 0:
            # Classify overall strength
            if score >= 25:
                strength = "strong"
            elif score >= 15:
                strength = "moderate"
            else:
                strength = "weak"

            matches.append(
                {
                    "name": company.name,
                    "domain": company.domain,
                    "match_reason": "; ".join(match_reasons),
                    "match_strength": strength,
                    "score": score,
                }
            )

    # Sort by score descending, take top 3
    matches.sort(key=lambda m: m["score"], reverse=True)
    top_matches = matches[:3]

    # Remove internal score from output
    result = [{k: v for k, v in m.items() if k != "score"} for m in top_matches]

    prospect.similar_customers = result
    db.commit()

    logger.info(
        "Found {} similar customers for prospect {} (top: {})",
        len(result),
        prospect.id,
        [m["name"] for m in result],
    )

    return result


def _compare_sizes(range1: str | None, range2: str | None) -> bool:
    """Check if two employee size ranges are in similar brackets.

    Adjacent brackets count as similar (e.g., 201-500 and 501-1000).
    """
    if not range1 or not range2:
        return False

    brackets = [
        (0, 50),
        (51, 200),
        (201, 500),
        (501, 1000),
        (1001, 5000),
        (5001, 10000),
        (10001, 999999),
    ]

    def _to_bracket_index(s: str) -> int | None:
        s = s.strip().replace(",", "")
        # Try to extract a number
        num = None
        if "+" in s:
            try:
                num = int(s.replace("+", ""))
            except ValueError:
                pass
        elif "-" in s:
            parts = s.split("-")
            try:
                num = (int(parts[0]) + int(parts[1])) // 2
            except (ValueError, IndexError):
                pass
        else:
            try:
                num = int(s)
            except ValueError:
                pass

        if num is None:
            return None

        for i, (lo, hi) in enumerate(brackets):
            if lo <= num <= hi:
                return i
        return None

    idx1 = _to_bracket_index(range1)
    idx2 = _to_bracket_index(range2)

    if idx1 is None or idx2 is None:
        return False

    # Same bracket or adjacent
    return abs(idx1 - idx2) <= 1


# ── AI Writeup Generation ───────────────────────────────────────────


WRITEUP_SYSTEM = (
    "You are a sales intelligence analyst for Trio Sourcing / AVAIL, "
    "an electronic component sourcing company. Write concise, factual "
    "2-3 sentence prospect summaries for the sales team. Focus on company "
    "profile, why they match the ICP, and any buying signals. "
    "Do not use marketing language or speculation."
)


def _build_writeup_prompt(prospect: ProspectAccount) -> str:
    """Build the prompt for AI writeup generation."""
    signals = prospect.readiness_signals or {}
    similar = prospect.similar_customers or []

    parts = [
        f"Company: {prospect.name}",
        f"Domain: {prospect.domain}",
    ]
    if prospect.industry:
        parts.append(f"Industry: {prospect.industry}")
    if prospect.employee_count_range:
        parts.append(f"Size: {prospect.employee_count_range} employees")
    if prospect.revenue_range:
        parts.append(f"Revenue: {prospect.revenue_range}")
    if prospect.hq_location:
        parts.append(f"HQ: {prospect.hq_location}")
    if prospect.fit_score:
        parts.append(f"ICP Fit Score: {prospect.fit_score}/100")
    if prospect.fit_reasoning:
        parts.append(f"Fit Reasoning: {prospect.fit_reasoning}")

    # Signals
    intent = signals.get("intent", {})
    if intent:
        strength = intent.get("strength", "unknown")
        topics = intent.get("component_topics", [])
        parts.append(f"Intent: {strength} ({', '.join(topics[:3])})" if topics else f"Intent: {strength}")

    hiring = signals.get("hiring", {})
    if hiring:
        parts.append(f"Hiring: {hiring.get('type', 'unknown')} dept growth")

    events = signals.get("events", [])
    if events:
        event_types = [e.get("type", "") for e in events if isinstance(e, dict)]
        parts.append(f"Recent events: {', '.join(event_types[:3])}")

    # Similar customers
    if similar:
        sim_names = [s.get("name", "") for s in similar[:3] if isinstance(s, dict)]
        parts.append(f"Similar existing customers: {', '.join(sim_names)}")

    parts.append("\nWrite a 2-3 sentence sales-ready summary of this prospect.")

    return "\n".join(parts)


def _template_fallback_writeup(prospect: ProspectAccount) -> str:
    """Generate a template-based writeup when Claude API is unavailable."""
    signals = prospect.readiness_signals or {}
    similar = prospect.similar_customers or []

    size = prospect.employee_count_range or "unknown-size"
    industry = prospect.industry or "unknown industry"
    location = prospect.hq_location or "undisclosed location"

    # Segment reason from fit_reasoning
    segment_reason = ""
    if prospect.fit_reasoning:
        # Extract the industry segment match from reasoning
        parts = prospect.fit_reasoning.split(";")
        for part in parts:
            if "Industry:" in part:
                segment_reason = part.strip()
                break

    # Top signal
    top_signal = ""
    intent = signals.get("intent", {})
    if isinstance(intent, dict) and intent.get("strength") in ("strong", "moderate"):
        top_signal = f"Active component sourcing intent detected ({intent['strength']})"
    elif signals.get("events"):
        events = signals["events"]
        if isinstance(events, list) and events:
            top_signal = f"Recent event: {events[0].get('type', 'activity')}"
    elif isinstance(signals.get("hiring"), dict) and signals["hiring"].get("type"):
        top_signal = f"Hiring in {signals['hiring']['type']} department"

    # Similar customers mention
    sim_text = ""
    if similar:
        sim_names = [s.get("name", "") for s in similar[:2] if isinstance(s, dict) and s.get("name")]
        if sim_names:
            sim_text = f" Similar to existing customer{'s' if len(sim_names) > 1 else ''} {' and '.join(sim_names)}."

    sentence1 = f"{prospect.name} is a {size} employee {industry} company in {location}."
    sentence2 = f"{segment_reason}." if segment_reason else ""
    sentence3 = f"{top_signal}." if top_signal else ""

    writeup = sentence1
    if sentence2:
        writeup += f" {sentence2}"
    if sentence3:
        writeup += f" {sentence3}"
    if sim_text:
        writeup += sim_text

    return writeup.strip()


async def generate_ai_writeup(prospect: ProspectAccount, db: Session) -> str:
    """Generate a 2-3 sentence sales-ready writeup using Claude Haiku.

    Falls back to template-based writeup if Claude API is unavailable. Stores result in
    prospect.ai_writeup field.
    """
    # Build prompt with all available enrichment data
    prompt = _build_writeup_prompt(prospect)

    writeup = None
    try:
        from app.utils.claude_client import claude_text

        writeup = await claude_text(
            prompt,
            system=WRITEUP_SYSTEM,
            model_tier="fast",  # Haiku for cost efficiency
            max_tokens=300,
            timeout=15,
        )
    except Exception as e:
        logger.warning("Claude API unavailable for writeup: {}", e)

    if not writeup:
        logger.info("Using template fallback writeup for prospect {}", prospect.id)
        writeup = _template_fallback_writeup(prospect)

    prospect.ai_writeup = writeup
    prospect.last_enriched_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("Writeup generated for prospect {} ({} chars)", prospect.id, len(writeup))
    return writeup


# ── Batch Orchestration ──────────────────────────────────────────────


async def run_signal_enrichment_batch(min_fit_score: int = 40) -> dict:
    """Run signal enrichment across all qualifying prospects.

    Step 1: Enrich missing signals for prospects without them (email-mined)
    Step 2: Find similar customers for ALL prospects that don't have them
    Step 3: Generate AI writeups for ALL prospects scoring >min_fit_score without writeups

    Returns summary: {signals_added, similar_computed, writeups_generated, errors}
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        summary = {
            "signals_added": 0,
            "similar_computed": 0,
            "writeups_generated": 0,
            "errors": 0,
        }

        # Step 1: Backfill missing signals
        prospects_needing_signals = (
            db.query(ProspectAccount)
            .filter(
                ProspectAccount.status == "suggested",
                ProspectAccount.fit_score >= min_fit_score,
            )
            .all()
        )

        for prospect in prospects_needing_signals:
            signals = prospect.readiness_signals or {}
            has_intent = bool(signals.get("intent"))
            has_hiring = bool(signals.get("hiring"))
            has_events = bool(signals.get("events"))

            if has_intent and (has_hiring or has_events):
                continue

            try:
                added = await enrich_missing_signals(prospect.id, db)
                if added:
                    summary["signals_added"] += 1
            except Exception as e:
                logger.error("Signal enrichment error for {}: {}", prospect.id, e)
                summary["errors"] += 1

        # Step 2: Find similar customers
        prospects_needing_similar = (
            db.query(ProspectAccount)
            .filter(
                ProspectAccount.status == "suggested",
                ProspectAccount.fit_score >= min_fit_score,
            )
            .all()
        )

        # Load the owned-company set ONCE for the whole loop (audit M9) instead of
        # re-scanning it per prospect.
        owned_companies = _load_owned_companies(db)
        for prospect in prospects_needing_similar:
            if prospect.similar_customers:
                continue
            try:
                find_similar_customers(prospect, db, owned_companies=owned_companies)
                summary["similar_computed"] += 1
            except Exception as e:
                logger.error("Similar customer error for {}: {}", prospect.id, e)
                summary["errors"] += 1

        # Step 3: Generate AI writeups
        prospects_needing_writeups = (
            db.query(ProspectAccount)
            .filter(
                ProspectAccount.status == "suggested",
                ProspectAccount.fit_score >= min_fit_score,
                ProspectAccount.ai_writeup.is_(None),
            )
            .all()
        )

        for prospect in prospects_needing_writeups:
            try:
                await generate_ai_writeup(prospect, db)
                summary["writeups_generated"] += 1
            except Exception as e:
                logger.error("Writeup generation error for {}: {}", prospect.id, e)
                summary["errors"] += 1

        logger.info(
            "Signal enrichment batch complete: {}",
            summary,
        )

        return summary

    finally:
        db.close()
