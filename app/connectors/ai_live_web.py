"""AI live web search connector for sourcing sightings.

What it does:
- Uses Claude web search to find current supplier listings for a part number.
- Normalizes extracted listings into the standard sighting shape used by AVAIL.

What calls it:
- app/search_service.py (_fetch_fresh connector fan-out)

What it depends on:
- app.connectors.sources.BaseConnector
- app.utils.claude_client.claude_json (with web_search tool)
- app.utils.safe_float / safe_int
"""

import re

from loguru import logger

from ..utils import safe_float, safe_int
from ..utils.claude_client import claude_json
from .sources import BaseConnector

_MAX_LISTING_AGE_DAYS = 30
_STOCK_SIGNAL_WORDS = (
    "in stock",
    "stock available",
    "available now",
    "on hand",
    "qty",
    "quantity",
    "pieces",
)
_SEARCH_SCHEMA_HINT = {
    "offers": [
        {
            "vendor_name": "Supplier name",
            "mpn": "Manufacturer part number listing matches",
            "manufacturer": "Manufacturer name if known",
            "qty_available": "Integer quantity or null",
            "unit_price": "Numeric unit price or null",
            "currency": "ISO currency code like USD",
            "condition": "new / used / refurbished / null",
            "lead_time": "Lead time text if shown",
            "vendor_url": "Supplier/product page URL",
            "vendor_email": "Supplier email if shown",
            "vendor_phone": "Supplier phone if shown",
            "in_stock_explicit": "Boolean true only if page explicitly says in stock/available",
            "listing_age_days": "Integer age in days if visible/inferrable, else null",
            "evidence_note": "Short quote/snippet proving data",
        }
    ]
}


class AIWebSearchConnector(BaseConnector):
    """Live internet search for sourcing offers via Claude web search."""

    def __init__(self, api_key: str):
        super().__init__(timeout=30.0, max_retries=0)
        self.api_key = api_key

    @staticmethod
    def _normalize_vendor_url(raw_url: str) -> str | None:
        url = (raw_url or "").strip()
        if not url:
            return None
        if url.startswith("www."):
            url = f"https://{url}"
        if not (url.startswith("https://") or url.startswith("http://")):
            return None
        return url

    @staticmethod
    def _has_current_stock_signal(item: dict, evidence_note: str) -> bool:
        explicit = item.get("in_stock_explicit")
        if isinstance(explicit, bool):
            if explicit:
                return True
        evidence_l = (evidence_note or "").lower()
        return any(token in evidence_l for token in _STOCK_SIGNAL_WORDS)

    @staticmethod
    def _is_recent_listing(item: dict) -> bool:
        age = safe_int(item.get("listing_age_days"))
        if age is None:
            return True
        return 0 <= age <= _MAX_LISTING_AGE_DAYS

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        prompt = (
            f"Find current electronic component supplier offers for exact MPN: {part_number}.\n"
            "Prioritize distributor, broker, and marketplace listings with explicit current stock.\n"
            "Return strict JSON with key 'offers' (array). Use this shape exactly:\n"
            f"{_SEARCH_SCHEMA_HINT}\n\n"
            "Rules:\n"
            "- Include at most 8 best offers.\n"
            "- Do not invent data. Use null when unknown.\n"
            "- Keep numeric fields numeric (qty_available, unit_price).\n"
            "- MPN must be the listing's shown MPN.\n"
            "- Include only offers where listing shows current availability (not RFQ-only pages).\n"
            "- evidence_note must quote proof of stock and quantity from the page."
        )

        data = await claude_json(
            prompt,
            system=(
                "You are a sourcing extraction assistant for electronic components. "
                "Use web search, then return only valid JSON."
            ),
            model_tier="smart",
            max_tokens=1800,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            timeout=30,
        )

        offers = data.get("offers", []) if isinstance(data, dict) else []
        if not isinstance(offers, list):
            return []

        out: list[dict] = []
        dropped = 0
        for item in offers:
            if not isinstance(item, dict):
                dropped += 1
                continue
            vendor = (item.get("vendor_name") or "").strip()
            mpn = (item.get("mpn") or "").strip() or part_number
            if not vendor:
                dropped += 1
                continue

            qty = safe_int(item.get("qty_available"))
            price = safe_float(item.get("unit_price"))
            currency = (item.get("currency") or "USD").strip().upper()[:3] or "USD"
            lead_time = (item.get("lead_time") or "").strip() or None
            condition = (item.get("condition") or "").strip().lower() or None
            vendor_url = self._normalize_vendor_url(item.get("vendor_url") or "")
            evidence_note = (item.get("evidence_note") or "").strip()
            listing_age_days = safe_int(item.get("listing_age_days"))
            if condition not in {"new", "used", "refurbished"}:
                condition = None

            # Quality gate — keep only credible current stock postings.
            if qty is None or qty <= 0:
                dropped += 1
                continue
            if not vendor_url:
                dropped += 1
                continue
            if not evidence_note:
                dropped += 1
                continue
            if not self._has_current_stock_signal(item, evidence_note):
                dropped += 1
                continue
            if not self._is_recent_listing(item):
                dropped += 1
                continue

            # Extra sanity: evidence should include a stock/qty-like token.
            if not re.search(r"(in stock|available|qty|quantity|\d+\s*(pcs|pieces|units)?)", evidence_note, re.I):
                dropped += 1
                continue

            out.append(
                {
                    "vendor_name": vendor,
                    "vendor_email": (item.get("vendor_email") or "").strip() or None,
                    "vendor_phone": (item.get("vendor_phone") or "").strip() or None,
                    "manufacturer": (item.get("manufacturer") or "").strip() or None,
                    "mpn_matched": mpn,
                    "qty_available": qty,
                    "unit_price": round(price, 6) if price and price > 0 else None,
                    "currency": currency,
                    "source_type": "ai_live_web",
                    "is_authorized": False,
                    "confidence": 3 if item.get("in_stock_explicit") else 2,
                    "condition": condition,
                    "lead_time": lead_time,
                    "vendor_url": vendor_url,
                    "raw_data": {
                        "evidence_note": evidence_note,
                        "in_stock_explicit": item.get("in_stock_explicit"),
                        "listing_age_days": listing_age_days,
                    },
                }
            )

        logger.info("ai_live_web: {} -> {} offers kept ({} dropped by quality gate)", part_number, len(out), dropped)
        return out
