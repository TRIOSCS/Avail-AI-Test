"""NetComponents results HTML parser.

Parses the server-side rendered HTML from /search/result into structured
NcSighting dataclass instances. Uses confirmed CSS selectors from site
inspection (Feb 2026).

HTML structure:
  .div-table-float-reg.floating-block  (one per region)
    .region-header                      "The Americas", "Europe", etc.
    .stock-type                         "In-Stock Inventory"
    table.searchresultstable            data rows (NOT #trv_0 which is header clone)

Each row has 14 <td> cells:
  0=Part, 1=.nctd(url), 2=.ncdsl, 3=Mfr, 4=DC, 5=Desc,
  6=Uploaded, 7=Ctr, 8=Qty, 9=.ncprc(price), 10=.nccart,
  11=.ncsqrs, 12=Supplier, 13=.spn(sponsor)

Called by: worker loop (after search_engine)
Depends on: beautifulsoup4, lxml
"""

import json
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class PriceBreak:
    """A single price/quantity tier from NC results."""

    price: float = 0.0
    min_qty: int = 0


@dataclass
class NcSighting:
    """A single vendor listing parsed from NC results HTML."""

    part_number: str = ""
    manufacturer: str = ""
    date_code: str = ""
    description: str = ""
    uploaded_date: str = ""
    country: str = ""
    quantity: int | None = None
    vendor_name: str = ""
    region: str = ""
    inventory_type: str = "in_stock"
    is_sponsor: bool = False
    is_authorized: bool = False
    price_breaks: list[PriceBreak] = field(default_factory=list)
    currency: str | None = None
    supplier_product_url: str = ""


def parse_quantity(text: str) -> int | None:
    """Parse NC quantity string to int.

    Handles commas, '+' suffix, empty.
    """
    if not text:
        return None
    cleaned = text.strip().rstrip("+").replace(",", "")
    try:
        return int(cleaned)
    except (ValueError, TypeError):
        return None


def parse_price_breaks(element) -> tuple[list[PriceBreak], str | None]:
    """Parse price break JSON from data-pbrk attribute on .ncprc element.

    Returns (list of PriceBreak, currency string or None).
    """
    if not element:
        return [], None

    pbrk_json = element.get("data-pbrk")
    if not pbrk_json:
        return [], None

    try:
        data = json.loads(pbrk_json)
        currency = data.get("currency")
        breaks = []
        for p in data.get("Prices", []):
            breaks.append(
                PriceBreak(
                    price=float(p.get("price", 0)),
                    min_qty=int(p.get("minQty", 0)),
                )
            )
        return breaks, currency
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.debug("NC parser: failed to parse price breaks: {}", e)
        return [], None


