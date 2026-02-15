"""DigiKey Product Search API connector."""
import logging, httpx
from .sources import BaseConnector

log = logging.getLogger(__name__)


class DigiKeyConnector(BaseConnector):
    """DigiKey Product Search v4 — OAuth2 client credentials."""
    # DigiKey uses a two-legged OAuth for product data (client_credentials)
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

    def __init__(self, client_id: str, client_secret: str):
        super().__init__(timeout=15.0)
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(self.TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            })
            r.raise_for_status()
            self._token = r.json()["access_token"]
            return self._token

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.client_id:
            return []
        token = await self._get_token()

        payload = {
            "Keywords": part_number,
            "RecordCount": 25,
            "RecordStartPosition": 0,
            "ExcludeMarketPlaceProducts": False,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(self.SEARCH_URL, headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": self.client_id,
                "Content-Type": "application/json",
                "X-DIGIKEY-Locale-Site": "US",
                "X-DIGIKEY-Locale-Language": "en",
                "X-DIGIKEY-Locale-Currency": "USD",
            }, json=payload)

            if r.status_code == 401:
                self._token = None
                token = await self._get_token()
                r = await c.post(self.SEARCH_URL, headers={
                    "Authorization": f"Bearer {token}",
                    "X-DIGIKEY-Client-Id": self.client_id,
                    "Content-Type": "application/json",
                    "X-DIGIKEY-Locale-Site": "US",
                    "X-DIGIKEY-Locale-Language": "en",
                    "X-DIGIKEY-Locale-Currency": "USD",
                }, json=payload)

            r.raise_for_status()
            data = r.json()

        return self._parse(data, part_number)

    def _parse(self, data: dict, pn: str) -> list[dict]:
        products = data.get("Products") or data.get("products") or []
        results = []

        for prod in products:
            mpn = prod.get("ManufacturerPartNumber") or prod.get("manufacturerPartNumber") or pn
            mfr = (prod.get("Manufacturer") or prod.get("manufacturer") or {}).get("Name", "")
            dk_pn = prod.get("DigiKeyPartNumber") or prod.get("digiKeyPartNumber") or ""
            qty = prod.get("QuantityAvailable") or prod.get("quantityAvailable")
            desc = prod.get("Description") or prod.get("description") or {
            }
            detail_desc = desc.get("DetailedDescription", "") if isinstance(desc, dict) else str(desc)
            url = prod.get("ProductUrl") or prod.get("productUrl") or ""

            # Price — use unit price or first price break
            price = None
            price_breaks = prod.get("StandardPricing") or prod.get("standardPricing") or []
            if price_breaks and isinstance(price_breaks, list):
                # Find the smallest qty price break
                best = min(price_breaks, key=lambda p: p.get("BreakQuantity", p.get("breakQuantity", 999999)))
                price = best.get("UnitPrice", best.get("unitPrice"))
            else:
                price = prod.get("UnitPrice") or prod.get("unitPrice")

            results.append({
                "vendor_name": "DigiKey",
                "manufacturer": mfr,
                "mpn_matched": mpn,
                "qty_available": _safe_int(qty),
                "unit_price": _safe_float(price),
                "currency": "USD",
                "source_type": "digikey",
                "is_authorized": True,
                "confidence": 5 if qty else 3,
                "click_url": url if url.startswith("http") else f"https://www.digikey.com{url}" if url else "",
                "vendor_sku": dk_pn,
                "vendor_url": "https://www.digikey.com",
                "description": detail_desc,
            })

        log.info(f"DigiKey: {pn} -> {len(results)} results")
        return results


def _safe_int(v):
    if v is None: return None
    try: return int(v)
    except: return None

def _safe_float(v):
    if v is None: return None
    try: return float(v)
    except: return None
