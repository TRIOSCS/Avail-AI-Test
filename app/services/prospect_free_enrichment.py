"""Free enrichment sources — SAM.gov + Google News RSS.

No API keys required. Enriches prospect accounts with:
- SAM.gov: Government contract data, CAGE codes, NAICS for defense/gov prospects
- Google News RSS: Recent headlines, expansion/funding/M&A signals

Called by: prospect_signals (batch enrichment), prospect_claim (on-claim enrichment)
Depends on: httpx (app.http_client), prospect_account model
"""

from datetime import datetime, timezone

from defusedxml.ElementTree import fromstring as _safe_xml_fromstring
from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.prospect_account import ProspectAccount

# ── Seniority Inference + Prospect Helpers ──────────────────────────────────

_DECISION_MAKER_KEYWORDS = (
    "vp",
    "vice president",
    "director",
    "chief",
    "ceo",
    "coo",
    "cfo",
    "cto",
    "cpo",
    "head of",
    "owner",
    "president",
)
_INFLUENCER_KEYWORDS = (
    "manager",
    "lead",
    "senior",
    "principal",
    "buyer",
    "sourcing",
    "procurement",
    "purchasing",
    "commodity",
)


def infer_seniority(title: str | None) -> str:
    """Bucket a job title into decision_maker | influencer | contributor (keyword
    match)."""
    t = (title or "").lower()
    if any(kw in t for kw in _DECISION_MAKER_KEYWORDS):
        return "decision_maker"
    if any(kw in t for kw in _INFLUENCER_KEYWORDS):
        return "influencer"
    return "contributor"


def _apply_company_to_prospect(prospect: ProspectAccount, company: dict | None) -> None:
    """Fill-only firmographic write from enrich_entity output (never clobbers
    existing)."""
    if not company:
        return
    if company.get("industry") and not prospect.industry:
        prospect.industry = company["industry"]
    if company.get("employee_size") and not prospect.employee_count_range:
        prospect.employee_count_range = company["employee_size"]
    if company.get("revenue_range") and not prospect.revenue_range:
        prospect.revenue_range = company["revenue_range"]
    if company.get("naics") and not prospect.naics_code:  # preserve SAM.gov naics
        prospect.naics_code = company["naics"]
    if not prospect.hq_location and (company.get("hq_city") or company.get("hq_state")):
        city, state = company.get("hq_city"), company.get("hq_state")
        prospect.hq_location = ", ".join(part for part in (city, state) if part)


def _apply_contacts_to_prospect(prospect: ProspectAccount, contacts: list[dict], limit: int) -> list[dict]:
    """Map provider contacts → canonical preview rows (dedup, cap), write
    contacts_preview."""
    mapped: list[dict] = []
    seen: set[str] = set()
    for c in contacts:
        name = c.get("full_name")
        if not name:
            continue
        key = (c.get("email") or "").lower() or name.lower()
        if key in seen:
            continue
        seen.add(key)
        mapped.append(
            {
                "name": name,
                "title": c.get("title"),
                "seniority": infer_seniority(c.get("title")),
                "email": c.get("email"),
                "verified": bool(c.get("verified")),
            }
        )
        if len(mapped) >= limit:
            break
    prospect.contacts_preview = mapped
    return mapped


# ── SAM.gov Enrichment ──────────────────────────────────────────────


