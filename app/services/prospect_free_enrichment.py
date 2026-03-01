"""Free enrichment sources — SAM.gov + Google News RSS.

No API keys required. Enriches prospect accounts with:
- SAM.gov: Government contract data, CAGE codes, NAICS for defense/gov prospects
- Google News RSS: Recent headlines, expansion/funding/M&A signals

Called by: prospect_signals (batch enrichment), prospect_claim (on-claim enrichment)
Depends on: httpx (app.http_client), prospect_account model
"""

from datetime import datetime, timezone
from xml.etree import ElementTree

from loguru import logger
from sqlalchemy.orm.attributes import flag_modified

from app.models.prospect_account import ProspectAccount

# ── SAM.gov Enrichment ──────────────────────────────────────────────


async def enrich_from_sam_gov(prospect: ProspectAccount) -> dict | None:
    """Search SAM.gov for entity registration data.

    SAM.gov public API (no key required for basic entity search).
    Returns CAGE code, NAICS codes, entity status, gov contract eligibility.

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
        for n in (core.get("naicsCodeList") or []):
            if isinstance(n, dict):
                naics_list.append({
                    "code": n.get("naicsCode"),
                    "description": n.get("naicsDescription", ""),
                    "primary": n.get("primaryNaicsCode", False),
                })

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
        logger.debug("SAM.gov enrichment failed for '{}': {}", name, e)
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

        root = ElementTree.fromstring(resp.content)
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

            items.append({
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "source": source,
                "signal_type": signal_type,
            })

        return items

    except Exception as e:
        logger.debug("Google News enrichment failed for '{}': {}", name, e)
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


async def run_free_enrichment(prospect_id: int) -> dict:
    """Run all free enrichment sources for a single prospect.

    Stores results in enrichment_data JSONB.
    Returns: {sam_gov: bool, news_count: int}
    """
    from app.database import SessionLocal

    db = SessionLocal()
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
                    primary = next(
                        (n for n in sam_data["naics_codes"] if n.get("primary")),
                        sam_data["naics_codes"][0] if sam_data["naics_codes"] else None,
                    )
                    if primary:
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
                    signal_events.append({
                        "type": item["signal_type"],
                        "description": item["title"][:120],
                        "date": item["pub_date"],
                        "source": "google_news",
                    })

            # Merge news-derived events into readiness_signals
            if signal_events:
                signals = dict(prospect.readiness_signals or {})
                existing_events = signals.get("events", [])
                # Add news events without duplicating
                existing_types = {
                    e.get("description", "")[:50]
                    for e in existing_events
                    if isinstance(e, dict)
                }
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
            prospect_id, result["sam_gov"], result["news_count"],
        )
        return result

    except Exception as e:
        logger.error("Free enrichment failed for prospect {}: {}", prospect_id, e)
        return {"error": str(e)}
    finally:
        db.close()


async def run_free_enrichment_batch(min_fit_score: int = 40) -> dict:
    """Run free enrichment across qualifying prospects.

    Skips prospects already enriched with SAM.gov/news data.
    Returns batch summary.
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
                result = await run_free_enrichment(prospect_id)
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
