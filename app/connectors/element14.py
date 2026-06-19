"""Element14 / Newark Search API connector.

Searches the element14 product catalog (Newark US store) by manufacturer
part number. REST API with simple API key auth. Falls back to keyword
search if exact MPN match returns no results.

Called by: search_service.py
Depends on: BaseConnector, httpx
"""

from typing import Any
from urllib.parse import quote_plus

from loguru import logger

from ..http_client import http
from ..utils import safe_float, safe_int
from ._core_attrs import clean_str, generic_attribute, map_rohs
from ._vendor_spec_map import extract_vendor_specs
from .errors import ConnectorAuthError, ConnectorRateLimitError
from .sources import BaseConnector

# Markers that mean a 403 is a credential rejection (not a transient QPS cap).
_AUTH_MARKERS = ("invalid", "unauthorized", "forbidden", "api key", "not accepted")


def _product_category(prod: dict, attrs: Any) -> str | None:
    """The product's distributor category string, if the response carries one.

    Element14's `large` response shape is inconsistent across catalogs: a per-product
    category may arrive as ``category.name`` (object), a bare ``categoryName`` string, or a
    "Category"/"Product Category" attribute. None when nothing is present — the spec mapper
    then falls back to the description grammar (see ``_resolve_commodity_for_specs``).
    """
    cat = prod.get("category")
    if isinstance(cat, dict):
        cat = cat.get("name")
    return clean_str(
        cat or prod.get("categoryName") or generic_attribute(attrs, "attributeLabel", "attributeValue", ("Category",)),
        maxlen=255,
    )


def _resolve_commodity_for_specs(category: str | None, description: str) -> str | None:
    """Canonical commodity for attribute mapping: the distributor category string first
    (normalized like ``set_category`` does), then the description grammar fallback.

    Mirrors ``vendor_spec_enrich._resolve_commodity`` (the writer's resolver) so the
    connector maps attributes under the SAME commodity the writer will categorize the card
    to. Lazy imports keep the connector free of a hard service dependency at module load.
    """
    from app.services.category_normalizer import normalize_category
    from app.services.desc_extractor.categorizer import categorize_from_desc

    return normalize_category(category) or categorize_from_desc(description or "")


class Element14Connector(BaseConnector):
    """Element14 Product Search — API key auth, Newark US store."""

    source_name: str = "element14"

    SEARCH_URL = "https://api.element14.com/catalog/products"

    def __init__(self, api_key: str):
        super().__init__(timeout=15.0)
        self.api_key = api_key

    async def _do_search(self, part_number: str) -> list[dict]:
        if not self.api_key:
            return []

        # Try exact MPN search first
        results = await self._api_search(f"manuPartNum:{part_number}", part_number)
        if results:
            return results

        # Fallback: keyword search (catches partial matches and alternate formats)
        logger.debug(f"element14: exact MPN match returned 0 for {part_number}, trying keyword search")
        return await self._api_search(part_number, part_number)

    async def _api_search(self, term: str, part_number: str) -> list[dict]:
        """Run a single search against the element14 API."""
        params = {
            "term": term,
            "storeInfo.id": "www.newark.com",
            "resultsSettings.offset": "0",
            "resultsSettings.numberOfResults": "25",
            "resultsSettings.responseGroup": "large",
            "callInfo.apiKey": self.api_key,
            "callInfo.responseDataFormat": "json",
        }

        r = await http.get(self.SEARCH_URL, params=params, timeout=self.timeout)
        if r.status_code == 400:
            logger.debug(f"element14: 400 Bad Request for term '{term}' — skipping")
            return []

        # Hard errors raise typed ConnectorError subclasses so
        # health_monitor.ping_source flips api_sources.status to 'error'.
        # The first 401/403 raised by an exact MPN search also short-
        # circuits the keyword fallback in _do_search — auto-recovers
        # when the next ping returns 200. See
        # docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract.
        # element14 returns HTTP 403 for BOTH credential rejection AND a per-second
        # rate cap ("Account Over Queries Per Second Limit"). Distinguish by body:
        # QPS errors contain "queries per second" but no auth-failure markers.
        body = r.text.lower()
        if r.status_code == 403 and "queries per second" in body and not any(m in body for m in _AUTH_MARKERS):
            raise ConnectorRateLimitError(f"element14 rate limited (QPS): {r.text[:200]}")
        if r.status_code in (401, 403):
            # 401 = bad/expired API key; 403 = key rejected for the requested
            # store/region. Both require operator credential rotation.
            raise ConnectorAuthError(f"element14 auth error: HTTP {r.status_code} {r.text[:200]}")
        if r.status_code == 429:
            raise ConnectorRateLimitError(f"element14 rate limited: {r.text[:200]}")

        r.raise_for_status()
        data = r.json()

        return self._parse(data, part_number)

    def _parse(self, data: dict, pn: str) -> list[dict]:
        container = data.get("manufacturerPartNumberSearchReturn", {})
        products = container.get("products", [])
        results = []

        for prod in products:
            mpn = prod.get("translatedManufacturerPartNumber") or pn
            mfr = prod.get("brandName", "")
            desc = prod.get("displayName", "")
            sku = prod.get("sku", "")

            # Stock
            stock_info = prod.get("stock", {})
            qty = safe_int(stock_info.get("level")) if stock_info else None

            # Price — first price break
            price = None
            prices = prod.get("prices", [])
            if prices:
                price = safe_float(prices[0].get("cost"))

            click_url = f"https://www.newark.com/search?st={quote_plus(mpn)}"

            # Core attributes (optional — None when absent)
            attrs = prod.get("attributes")
            rohs = map_rohs(
                generic_attribute(attrs, "attributeLabel", "attributeValue", ("RoHS", "RoHS Status", "ROHS"))
            )
            package = clean_str(
                generic_attribute(
                    attrs, "attributeLabel", "attributeValue", ("Package", "Case / Package", "Package / Case")
                ),
                maxlen=100,
            )

            # Commodity-specific parametrics: Element14's `attributes` ARE structured
            # parametrics (Capacitance / Tolerance / …). Map them to seeded spec keys
            # under the resolved commodity (category string → desc-grammar fallback). The
            # WRITER (vendor_spec_enrich) records each via record_spec at element14_api/90
            # — the ladder + the seed schema's enum/numeric gate are the final arbiter, so
            # values are emitted raw (only cosmetic enum-format gaps are closed in the map).
            # Short-circuit when there are no attributes to map: skips the commodity-grammar
            # resolution (extract_desc) on the live pricing path for results that can never
            # produce specs anyway.
            category = _product_category(prod, attrs)
            specs: dict[str, str] = {}
            dropped: dict[str, str] = {}
            if isinstance(attrs, list) and attrs:
                commodity = _resolve_commodity_for_specs(category, desc)
                specs, dropped = extract_vendor_specs(
                    attrs, commodity, name_key="attributeLabel", value_key="attributeValue"
                )

            results.append(
                {
                    "vendor_name": "element14",
                    "manufacturer": mfr,
                    "mpn_matched": mpn,
                    "qty_available": qty,
                    "unit_price": round(price, 4) if price else None,
                    "currency": "USD",
                    "source_type": "element14",
                    "is_authorized": True,
                    "confidence": 5 if qty and qty > 0 else 3,
                    "click_url": click_url,
                    "vendor_sku": sku,
                    "vendor_url": "https://www.newark.com",
                    "description": desc[:500] if desc else "",
                    "category": category,
                    "rohs_status": rohs,
                    "package_type": package,
                    "specs": specs,
                    "dropped": dropped,
                }
            )

        logger.info(f"element14: {pn} -> {len(results)} results")
        return results