async def enrich_from_sam_gov(prospect: ProspectAccount) -> dict | None:
    """Search SAM.gov for entity registration data.

    SAM.gov public API (no key required for basic entity search). Returns CAGE code,
    NAICS codes, entity status, gov contract eligibility.

    Returns None if no match or API error.
    """
    from app.http_client import http

    name = (prospect.name or "").strip()
    if not name:
        return None

    try:
        # SAM.gov Entity Management API (public, no auth)
        params = {
            "api_key": "DEMO_KEY",  # SAM.gov allows DEMO_KEY for low-rate access
            "legalBusinessName": name,
            "registrationStatus": "A",  # Active only
        }
        resp = await http.get(
            "https://api.sam.gov/entity-information/v3/entities",
            params=params,
            timeout=15,
        )

        if resp.status_code != 200:
            logger.debug("SAM.gov returned {}: {}", resp.status_code, resp.text[:200])
            return None

        data = resp.json()
        entities = data.get("entityData", [])
        if not entities:
            return None

        entity = entities[0]
        registration = entity.get("entityRegistration", {})
        core = entity.get("coreData", {})
        general = core.get("generalInformation", {})
        physical = core.get("physicalAddress", {})

        naics_list = []
        for n in core.get("naicsCodeList") or []:
            if isinstance(n, dict):
                naics_list.append(
                    {
                        "code": n.get("naicsCode"),
                        "description": n.get("naicsDescription", ""),
                        "primary": n.get("primaryNaicsCode", False),
                    }
                )

        return {
            "source": "sam_gov",
            "uei": registration.get("ueiSAM"),
            "cage_code": registration.get("cageCode"),
            "legal_name": registration.get("legalBusinessName"),
            "dba_name": registration.get("dbaName"),
            "entity_status": registration.get("registrationStatus"),
            "purpose": registration.get("purposeOfRegistrationDesc"),
            "naics_codes": naics_list,
            "entity_type": general.get("entityTypeDesc"),
            "organization_type": general.get("organizationTypeDesc"),
            "state": physical.get("stateOrProvinceCode"),
            "country": physical.get("countryCode"),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.warning("SAM.gov enrichment failed for '{}': {}", name, e)
        return None


# ── Google News RSS Enrichment ──────────────────────────────────────


async def enrich_from_google_news(prospect: ProspectAccount, max_items: int = 5) -> list[dict]:
    """Fetch recent news for a prospect via Google News RSS (no API key needed).

    Returns list of recent headlines with links.
    Useful signals: funding, expansion, acquisition, layoffs, new products.
    """
    from app.http_client import http

    name = (prospect.name or "").strip()
    if not name:
        return []

    try:
        # Google News RSS feed (public, no auth)
        query = name.replace(" ", "+")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

        resp = await http.get(url, timeout=10)
        if resp.status_code != 200:
            logger.debug("Google News RSS returned {} for '{}'", resp.status_code, name)
            return []

        root = _safe_xml_fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return []

        items = []
        for item in channel.findall("item")[:max_items]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub_date = item.findtext("pubDate", "")
            source = item.findtext("source", "")

            # Classify the signal type from the headline
            signal_type = _classify_headline(title)

            items.append(
                {
                    "title": title,
                    "link": link,
                    "pub_date": pub_date,
                    "source": source,
                    "signal_type": signal_type,
                }
            )

        return items

    except Exception as e:
        logger.warning("Google News enrichment failed for '{}': {}", name, e)
        return []


def _classify_headline(title: str) -> str:
    """Classify a news headline into a signal type.

    Returns: "funding", "expansion", "acquisition", "product", "hiring",
             "layoffs", "contract", "regulatory", or "general".
    """
    t = title.lower()

    if any(kw in t for kw in ["funding", "raises", "raised", "series", "investment", "ipo"]):
        return "funding"
    if any(kw in t for kw in ["acqui", "merger", "merges", "buys", "takeover"]):
        return "acquisition"
    if any(kw in t for kw in ["expan", "new facility", "new plant", "new office", "headquarter", "relocat"]):
        return "expansion"
    if any(kw in t for kw in ["launch", "new product", "unveil", "introduces", "release"]):
        return "product"
    if any(kw in t for kw in ["hiring", "hires", "recrui", "talent", "workforce"]):
        return "hiring"
    if any(kw in t for kw in ["layoff", "cuts", "downsiz", "restructur"]):
        return "layoffs"
    if any(kw in t for kw in ["contract", "award", "wins", "defense", "government", "dod", "pentagon"]):
        return "contract"
    if any(kw in t for kw in ["regulat", "compliance", "fda", "faa", "certif"]):
        return "regulatory"

    return "general"


# ── Combined Free Enrichment ───────────────────────────────────────


async def run_free_enrichment(prospect_id: int, db: Session | None = None) -> dict:
    """Run all free enrichment sources for a single prospect.

    Stores results in enrichment_data JSONB.
    Returns: {sam_gov: bool, news_count: int}
    """
    from app.database import SessionLocal

    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    assert db is not None  # narrowed: either passed in or just created
    try:
        prospect = db.get(ProspectAccount, prospect_id)
        if not prospect:
            return {"error": "not_found"}

        ed = dict(prospect.enrichment_data or {})
        result = {"sam_gov": False, "news_count": 0}

        # SAM.gov
        if not ed.get("sam_gov"):
            sam_data = await enrich_from_sam_gov(prospect)
            if sam_data:
                ed["sam_gov"] = sam_data
                result["sam_gov"] = True

                # Update NAICS code if we found one and prospect doesn't have it
                if not prospect.naics_code and sam_data.get("naics_codes"):
                    naics_codes = sam_data["naics_codes"]
                    primary = next(
                        (n for n in naics_codes if n.get("primary")),
                        naics_codes[0],
                    )
                    prospect.naics_code = primary["code"]

        # Google News
        news = await enrich_from_google_news(prospect)
        if news:
            ed["recent_news"] = news
            ed["news_retrieved_at"] = datetime.now(timezone.utc).isoformat()
            result["news_count"] = len(news)

            # Extract signal events from news
            signal_events = []
            for item in news:
                if item["signal_type"] != "general":
                    signal_events.append(
                        {
                            "type": item["signal_type"],
                            "description": item["title"][:120],
                            "date": item["pub_date"],
                            "source": "google_news",
                        }
                    )

            # Merge news-derived events into readiness_signals
            if signal_events:
                signals = dict(prospect.readiness_signals or {})
                existing_events = signals.get("events", [])
                # Add news events without duplicating
                existing_types = {e.get("description", "")[:50] for e in existing_events if isinstance(e, dict)}
                for ev in signal_events:
                    if ev["description"][:50] not in existing_types:
                        existing_events.append(ev)
                signals["events"] = existing_events[:10]  # cap at 10
                prospect.readiness_signals = signals

        prospect.enrichment_data = ed
        flag_modified(prospect, "enrichment_data")
        if prospect.readiness_signals is not None:
            flag_modified(prospect, "readiness_signals")
        prospect.last_enriched_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "Free enrichment for prospect {}: sam={}, news={}",
            prospect_id,
            result["sam_gov"],
            result["news_count"],
        )
        return result

    except Exception as e:
        logger.error("Free enrichment failed for prospect {}: {}", prospect_id, e)
        return {"error": str(e)}
    finally:
        if owns_session:
            db.close()


