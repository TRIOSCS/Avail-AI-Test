"""
API connectors for searching vendor databases.

Each connector searches one source and returns a list of dicts with:
  vendor_name, part_number, quantity, price, condition, lead_time_days,
  lead_time_text, manufacturer, source_type, source_url, confidence, evidence_type
"""
import httpx
import asyncio
import structlog
from abc import ABC, abstractmethod

logger = structlog.get_logger()


class BaseConnector(ABC):
    """Base class with built-in retry logic."""

    def __init__(self, timeout: float = 15.0, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries

    async def search(self, part_number: str) -> list[dict]:
        """Search with automatic retry. Never raises — returns [] on failure."""
        for attempt in range(self.max_retries + 1):
            try:
                return await self._do_search(part_number)
            except Exception as e:
                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff
                else:
                    logger.warning(f"{self.__class__.__name__}_failed", part=part_number, error=str(e))
        return []

    @abstractmethod
    async def _do_search(self, part_number: str) -> list[dict]:
        pass


class OctopartConnector(BaseConnector):
    """Search Octopart (Nexar) API."""

    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key
        self.source_type = "octopart"

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                "https://octopart.com/api/v4/rest/search",
                params={"q": part_number, "apikey": self.api_key, "limit": 20},
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for hit in data.get("results", []):
            item = hit.get("item", {})
            mfr = item.get("manufacturer", {}).get("name", "")

            for seller in item.get("sellers", []):
                vendor_name = seller.get("company", {}).get("name", "")
                is_auth = seller.get("is_authorized", False)

                for offer in seller.get("offers", []):
                    qty = offer.get("inventory_level")
                    prices = offer.get("prices", {})
                    price = None
                    for currency_prices in prices.values():
                        if currency_prices:
                            price = currency_prices[0][1] if len(currency_prices[0]) > 1 else None
                            break

                    results.append({
                        "vendor_name": vendor_name,
                        "part_number": item.get("mpn", part_number),
                        "manufacturer": mfr,
                        "quantity": int(qty) if qty else None,
                        "price": float(price) if price else None,
                        "condition": "new",
                        "lead_time_days": 0 if qty and qty > 0 else None,
                        "lead_time_text": "Stock" if qty and qty > 0 else None,
                        "source_type": self.source_type,
                        "source_url": item.get("octopart_url", ""),
                        "confidence": 5 if is_auth and qty else 4,
                        "evidence_type": "active_listing",
                        "vendor_type": "distributor" if is_auth else "broker",
                        "is_authorized": is_auth,
                    })

        logger.info("octopart_search", part=part_number, results=len(results))
        return results


class BrokerBinConnector(BaseConnector):
    """Search BrokerBin API."""

    def __init__(self, api_key: str, api_secret: str):
        super().__init__()
        self.api_key = api_key
        self.api_secret = api_secret
        self.source_type = "brokerbin"

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                "https://api.brokerbin.com/v1/search",
                params={"q": part_number},
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "X-Api-Secret": self.api_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("results", []):
            results.append({
                "vendor_name": item.get("company", ""),
                "part_number": item.get("part_number", part_number),
                "manufacturer": item.get("manufacturer", ""),
                "quantity": int(item["quantity"]) if item.get("quantity") else None,
                "price": float(item["price"]) if item.get("price") else None,
                "condition": item.get("condition"),
                "lead_time_days": None,
                "lead_time_text": None,
                "source_type": self.source_type,
                "source_url": item.get("url", ""),
                "confidence": 4 if item.get("quantity") else 3,
                "evidence_type": "active_listing",
                "vendor_type": "broker",
                "is_authorized": False,
            })

        logger.info("brokerbin_search", part=part_number, results=len(results))
        return results
