"""Sourcengine API connector — B2B electronic component marketplace."""

from loguru import logger

from ..http_client import http
from ..utils import safe_float, safe_int
from .errors import ConnectorAuthError, ConnectorRateLimitError
from .sources import BaseConnector


class SourcengineConnector(BaseConnector):
    """Sourcengine REST API — search by MPN across suppliers."""

    source_name: str = "sourcengine"

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

        params: dict[str, str | int] = {
            "q": part_number,
            "limit": 30,
        }

        r = await http.get(self.SEARCH_URL, headers=headers, params=params, timeout=self.timeout)

        # Hard errors raise typed ConnectorError subclasses so
        # health_monitor.ping_source flips api_sources.status to 'error'.
        # search_service excludes the source from user searches with an
        # 'error_skipped' chip until the next ping returns 200, at which
        # point status auto-recovers to 'live'. Persistent failures
        # (revoked key) keep flipping back to 'error' on each ping. See
        # docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract.
        if r.status_code in (401, 403):
            raise ConnectorAuthError(f"Sourcengine auth error: HTTP {r.status_code} {r.text[:200]}")
        if r.status_code == 429:
            raise ConnectorRateLimitError(f"Sourcengine rate limited: {r.text[:200]}")

        r.raise_for_status()
        data = r.json()

        return self._parse(data, part_number)

    # Recognized top-level envelope keys that hold the offer list. Kept as a constant
    # so the drift guard below and the extraction stay in lock-step.
    _OFFER_KEYS = ("offers", "results", "data")

    def _parse(self, data: dict, pn: str) -> list[dict]:
        # Defensive guard: a shape drift must surface LOUDLY, never masquerade as an
        # empty "no matches" result. Two drift signals are logged (WARNING) rather than
        # silently swallowed:
        #   1. The 200 body is not a JSON object at all (e.g. a top-level array) — that
        #      would also crash the ``data.get`` calls below, so bail out cleanly.
        #   2. The body IS an object but carries NONE of the recognized offer keys
        #      (offers/results/data) — the envelope has changed.
        # FLAG (Phase-4 audit): the connector's SEARCH_URL (/v1/search) no longer matches
        # the currently-documented endpoint (/app/api/search/parts/searchpart); a LIVE
        # Sourcengine call is required to confirm the real endpoint + response shape.
        if not isinstance(data, dict):
            logger.warning(
                "Sourcengine: 200 response for {} was not a JSON object ({}) — response shape may have drifted",
                pn,
                type(data).__name__,
            )
            return []
        if data and not any(k in data for k in self._OFFER_KEYS):
            logger.warning(
                "Sourcengine: 200 response for {} had none of the recognized offer keys "
                "{} — response shape may have drifted (keys={})",
                pn,
                self._OFFER_KEYS,
                list(data.keys())[:10],
            )

        offers = data.get("offers", data.get("results", data.get("data", [])))
        if not isinstance(offers, list):
            offers = []

        results = []
        seen = set()

        for offer in offers:
            if not isinstance(offer, dict):
                continue

            supplier = offer.get("supplier", {})
            sup_name = supplier.get("name", "") if isinstance(supplier, dict) else str(supplier)
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
                    "qty_available": safe_int(qty),
                    "unit_price": safe_float(price),
                    "currency": currency,
                    "source_type": "sourcengine",
                    "is_authorized": offer.get("authorized", False),
                    "confidence": 4 if qty else 3,
                    "click_url": url,
                    "vendor_sku": sku,
                    "moq": safe_int(moq) or None,
                }
            )

        logger.info(f"Sourcengine: {pn} -> {len(results)} results")
        return results
