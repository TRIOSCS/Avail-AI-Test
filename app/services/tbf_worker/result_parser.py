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
- td[6]: company / vendor. When the session is authenticated, this carries the
  real seller: the company name (the cell's ``title`` attr — full/untruncated —
  and the first inner ``<div>``) plus a phone number in a second ``<div>``. When
  the session is logged OUT, TBF anonymizes it to a single ``"TBS Member"`` div
  with no phone. We pull the name from the title (falling back to the first div)
  and the phone from the second div.
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


def _is_phone_like(text: str) -> bool:
    """True if ``text`` looks like a phone number rather than a company name.

    TBF contact lines are like ``"+30 2492024777"`` / ``"+1 6087814030"``. A
    nameless seller renders only such a string in the company cell; we must not
    treat it as a vendor name. Heuristic: it has digits and, once ``+``, spaces,
    ``-``, ``/``, ``(`` and ``)`` are stripped, what remains is all digits (i.e.
    no letters — a real company name always carries letters).
    """
    text = (text or "").strip()
    if not text or not any(ch.isdigit() for ch in text):
        return False
    stripped = text.translate(str.maketrans("", "", "+-/() "))
    return stripped.isdigit()


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

            # td[6]: company/vendor. Authenticated → real name (cell `title`
            # attr / first div) + phone (second div). Logged out → a single
            # "TBS Member" div, no phone. Never `get_text()` the whole cell —
            # that would mash the name and phone together.
            company_td = cells[6]
            company_divs = company_td.find_all("div")
            vendor_name = (company_td.get("title") or "").strip()
            if not vendor_name and company_divs:
                first_div_text = company_divs[0].get_text(strip=True)
                # A nameless seller (no company set) renders only a phone in the
                # cell. Never let that phone become the vendor identity — that
                # would corrupt vendor matching/dedup downstream. Treat a
                # phone-shaped first div as "no name" (row then skipped by
                # sighting_writer's no-vendor guard).
                vendor_name = "" if _is_phone_like(first_div_text) else first_div_text
            vendor_phone = ""
            if len(company_divs) > 1:
                phone_text = company_divs[1].get_text(strip=True)
                # The second div is the contact line; guard that it is phone-like.
                if _is_phone_like(phone_text):
                    vendor_phone = phone_text
            # Either the name's own div was the phone, or a phone-only cell with
            # no name div at all — recover the phone so we don't drop the contact.
            if not vendor_phone and company_divs:
                lone_text = company_divs[0].get_text(strip=True)
                if not vendor_name and _is_phone_like(lone_text):
                    vendor_phone = lone_text

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
                    vendor_phone=vendor_phone,
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
