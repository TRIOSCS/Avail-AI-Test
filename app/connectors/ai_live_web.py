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

from loguru import logger

from ..utils import safe_float, safe_int
from ..utils.claude_client import claude_json
from .sources import BaseConnector

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
            "evidence_note": "Short quote/snippet proving data",
        }
    ]
}


class AIWebSearchConnector(BaseConnector):
    """Live internet search for sourcing offers via Claude web search."""

    def __init__(self, api_key: str):
        super().__init__(timeout=30.0, max_retries=0)
        self.api_key = api_key

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        prompt = (
            f"Find current electronic component supplier offers for exact MPN: {part_number}.\n"
            "Prioritize distributor, broker, and marketplace listings that show stock and/or price.\n"
            "Return strict JSON with key 'offers' (array). Use this shape exactly:\n"
            f"{_SEARCH_SCHEMA_HINT}\n\n"
            "Rules:\n"
            "- Include at most 8 best offers.\n"
            "- Do not invent data. Use null when unknown.\n"
            "- Keep numeric fields numeric (qty_available, unit_price).\n"
            "- Include vendor_url when available.\n"
            "- MPN must be the listing's shown MPN."
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
            timeout=60,
        )

        offers = data.get("offers", []) if isinstance(data, dict) else []
        if not isinstance(offers, list):
            return []

        out: list[dict] = []
        for item in offers:
            if not isinstance(item, dict):
                continue
            vendor = (item.get("vendor_name") or "").strip()
            mpn = (item.get("mpn") or "").strip() or part_number
            if not vendor:
                continue

            qty = safe_int(item.get("qty_available"))
            price = safe_float(item.get("unit_price"))
            currency = (item.get("currency") or "USD").strip().upper()[:3] or "USD"
            lead_time = (item.get("lead_time") or "").strip() or None
            condition = (item.get("condition") or "").strip().lower() or None
            if condition not in {"new", "used", "refurbished"}:
                condition = None

            out.append(
                {
                    "vendor_name": vendor,
                    "vendor_email": (item.get("vendor_email") or "").strip() or None,
                    "vendor_phone": (item.get("vendor_phone") or "").strip() or None,
                    "manufacturer": (item.get("manufacturer") or "").strip() or None,
                    "mpn_matched": mpn,
                    "qty_available": qty if qty and qty > 0 else None,
                    "unit_price": round(price, 6) if price and price > 0 else None,
                    "currency": currency,
                    "source_type": "ai_live_web",
                    "is_authorized": False,
                    "confidence": 2,  # Lower confidence than direct APIs
                    "condition": condition,
                    "lead_time": lead_time,
                    "vendor_url": (item.get("vendor_url") or "").strip() or None,
                    "raw_data": {"evidence_note": item.get("evidence_note")},
                }
            )

        logger.info("ai_live_web: {} -> {} offers", part_number, len(out))
        return out
