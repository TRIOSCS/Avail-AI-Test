"""Phase 4 — Signal gap-fill, similar customer matching, and AI writeups.

Enriches prospect accounts with:
- Missing intent/hiring/event signals (backfill for email-mined prospects)
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
    calculate_readiness_score,
)

# ── Signal Enrichment ────────────────────────────────────────────────


def enrich_with_intent(prospect_id: int, intent_data: dict, db: Session) -> None:
    """Store intent topic data in readiness_signals JSONB under 'intent' key.

    Relevant topics: electronic components, integrated circuits, semiconductors,
    procurement solutions, supply chain management, component sourcing.
    Recalculates readiness_score after update.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        logger.warning("enrich_with_intent: prospect {} not found", prospect_id)
        return

    signals = dict(prospect.readiness_signals or {})
    signals["intent"] = intent_data
    signals["enriched_at"] = datetime.now(timezone.utc).isoformat()
    signals["source"] = signals.get("source", "backfill")
    prospect.readiness_signals = signals

    _recalculate_readiness(prospect)
    prospect.last_enriched_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Intent signals stored for prospect {}", prospect_id)


def enrich_with_hiring(prospect_id: int, workforce_data: dict, db: Session) -> None:
    """Store workforce trend data in readiness_signals JSONB under 'hiring' key.

    Looks for procurement/engineering department growth, active hiring.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        logger.warning("enrich_with_hiring: prospect {} not found", prospect_id)
        return

    signals = dict(prospect.readiness_signals or {})
    signals["hiring"] = workforce_data
    signals["enriched_at"] = datetime.now(timezone.utc).isoformat()
    signals["source"] = signals.get("source", "backfill")
    prospect.readiness_signals = signals

    _recalculate_readiness(prospect)
    prospect.last_enriched_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Hiring signals stored for prospect {}", prospect_id)


def enrich_with_events(prospect_id: int, events: list[dict], db: Session) -> None:
    """Store company events in readiness_signals JSONB under 'events' key.

    Event types: new_funding_round, new_product, new_office, M&A.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        logger.warning("enrich_with_events: prospect {} not found", prospect_id)
        return

    signals = dict(prospect.readiness_signals or {})
    signals["events"] = events
    signals["enriched_at"] = datetime.now(timezone.utc).isoformat()
    signals["source"] = signals.get("source", "backfill")
    prospect.readiness_signals = signals

    _recalculate_readiness(prospect)
    prospect.last_enriched_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Event signals stored for prospect {}", prospect_id)


def _recalculate_readiness(prospect: ProspectAccount) -> None:
    """Recalculate readiness_score from current readiness_signals."""
    signals = prospect.readiness_signals or {}
    prospect_data = {"name": prospect.name}
    score, _ = calculate_readiness_score(prospect_data, signals)
    prospect.readiness_score = score


# ── Missing Signal Backfill ──────────────────────────────────────────


