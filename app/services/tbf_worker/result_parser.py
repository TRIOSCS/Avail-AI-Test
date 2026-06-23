"""The Broker Forum (TBF) results HTML parser.

Parses the HTML returned by a TBF search into structured TbfSighting
dataclass instances.

TBF is a Vue SPA. The results table is the first ``<table>`` whose rows include
``tr.hover-higlight-anchor`` (data rows). Each data row has 8 ``<td>`` cells:

- td[0]: part# (first inner ``div.text-red-600``) + description (second inner div
  / its ``title`` attr)
- td[1]: manufacturer
- td[2]: quantity
- td[3]: condition (REF / USED / NEW / CALL / ...)
- td[4]: price ``<span>`` — either ``"CALL"`` (no numeric) or a currency-symboled
  number like ``"€ 114"`` / ``"$ 99"`` / ``"£ 50"``
- td[5]: member-tier badge (Gold/Diamond) — not parsed
- td[6]: company / vendor — currently always anonymized to ``"TBS Member"``; the
  real vendor identity is behind a row click (documented future enhancement, so
  vendor_email / vendor_phone / vendor_company_id stay empty)
- td[7]: country ISO (e.g. "GR", "US")

Called by: worker loop (after search_engine)
Depends on: beautifulsoup4
"""

from dataclasses import dataclass

from loguru import logger

# Currency symbol -> ISO code. TBF is a European marketplace (EUR/USD/GBP).
_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}


@dataclass
class TbfSighting:
    """A single vendor listing parsed from TBF results HTML."""

    part_number: str = ""
    manufacturer: str = ""
    date_code: str = ""
    description: str = ""
    quantity: int | None = None
    price: str = ""  # raw price string / price-breaks blob
    currency: str = ""  # EUR / USD / GBP — TBF is a European marketplace
    vendor_name: str = ""  # company name
    vendor_email: str = ""  # from mailto link
    vendor_phone: str = ""  # from contact block
    vendor_company_id: str = ""  # TBF company profile id
    country: str = ""  # vendor country
    region: str = ""  # vendor region
    in_stock: bool = False  # stock vs. brokered
    is_authorized: bool = False  # authorized distributor flag
    uploaded_date: str = ""  # listing upload/refresh date
    supplier_product_url: str = ""  # deep link to the listing/profile


def parse_quantity(text: str) -> int | None:
    """Parse a TBF quantity string to int.

    Handles commas, '+' suffix, empty.
    """
    if not text:
        return None
    cleaned = text.strip().rstrip("+").replace(",", "")
    try:
        return int(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_price(text: str) -> tuple[str, str]:
    """Parse a TBF price cell into ``(price, currency)``.

    "CALL" (any case) -> ("", "") — no numeric price, no currency. "€ 114" -> ("114",
    "EUR"); "$ 99" -> ("99", "USD"); "£ 50" -> ("50", "GBP"). Unknown / unparseable ->
    the raw text as price, currency "".
    """
    raw = (text or "").strip()
    if not raw or raw.upper() == "CALL":
        return "", ""

    symbol = raw[0]
    currency = _CURRENCY_SYMBOLS.get(symbol, "")
    if currency:
        numeric = raw[1:].strip()
        return numeric, currency
    return raw, ""


def parse_results_html(html: str) -> list[TbfSighting]:
    """Parse TBF results HTML into a list of TbfSighting instances.

    Locates the first ``<table>`` containing ``tr.hover-higlight-anchor`` rows and
    extracts one TbfSighting per such row. Empty input, or no matching table,
    returns ``[]``.

    This parser is defensive — malformed rows are skipped with a debug log and
    never raise.
    """
    from bs4 import BeautifulSoup

    if not html or not html.strip():
        return []

    soup = BeautifulSoup(html, "html.parser")

    # The results table is the first <table> whose rows include a data row.
    results_table = None
    for table in soup.find_all("table"):
        if table.select_one("tr.hover-higlight-anchor"):
            results_table = table
            break

    if results_table is None:
        return []

    sightings: list[TbfSighting] = []

    for row in results_table.select("tr.hover-higlight-anchor"):
        try:
            cells = row.find_all("td")
            if len(cells) < 8:
                logger.debug("TBF parser: skipping row with {} cells (need 8)", len(cells))
                continue

            # td[0]: part# (first inner div, class contains text-red-600) +
            # description (second inner div, or its title attr).
            inner_divs = cells[0].find_all("div")
            part_number = ""
            description = ""
            if inner_divs:
                part_number = inner_divs[0].get_text(strip=True)
            if len(inner_divs) > 1:
                desc_div = inner_divs[1]
                description = desc_div.get_text(strip=True) or (desc_div.get("title") or "").strip()

            manufacturer = cells[1].get_text(strip=True)
            quantity = parse_quantity(cells[2].get_text(strip=True))
            # td[3] is condition (REF/USED/NEW/CALL/...) — TbfSighting has no
            # condition field, so it is intentionally dropped.

            # td[4]: price span — "CALL" or currency-symboled number.
            price_span = cells[4].find("span")
            price_text = price_span.get_text(strip=True) if price_span else cells[4].get_text(strip=True)
            price, currency = _parse_price(price_text)

            # td[6]: company/vendor (anonymized "TBS Member"). The real vendor
            # is behind a row click — vendor_email/phone/company_id left empty
            # (documented future enhancement).
            vendor_name = cells[6].get_text(strip=True)

            # td[7]: country ISO. No region mapping available — store country
            # and use the country code as region (sighting_writer tolerates it).
            country = cells[7].get_text(strip=True)

            sightings.append(
                TbfSighting(
                    part_number=part_number,
                    manufacturer=manufacturer,
                    description=description,
                    quantity=quantity,
                    price=price,
                    currency=currency,
                    vendor_name=vendor_name,
                    country=country,
                    region=country,
                    in_stock=True,  # an active listing
                    is_authorized=False,  # broker marketplace, never authorized-distributor
                )
            )
        except (IndexError, AttributeError, ValueError) as e:
            logger.debug("TBF parser: skipping malformed row: {}", e)
            continue

    logger.info("TBF parser: extracted {} sightings from HTML", len(sightings))
    return sightings
