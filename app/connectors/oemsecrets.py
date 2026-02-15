"""OEMSecrets API connector — meta-aggregator across 140+ distributors.

Returns pricing/stock from DigiKey, Mouser, Arrow, Avnet, Farnell, RS,
Future, TME, and many more in a single API call.
"""
import logging, httpx
from .sources import BaseConnector

log = logging.getLogger(__name__)


class OEMSecretsConnector(BaseConnector):
    """OEMSecrets Part Search API — JSON endpoint."""
    # Docs: https://www.oemsecrets.com/api
    SEARCH_URL = "https://www.oemsecrets.com/api/v1/search"

    def __init__(self, api_key: str):
        super().__init__(timeout=20.0)
        self.api_key = api_key

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        params = {
            "apiKey": self.api_key,
            "searchTerm": part_number,
            "currency": "USD",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(self.SEARCH_URL, params=params)
            r.raise_for_status()
            data = r.json()

        return self._parse(data, part_number)

    def _parse(self, data: dict, pn: str) -> list[dict]:
        # OEMSecrets response varies — handle both formats
        stock_data = data if isinstance(data, list) else data.get("stock", data.get("results", []))
        if not isinstance(stock_data, list):
            stock_data = []

        results = []
        seen = set()

        for item in stock_data:
            if not isinstance(item, dict):
                continue

            distributor = item.get("distributor", {})
            dist_name = distributor.get("name", "") if isinstance(distributor, dict) else str(distributor)
            if not dist_name:
                dist_name = item.get("distributor_name", item.get("seller", ""))
            if not dist_name:
                continue

            mpn = item.get("mpn", item.get("part_number", pn))
            mfr = item.get("manufacturer", "")
            qty = item.get("stock", item.get("quantity", item.get("qty")))
            price = item.get("price", item.get("unit_price"))
            currency = item.get("currency", "USD")
            url = item.get("url", item.get("buy_url", ""))
            moq = item.get("moq", item.get("minimum_order"))
            sku = item.get("sku", item.get("distributor_pn", ""))
            datasheet = item.get("datasheet_url", "")

            key = f"{dist_name}_{mpn}_{sku}".lower()
            if key in seen:
                continue
            seen.add(key)

            # Authorized distributors from OEMSecrets are generally legit
            is_auth = item.get("authorized", item.get("is_authorized", True))

            results.append({
                "vendor_name": dist_name,
                "manufacturer": mfr,
                "mpn_matched": mpn,
                "qty_available": _safe_int(qty),
                "unit_price": _safe_float(price),
                "currency": currency,
                "source_type": "oemsecrets",
                "is_authorized": bool(is_auth),
                "confidence": 5 if qty else 3,
                "click_url": url,
                "vendor_sku": sku,
                "moq": _safe_int(moq),
                "datasheet_url": datasheet,
            })

        log.info(f"OEMSecrets: {pn} -> {len(results)} results")
        return results


def _safe_int(v):
    if v is None: return None
    try: return int(v)
    except: return None

def _safe_float(v):
    if v is None: return None
    try: return float(v)
    except: return None
