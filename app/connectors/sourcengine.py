"""Sourcengine API connector — B2B electronic component marketplace."""

import logging
import httpx
from .sources import BaseConnector

log = logging.getLogger(__name__)


class SourcengineConnector(BaseConnector):
    """Sourcengine REST API — search by MPN across suppliers."""

    # Docs: https://dev.sourcengine.com/
    SEARCH_URL = "https://api.sourcengine.com/v1/search"

    def __init__(self, api_key: str):
        super().__init__(timeout=15.0)
        self.api_key = api_key

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        params = {
            "q": part_number,
            "limit": 30,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(self.SEARCH_URL, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()

        return self._parse(data, part_number)

    def _parse(self, data: dict, pn: str) -> list[dict]:
        # Sourcengine response format — adapt to actual schema
        offers = data.get("offers", data.get("results", data.get("data", [])))
        if not isinstance(offers, list):
            offers = []

        results = []
        seen = set()

        for offer in offers:
            if not isinstance(offer, dict):
                continue

            supplier = offer.get("supplier", {})
            sup_name = (
                supplier.get("name", "")
                if isinstance(supplier, dict)
                else str(supplier)
            )
            if not sup_name:
                sup_name = offer.get("supplier_name", offer.get("company", ""))
            if not sup_name:
                continue

            mpn = offer.get("mpn", offer.get("part_number", pn))
            mfr = offer.get("manufacturer", offer.get("mfr", ""))
            if isinstance(mfr, dict):
                mfr = mfr.get("name", "")
            qty = offer.get("quantity", offer.get("stock", offer.get("qty")))
            price = offer.get("unit_price", offer.get("price"))
            currency = offer.get("currency", "USD")
            moq = offer.get("moq", offer.get("minimum_order_quantity"))
            url = offer.get("url", offer.get("buy_url", ""))
            sku = offer.get("sku", offer.get("supplier_pn", ""))

            key = f"{sup_name}_{mpn}_{sku}".lower()
            if key in seen:
                continue
            seen.add(key)

            results.append(
                {
                    "vendor_name": sup_name,
                    "manufacturer": mfr,
                    "mpn_matched": mpn,
                    "qty_available": _safe_int(qty),
                    "unit_price": _safe_float(price),
                    "currency": currency,
                    "source_type": "sourcengine",
                    "is_authorized": offer.get("authorized", False),
                    "confidence": 4 if qty else 3,
                    "click_url": url,
                    "vendor_sku": sku,
                    "moq": _safe_int(moq),
                }
            )

        log.info(f"Sourcengine: {pn} -> {len(results)} results")
        return results


def _safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