async def enrich_missing_signals(prospect_id: int, db: Session) -> bool:
    """Check if prospect has signal data; if not, call Explorium to backfill.

    Primarily serves email-mined and manually-added prospects.
    Explorium-discovered prospects will already have signals and be skipped.

    Returns True if new signals were added, False if already complete.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        logger.warning("enrich_missing_signals: prospect {} not found", prospect_id)
        return False

    signals = prospect.readiness_signals or {}

    # Check if signals are already populated (Explorium-discovered prospects)
    has_intent = bool(signals.get("intent"))
    has_hiring = bool(signals.get("hiring"))
    has_events = bool(signals.get("events"))

    if has_intent and (has_hiring or has_events):
        logger.debug(
            "Prospect {} already has signals, skipping backfill", prospect_id
        )
        return False

    # Lazy import to avoid circular dependency
    from app.http_client import http
    from app.services.prospect_discovery_explorium import (
        EXPLORIUM_BASE,
        SHARED_INTENT_TOPICS,
        _get_api_key,
    )

    api_key = _get_api_key()
    if not api_key:
        logger.warning("Explorium API key not configured — skipping signal backfill")
        return False

    domain = prospect.domain
    if not domain:
        return False

    try:
        resp = await http.post(
            f"{EXPLORIUM_BASE}/v1/businesses/search",
            json={
                "domain": domain,
                "business_intent_topics": SHARED_INTENT_TOPICS,
                "limit": 1,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout=30,
        )

        if resp.status_code != 200:
            logger.warning(
                "Explorium signal backfill failed for {}: {} {}",
                domain, resp.status_code, resp.text[:200],
            )
            return False

        data = resp.json()
        businesses = data.get("businesses", data.get("results", []))
        if not businesses:
            logger.debug("No Explorium data for domain {}", domain)
            return False

        raw = businesses[0] if isinstance(businesses, list) else {}

        new_signals = dict(signals)
        added = False

        # Intent
        if not has_intent:
            intent_topics = raw.get("business_intent_topics") or raw.get("intent_topics") or []
            if isinstance(intent_topics, list) and intent_topics:
                component_topics = [
                    t for t in intent_topics
                    if any(kw in t.lower() for kw in [
                        "electronic", "component", "semiconductor", "circuit",
                        "procurement", "sourcing",
                    ])
                ]
                strength = (
                    "strong" if len(component_topics) >= 3
                    else "moderate" if len(component_topics) >= 1
                    else "weak"
                )
                new_signals["intent"] = {
                    "strength": strength,
                    "topics": intent_topics,
                    "component_topics": component_topics,
                }
                added = True

        # Hiring
        if not has_hiring:
            workforce = raw.get("workforce_trends") or raw.get("department_growth") or {}
            if isinstance(workforce, dict):
                procurement_growth = workforce.get("procurement") or workforce.get("purchasing")
                engineering_growth = workforce.get("engineering") or workforce.get("r_and_d")
                if procurement_growth:
                    new_signals["hiring"] = {"type": "procurement", "detail": procurement_growth}
                    added = True
                elif engineering_growth:
                    new_signals["hiring"] = {"type": "engineering", "detail": engineering_growth}
                    added = True

        # Events
        if not has_events:
            events_raw = raw.get("recent_events") or raw.get("events") or []
            if isinstance(events_raw, list) and events_raw:
                parsed_events = []
                for ev in events_raw:
                    if isinstance(ev, dict):
                        parsed_events.append({
                            "type": ev.get("type") or ev.get("event_type", "unknown"),
                            "date": ev.get("date") or ev.get("event_date"),
                            "description": ev.get("description") or ev.get("title"),
                        })
                    elif isinstance(ev, str):
                        parsed_events.append({"type": ev, "date": None, "description": ev})
                if parsed_events:
                    new_signals["events"] = parsed_events
                    added = True

        if added:
            new_signals["enriched_at"] = datetime.now(timezone.utc).isoformat()
            new_signals["source"] = "backfill"
            prospect.readiness_signals = new_signals
            _recalculate_readiness(prospect)
            prospect.last_enriched_at = datetime.now(timezone.utc)
            db.commit()
            logger.info("Backfilled signals for prospect {} ({})", prospect_id, domain)

        return added

    except Exception as e:
        logger.error("Signal backfill error for prospect {}: {}", prospect_id, e)
        return False


# ── Similar Customer Matching ────────────────────────────────────────


def find_similar_customers(prospect: ProspectAccount, db: Session) -> list[dict]:
    """Compare prospect against existing owned Company records.

    Matching logic:
      - Same NAICS 4-digit prefix = strong match
      - Same industry keyword + similar size = moderate match
      - Same region + similar size = weak match

    Returns top 3 most similar existing Trio customers.
    Stores results in prospect.similar_customers JSONB.
    """
    # Get owned companies (have an account_owner_id)
    companies = (
        db.query(Company)
        .filter(
            Company.account_owner_id.isnot(None),
            Company.is_active.is_(True),
        )
        .all()
    )

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
                    if company.industry and any(
                        kw in company.industry.lower() for kw in seg["keywords"]
                    ):
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
        size_similar = _compare_sizes(
            prospect.employee_count_range, company.employee_size
        )
        if size_similar:
            match_reasons.append("Similar company size")
            score += 10

        # Region match
        if prospect.region and company.hq_country:
            prospect_region = prospect.region.upper()
            country = company.hq_country.upper()
            region_match = (
                (prospect_region == "US" and country in ("US", "USA", "UNITED STATES"))
                or (prospect_region == "EU" and country in (
                    "DE", "GB", "FR", "NL", "SE", "IT", "ES", "CH", "AT", "BE",
                    "GERMANY", "UNITED KINGDOM", "FRANCE", "NETHERLANDS",
                ))
                or (prospect_region == "ASIA" and country in (
                    "CN", "JP", "KR", "TW", "SG", "IN", "CHINA", "JAPAN",
                    "SOUTH KOREA", "TAIWAN", "SINGAPORE", "INDIA",
                ))
            )
            if region_match:
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

            matches.append({
                "name": company.name,
                "domain": company.domain,
                "match_reason": "; ".join(match_reasons),
                "match_strength": strength,
                "score": score,
            })

    # Sort by score descending, take top 3
    matches.sort(key=lambda m: m["score"], reverse=True)
    top_matches = matches[:3]

    # Remove internal score from output
    result = [
        {k: v for k, v in m.items() if k != "score"} for m in top_matches
    ]

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

    parts.append(
        "\nWrite a 2-3 sentence sales-ready summary of this prospect."
    )

    return "\n".join(parts)


def _template_fallback_writeup(prospect: ProspectAccount) -> str:
    """Generate a template-based writeup when Claude API is unavailable."""
    signals = prospect.readiness_signals or {}
    similar = prospect.similar_customers or []

    # Size description
    size = prospect.employee_count_range or "unknown-size"

    # Industry
    industry = prospect.industry or "unknown industry"

    # Location
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

    Falls back to template-based writeup if Claude API is unavailable.
    Stores result in prospect.ai_writeup field.
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
        logger.info(
            "Using template fallback writeup for prospect {}", prospect.id
        )
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

        for prospect in prospects_needing_similar:
            if prospect.similar_customers:
                continue
            try:
                find_similar_customers(prospect, db)
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