def parse_results_html(html: str) -> list[NcSighting]:
    """Parse NC results HTML into a list of NcSighting instances.

    Uses the confirmed .div-table-float-reg.floating-block containers with region
    headers and table.searchresultstable (excluding #trv_0 sticky header clone to avoid
    double-counting).
    """
    from bs4 import BeautifulSoup

    if not html or not html.strip():
        return []

    soup = BeautifulSoup(html, "lxml")
    sightings = []

    # Find all region containers
    containers = soup.select(".div-table-float-reg.floating-block")

    if not containers:
        # Fallback: try flat table approach for unexpected layouts
        logger.warning("NC parser: no .floating-block containers found, trying flat parse")
        return _parse_flat(soup)

    for container in containers:
        # Extract region
        region_el = container.select_one(".region-header")
        region = region_el.get_text(strip=True) if region_el else "Unknown"

        # Extract stock type
        stock_type_el = container.select_one(".stock-type")
        stock_type_text = stock_type_el.get_text(strip=True).lower() if stock_type_el else ""
        inventory_type = "brokered" if "brokered" in stock_type_text else "in_stock"

        # Find data table (exclude the sticky header clone #trv_0)
        data_tables = container.select("table.searchresultstable:not(#trv_0)")
        if not data_tables:
            continue

        for table in data_tables:
            rows = table.select("tr")
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 13:
                    continue

                try:
                    cell_texts = [c.get_text(strip=True) for c in cells]

                    part_number = cell_texts[0]
                    if not part_number or part_number.lower() in ("part number", ""):
                        continue

                    manufacturer = cell_texts[3] if len(cell_texts) > 3 else ""
                    date_code = cell_texts[4] if len(cell_texts) > 4 else ""
                    description = cell_texts[5] if len(cell_texts) > 5 else ""
                    uploaded_date = cell_texts[6] if len(cell_texts) > 6 else ""
                    country = cell_texts[7] if len(cell_texts) > 7 else ""
                    qty_text = cell_texts[8] if len(cell_texts) > 8 else ""
                    vendor_name = cell_texts[12] if len(cell_texts) > 12 else ""

                    # Sponsor check (cell 13)
                    is_sponsor = bool(cell_texts[13].strip()) if len(cell_texts) > 13 else False

                    # Supplier product URL from .nctd element
                    nctd = cells[1].select_one(".nctd") if len(cells) > 1 else None
                    if not nctd:
                        nctd = row.select_one(".nctd")
                    supplier_url = nctd.get("data-url", "") if nctd else ""

                    # Price breaks from .ncprc element
                    ncprc = cells[9].select_one(".ncprc") if len(cells) > 9 else None
                    if not ncprc:
                        ncprc = row.select_one(".ncprc")
                    price_breaks, currency = parse_price_breaks(ncprc)

                    # Authorized distributor heuristic: has price breaks = authorized
                    is_authorized = len(price_breaks) > 0

                    sighting = NcSighting(
                        part_number=part_number,
                        manufacturer=manufacturer,
                        date_code=date_code,
                        description=description,
                        uploaded_date=uploaded_date,
                        country=country,
                        quantity=parse_quantity(qty_text),
                        vendor_name=vendor_name,
                        region=region,
                        inventory_type=inventory_type,
                        is_sponsor=is_sponsor,
                        is_authorized=is_authorized,
                        price_breaks=price_breaks,
                        currency=currency,
                        supplier_product_url=supplier_url,
                    )
                    sightings.append(sighting)

                except (IndexError, AttributeError) as e:
                    logger.debug("NC parser: skipping malformed row: {}", e)
                    continue

    logger.info("NC parser: extracted {} sightings from HTML ({} regions)", len(sightings), len(containers))
    return sightings


def _parse_flat(soup) -> list[NcSighting]:
    """Fallback flat parser when floating-block containers aren't found.

    Scans all <tr> rows looking for data rows with 8+ cells.
    """
    sightings = []
    current_region = ""
    current_inventory_type = "in_stock"

    for row in soup.find_all("tr"):
        text = row.get_text(strip=True).lower()

        if "the americas" in text or "americas" in text:
            current_region = "The Americas"
            continue
        if "europe" in text:
            current_region = "Europe"
            continue
        if "asia" in text:
            current_region = "Asia"
            continue
        if "in-stock" in text or "in stock" in text:
            current_inventory_type = "in_stock"
            continue
        if "brokered" in text:
            current_inventory_type = "brokered"
            continue

        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        try:
            cell_texts = [c.get_text(strip=True) for c in cells]
            part_number = cell_texts[0]
            if not part_number or "part number" in part_number.lower():
                continue

            ncprc = row.select_one(".ncprc")
            price_breaks, currency = parse_price_breaks(ncprc)
            nctd = row.select_one(".nctd")
            supplier_url = nctd.get("data-url", "") if nctd else ""

            sighting = NcSighting(
                part_number=part_number,
                manufacturer=cell_texts[3] if len(cell_texts) > 3 else "",
                date_code=cell_texts[4] if len(cell_texts) > 4 else "",
                description=cell_texts[5] if len(cell_texts) > 5 else "",
                uploaded_date=cell_texts[6] if len(cell_texts) > 6 else "",
                country=cell_texts[7] if len(cell_texts) > 7 else "",
                quantity=parse_quantity(cell_texts[8] if len(cell_texts) > 8 else ""),
                vendor_name=cell_texts[12] if len(cell_texts) > 12 else cell_texts[-1],
                region=current_region,
                inventory_type=current_inventory_type,
                is_sponsor=bool(cell_texts[13].strip()) if len(cell_texts) > 13 else False,
                is_authorized=len(price_breaks) > 0,
                price_breaks=price_breaks,
                currency=currency,
                supplier_product_url=supplier_url,
            )
            sightings.append(sighting)
        except (IndexError, AttributeError) as e:
            logger.debug("NC parser (flat): skipping row: {}", e)
            continue

    logger.info("NC parser (flat fallback): extracted {} sightings", len(sightings))
    return sightings