async def run_enrichment_job(prospect_id: int, db: Session | None = None) -> None:
    """Full background enrichment pass for one prospect, recording enrich_status.

    Runs free enrichment (SAM.gov + news) then warm-intro detection, and stamps
    ``enrichment_data['enrich_status']`` = ``'done'`` (or ``'error'``). Fire-and-forget
    safe: opens its own session when ``db`` is None and never raises — the prospecting
    tab spawns it via ``safe_background_task`` and polls the status endpoint.
    """
    from app.database import SessionLocal
    from app.services.prospect_scoring import calculate_fit_score, calculate_readiness_score
    from app.services.prospect_warm_intros import detect_warm_intros, generate_one_liner

    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    assert db is not None
    try:
        result = await run_free_enrichment(prospect_id, db=db)
        status = "error" if isinstance(result, dict) and result.get("error") else "done"

        prospect = db.get(ProspectAccount, prospect_id)
        if prospect is None:
            return

        # ── Paid enrichment (Lusha chain) — 24h skip gate ──
        from app.config import settings as _settings
        from app.enrichment_service import enrich_entity, find_suggested_contacts

        ed = dict(prospect.enrichment_data or {})
        last = ed.get("contacts_enriched_at")
        recently = False
        if last:
            try:
                recently = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 86400
            except (TypeError, ValueError):
                recently = False

        if not recently:
            try:
                company = await enrich_entity(prospect.domain, prospect.name or "")
                contacts = await find_suggested_contacts(
                    prospect.domain,
                    prospect.name or "",
                    limit=_settings.prospect_enrich_contacts_per_account,
                )
                _apply_company_to_prospect(prospect, company)
                mapped = _apply_contacts_to_prospect(prospect, contacts, _settings.prospect_enrich_contacts_per_account)

                signals = dict(prospect.readiness_signals or {})
                signals["contacts_verified_count"] = sum(1 for c in mapped if c["verified"])
                signals["contacts_unverified_count"] = sum(1 for c in mapped if not c["verified"])
                prospect.readiness_signals = signals

                ed["contact_provider"] = (company or {}).get("source") or "lusha"
                ed["contacts_enriched_at"] = datetime.now(timezone.utc).isoformat()
                prospect.enrichment_data = ed
                flag_modified(prospect, "contacts_preview")
                flag_modified(prospect, "readiness_signals")
                flag_modified(prospect, "enrichment_data")
            except Exception as exc:  # noqa: BLE001 — paid step is best-effort; free data already saved
                logger.warning("Paid enrichment step failed for prospect {}: {}", prospect_id, exc)

        try:
            warm = detect_warm_intros(prospect, db)
            one_liner = generate_one_liner(prospect, warm)
        except Exception as exc:  # noqa: BLE001 — warm-intro is best-effort; free enrichment may have succeeded
            logger.warning("Warm-intro step failed for prospect {}: {}", prospect_id, exc)
            warm, one_liner = {}, ""

        # Recompute readiness from the now news-augmented signals so enrichment actually
        # moves the readiness tier + buyer-ready ranking — not just the displayed panels.
        new_readiness, _ = calculate_readiness_score({"name": prospect.name}, prospect.readiness_signals or {})
        prospect.readiness_score = new_readiness

        new_fit, fit_reasoning = calculate_fit_score(
            {
                "industry": prospect.industry,
                "naics_code": prospect.naics_code,
                "employee_count_range": prospect.employee_count_range,
                "region": prospect.region,
                "has_procurement_staff": None,
                "uses_brokers": None,
            }
        )
        prospect.fit_score = new_fit
        prospect.fit_reasoning = fit_reasoning

        ed = dict(prospect.enrichment_data or {})
        ed["warm_intro"] = warm
        ed["one_liner"] = one_liner
        ed["enrich_status"] = status
        prospect.enrichment_data = ed
        flag_modified(prospect, "enrichment_data")
        db.commit()

        # ── SP3: AI screen — final step, fire-and-forget ──
        try:
            from app.services.prospect_screening import screen_prospect

            await screen_prospect(prospect, db)
        except Exception as _screen_exc:  # noqa: BLE001 — screen must not affect enrich_status
            logger.warning("Screen step failed for prospect {}: {}", prospect_id, _screen_exc)
    except Exception as exc:  # noqa: BLE001 — fire-and-forget must never propagate
        logger.warning("Enrichment job failed for prospect {}: {}", prospect_id, exc)
        db.rollback()
        try:
            prospect = db.get(ProspectAccount, prospect_id)
            if prospect is not None:
                ed = dict(prospect.enrichment_data or {})
                ed["enrich_status"] = "error"
                prospect.enrichment_data = ed
                flag_modified(prospect, "enrichment_data")
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    finally:
        if owns_session:
            db.close()


async def run_free_enrichment_batch(min_fit_score: int = 40) -> dict:
    """Run free enrichment across qualifying prospects.

    Skips prospects already enriched with SAM.gov/news data. Returns batch summary.
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        prospects = (
            db.query(ProspectAccount.id)
            .filter(
                ProspectAccount.status == "suggested",
                ProspectAccount.fit_score >= min_fit_score,
            )
            .order_by(ProspectAccount.fit_score.desc())
            .limit(50)  # batch limit
            .all()
        )

        summary = {"processed": 0, "sam_hits": 0, "news_hits": 0, "errors": 0}

        for (prospect_id,) in prospects:
            try:
                result = await run_free_enrichment(prospect_id, db=db)
                if result.get("error"):
                    summary["errors"] += 1
                else:
                    summary["processed"] += 1
                    if result.get("sam_gov"):
                        summary["sam_hits"] += 1
                    if result.get("news_count", 0) > 0:
                        summary["news_hits"] += 1
            except Exception as e:
                logger.error("Batch enrichment error for {}: {}", prospect_id, e)
                summary["errors"] += 1

        logger.info("Free enrichment batch complete: {}", summary)
        return summary

    finally:
        db.close()
