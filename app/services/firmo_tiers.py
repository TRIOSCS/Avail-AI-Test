"""Per-field source-authority ladder for company/contact enrichment blending.

Ports the materials F1 tier mechanic (app/services/spec_tiers.py) to firmographics
and contacts: for each field the value from the highest-authority source wins, ties
broken by confidence. Unknown source → tier 0 (loses every conflict).

Called by: app/services/enrichment_router.py, app/enrichment_service.py (blend + apply).
Depends on: nothing (pure logic).
"""

from loguru import logger

FIRMO_BASE_TIER: dict[str, int] = {
    "manual": 100,
    "explorium": 85,
    "lusha": 75,
    "clay": 70,
    "sam_gov": 60,
    "hunter": 40,
    "ai": 30,
}
FIRMO_FIELD_TIER: dict[str, dict[str, int]] = {
    "legal_name": {"sam_gov": 95, "explorium": 85, "lusha": 75, "clay": 70, "ai": 30},
    "naics": {"sam_gov": 95, "explorium": 85, "ai": 30},
    "ticker": {"explorium": 90, "clay": 75, "ai": 30},
    # Clay omitted: annual_revenue is not a base firmographic and enrich_company does
    # not request the paid Annual Revenue data point, so Clay never supplies this field.
    "revenue_range": {"explorium": 90, "ai": 30},
    "employee_size": {"explorium": 85, "lusha": 70, "clay": 70, "ai": 30},
    "industry": {"explorium": 85, "clay": 70, "lusha": 65, "ai": 30},
    "hq_city": {"explorium": 85, "sam_gov": 80, "lusha": 60, "clay": 60, "ai": 30},
    "hq_state": {"explorium": 85, "sam_gov": 80, "lusha": 60, "clay": 60, "ai": 30},
    "hq_country": {"explorium": 85, "sam_gov": 80, "lusha": 60, "clay": 60, "ai": 30},
    "website": {"explorium": 80, "clay": 70, "lusha": 60, "ai": 30},
    "domain": {"explorium": 80, "clay": 70, "lusha": 60, "ai": 30},
    "linkedin_url": {"explorium": 85, "lusha": 80, "clay": 60, "ai": 30},
}
CONTACT_BASE_TIER: dict[str, int] = {
    "lusha": 80,
    "explorium": 70,
    "clay": 65,
    "hunter": 50,
    "ai": 30,
}
CONTACT_FIELD_TIER: dict[str, dict[str, int]] = {
    "phone": {"lusha": 95, "explorium": 65, "hunter": 50, "ai": 30},
    "email": {"lusha": 95, "hunter": 85, "explorium": 65, "ai": 30},
    "title": {"explorium": 80, "lusha": 70, "clay": 65, "hunter": 50, "ai": 30},
    "full_name": {"lusha": 80, "explorium": 70, "clay": 65, "hunter": 50, "ai": 30},
    "linkedin_url": {"lusha": 80, "explorium": 70, "clay": 65, "hunter": 50, "ai": 30},
}
_warned: set[str] = set()


def firmo_tier(field: str, source: str) -> int:
    t = FIRMO_FIELD_TIER.get(field, {}).get(source)
    if t is None:
        t = FIRMO_BASE_TIER.get(source, 0)
    if t == 0 and source not in _warned:
        _warned.add(source)
        logger.warning("firmo_tier: unknown source {!r} → tier 0 (loses every conflict)", source)
    return t


def contact_tier(field: str, source: str) -> int:
    t = CONTACT_FIELD_TIER.get(field, {}).get(source)
    if t is None:
        t = CONTACT_BASE_TIER.get(source, 0)
    if t == 0 and source not in _warned:
        _warned.add(source)
        logger.warning("contact_tier: unknown source {!r} → tier 0 (loses every conflict)", source)
    return t


_CONTACT_FIELDS = ("full_name", "email", "phone", "title", "linkedin_url", "location", "company")


def blend_company(results: list[dict]) -> dict:
    blended: dict = {}
    prov: dict = {}
    sources: list[str] = []
    for r in results:
        if not r:
            continue
        src = r.get("source") or "unknown"
        if src not in sources:
            sources.append(src)
        conf_map = r.get("_confidence") if isinstance(r.get("_confidence"), dict) else {}
        for field, value in r.items():
            if field in ("source", "_provenance", "_confidence") or not value:
                continue
            tier = firmo_tier(field, src)
            conf = float(conf_map.get(field, 1.0))
            cur = prov.get(field)
            if cur is None or (tier, conf) > (cur["tier"], cur["confidence"]):
                blended[field] = value
                prov[field] = {"source": src, "tier": tier, "confidence": conf}
    if blended:
        blended["source"] = "+".join(sources)
        blended["_provenance"] = prov
    return blended


def _contact_key(c: dict) -> str:
    return (c.get("email") or "").strip().lower() or c.get("linkedin_url") or (c.get("full_name") or "").strip().lower()


def blend_contacts(results: list[dict]) -> list[dict]:
    """Dedup by email→linkedin→name; per field keep the highest contact_tier value (a
    verified email/phone gets a confidence bump so it beats an unverified peer)."""
    merged: dict[str, dict] = {}
    field_prov: dict[str, dict] = {}
    contact_sources: dict[str, list[str]] = {}
    for c in results:
        if not c:
            continue
        key = _contact_key(c)
        if not key:
            continue
        src = c.get("source") or "unknown"
        verified = bool(c.get("verified"))
        if key not in merged:
            merged[key] = {"verified": verified}
            field_prov[key] = {}
            contact_sources[key] = [src]
        else:
            if src not in contact_sources[key]:
                contact_sources[key].append(src)
        row, fp = merged[key], field_prov[key]
        row["verified"] = row.get("verified") or verified
        for field in _CONTACT_FIELDS:
            value = c.get(field)
            if not value:
                continue
            conf = 0.9 if (field in ("email", "phone") and verified) else 0.5
            tier = contact_tier(field, src)
            cur = fp.get(field)
            if cur is None or (tier, conf) > cur:
                row[field] = value
                fp[field] = (tier, conf)
    for key, row in merged.items():
        row["source"] = "+".join(contact_sources[key])
    return list(merged.values())
