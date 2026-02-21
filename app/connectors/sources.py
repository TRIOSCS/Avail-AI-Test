"""API connectors — Nexar (Octopart) and BrokerBin."""

import asyncio
import logging
from abc import ABC, abstractmethod
from urllib.parse import quote_plus

from ..utils import safe_float, safe_int

log = logging.getLogger(__name__)


class BaseConnector(ABC):
    def __init__(self, timeout: float = 20.0, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries

    async def search(self, part_number: str) -> list[dict]:
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._do_search(part_number)
            except Exception as e:
                last_err = e
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
                else:
                    log.warning(
                        f"{self.__class__.__name__} failed for {part_number}: {e}"
                    )
        raise last_err  # propagate so caller can track the error

    @abstractmethod
    async def _do_search(self, part_number: str) -> list[dict]:
        pass


class NexarConnector(BaseConnector):
    """Nexar/Octopart GraphQL API — full seller data."""

    TOKEN_URL = "https://identity.nexar.com/connect/token"
    API_URL = "https://api.nexar.com/graphql"

    FULL_QUERY = """
    query ($mpn: String!) {
      supSearchMpn(q: $mpn, limit: 20) {
        results { part {
          mpn
          manufacturer { name }
          sellers {
            company { name homepageUrl }
            isAuthorized
            offers {
              inventoryLevel
              prices { price currency quantity }
              clickUrl
              sku
            }
          }
        }}
      }
    }"""

    # Fallback query when role blocks 'sellers' — gets aggregate availability/pricing
    AGGREGATE_QUERY = """
    query ($mpn: String!) {
      supSearchMpn(q: $mpn, limit: 20) {
        results { part {
          mpn
          manufacturer { name }
          shortDescription
          totalAvail
          medianPrice1000 { price currency }
        }}
      }
    }"""

    def __init__(self, client_id: str, client_secret: str):
        super().__init__()
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        from ..http_client import http

        r = await http.post(
            self.TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        r.raise_for_status()
        self._token = r.json()["access_token"]
        return self._token

    async def _run_query(self, query: str, part_number: str) -> dict:
        from ..http_client import http

        token = await self._get_token()
        r = await http.post(
            self.API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": {"mpn": part_number}},
            timeout=self.timeout,
        )
        if r.status_code == 401:
            self._token = None
            token = await self._get_token()
            r = await http.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": {"mpn": part_number}},
                timeout=self.timeout,
            )
        r.raise_for_status()
        return r.json()

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.client_id:
            return []

        data = await self._run_query(self.FULL_QUERY, part_number)
        errors = data.get("errors", [])
        if errors:
            msg = errors[0].get("message", "")
            log.warning(f"Nexar query error for {part_number}: {msg[:120]}")
            # Fall back to aggregate query if sellers field is not authorized
            if "not authorized" in msg.lower() and "sellers" in msg.lower():
                log.info(f"Nexar: retrying {part_number} with aggregate query (totalAvail + medianPrice)")
                data = await self._run_query(self.AGGREGATE_QUERY, part_number)
                results_data = (
                    (data.get("data") or {}).get("supSearchMpn", {}).get("results", [])
                )
                return self._parse_aggregate(results_data, part_number) if results_data else []

        results_data = (
            (data.get("data") or {}).get("supSearchMpn", {}).get("results", [])
        )
        if not results_data:
            return []

        return self._parse_full(results_data, part_number)

    def _parse_full(self, results_data: list, pn: str) -> list[dict]:
        results = []
        seen = set()
        octopart_url = f"https://octopart.com/search?q={quote_plus(pn)}"

        for hit in results_data:
            part = hit.get("part") or {}
            mpn = part.get("mpn", pn)
            mfr = (part.get("manufacturer") or {}).get("name", "")
            sellers = part.get("sellers") or []

            if not sellers:
                key = f"_ref_{mpn}_{mfr}"
                if key not in seen:
                    seen.add(key)
                    results.append(
                        {
                            "vendor_name": "(no sellers listed)",
                            "manufacturer": mfr,
                            "mpn_matched": mpn,
                            "qty_available": None,
                            "unit_price": None,
                            "currency": "USD",
                            "source_type": "octopart",
                            "is_authorized": False,
                            "confidence": 2,
                            "octopart_url": octopart_url,
                        }
                    )
                continue

            for seller in sellers:
                name = (seller.get("company") or {}).get("name", "")
                if not name:
                    continue
                auth = seller.get("isAuthorized", False)
                homepage = (seller.get("company") or {}).get("homepageUrl", "")

                offers = seller.get("offers") or []
                if not offers:
                    key = f"{name}_{mpn}"
                    if key not in seen:
                        seen.add(key)
                        results.append(
                            {
                                "vendor_name": name,
                                "manufacturer": mfr,
                                "mpn_matched": mpn,
                                "qty_available": None,
                                "unit_price": None,
                                "currency": "USD",
                                "source_type": "octopart",
                                "is_authorized": auth,
                                "confidence": 3 if auth else 2,
                                "octopart_url": octopart_url,
                                "vendor_url": homepage,
                            }
                        )
                    continue

                for offer in offers:
                    qty = offer.get("inventoryLevel")
                    price, currency = None, "USD"
                    prices = offer.get("prices") or []
                    if prices:
                        best = min(prices, key=lambda p: p.get("quantity", 999999))
                        price = best.get("price")
                        currency = best.get("currency", "USD")

                    click_url = offer.get("clickUrl", "")
                    sku = offer.get("sku", "")

                    key = f"{name}_{mpn}_{sku}"
                    if key in seen:
                        continue
                    seen.add(key)

                    results.append(
                        {
                            "vendor_name": name,
                            "manufacturer": mfr,
                            "mpn_matched": mpn,
                            "qty_available": int(qty) if qty else None,
                            "unit_price": round(float(price), 4) if price else None,
                            "currency": currency,
                            "source_type": "octopart",
                            "is_authorized": auth,
                            "confidence": 5 if auth and qty else 4 if qty else 3,
                            "octopart_url": octopart_url,
                            "click_url": click_url,
                            "vendor_url": homepage,
                            "vendor_sku": sku,
                        }
                    )

        log.info(f"Nexar: {pn} -> {len(results)} seller results")
        return results

    def _parse_aggregate(self, results_data: list, pn: str) -> list[dict]:
        """Parse Nexar aggregate results (totalAvail + medianPrice, no per-seller breakdown)."""
        results = []
        octopart_url = f"https://octopart.com/search?q={quote_plus(pn)}"

        for hit in results_data:
            part = hit.get("part") or {}
            mpn = part.get("mpn", pn)
            mfr = (part.get("manufacturer") or {}).get("name", "")
            total_avail = part.get("totalAvail")
            median_price = part.get("medianPrice1000") or {}
            price = median_price.get("price")
            currency = median_price.get("currency", "USD")

            if not total_avail and not price:
                continue  # Skip parts with no useful data

            results.append({
                "vendor_name": "Octopart (aggregate)",
                "manufacturer": mfr,
                "mpn_matched": mpn,
                "qty_available": int(total_avail) if total_avail else None,
                "unit_price": round(float(price), 4) if price else None,
                "currency": currency,
                "source_type": "octopart",
                "is_authorized": True,
                "confidence": 4 if total_avail and price else 3,
                "octopart_url": octopart_url,
                "click_url": octopart_url,
            })

        log.info(f"Nexar: {pn} -> {len(results)} aggregate results (totalAvail + medianPrice)")
        return results


class BrokerBinConnector(BaseConnector):
    """BrokerBin REST API v2.

    Auth: Bearer token in Authorization header + login header for user.
    Endpoint: GET https://search.brokerbin.com/api/v2/part/search?query={mpn}&size=100
    Response: { meta: {...}, data: [{ company, country, part, mfg, cond, description, price, qty, age_in_days }] }
    """

    API_URL = "https://search.brokerbin.com/api/v2/part/search"

    def __init__(self, api_key: str, api_secret: str = ""):
        super().__init__()
        self.token = api_key  # Bearer token
        self.login = api_secret  # BrokerBin username (e.g. "triomhk")

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.token:
            return []

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.login:
            headers["login"] = self.login

        params = {
            "query": part_number,
            "size": "100",
        }

        from ..http_client import http_redirect

        r = await http_redirect.get(self.API_URL, params=params, headers=headers, timeout=self.timeout)

        if r.status_code != 200:
            log.warning(
                f"BrokerBin: HTTP {r.status_code} for {part_number}: {r.text[:200]}"
            )
            return []

        try:
            body = r.json()
        except Exception:
            log.warning(f"BrokerBin: non-JSON response for {part_number}")
            return []

        items = body.get("data", [])
        if not isinstance(items, list):
            return []

        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            company = (item.get("company") or "").strip()
            if not company:
                continue

            # Log all available fields from first result (once per search)
            if not results:
                log.info(f"BrokerBin fields for {part_number}: {list(item.keys())}")

            qty = safe_int(item.get("qty"))
            price = safe_float(item.get("price"))
            age = safe_int(item.get("age_in_days"))

            # Confidence: higher for fresh listings with qty and price
            conf = 3
            if qty and qty > 0:
                conf = 4
            if qty and price and price > 0:
                conf = 5

            results.append(
                {
                    "vendor_name": company,
                    "manufacturer": (item.get("mfg") or "").strip(),
                    "mpn_matched": (item.get("part") or part_number).strip(),
                    "qty_available": qty,
                    "unit_price": round(price, 4) if price and price > 0 else None,
                    "currency": "USD",
                    "source_type": "brokerbin",
                    "is_authorized": False,
                    "confidence": conf,
                    "condition": (item.get("cond") or "").strip(),
                    "description": (item.get("description") or "").strip()[:500],
                    "country": (item.get("country") or "").strip(),
                    "age_in_days": age,
                    "vendor_phone": (
                        item.get("phone") or item.get("telephone") or ""
                    ).strip()
                    or None,
                    "vendor_email": (
                        item.get("email") or item.get("contact_email") or ""
                    ).strip()
                    or None,
                }
            )

        total = (body.get("meta") or {}).get("total", len(results))
        log.info(f"BrokerBin: {part_number} -> {len(results)} results (total: {total})")
        return results
