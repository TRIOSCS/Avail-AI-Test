"""ICsource results HTML parser.

Parses the HTML returned by ICsource search into structured IcsSighting
dataclass instances. Extracts vendor contact info (email, phone) from
the company info blocks alongside the listing data.

ICsource HTML structure:
- .divDateGroup headers group results by upload date
- .flex blocks contain company info (name, email, phone, OpenProfile link)
- .browseMatchItem rows contain listing data in td.resultsdontshowon900 cells
  Columns: Part#, Comments, Qty, Price, MFG, D/C, Stock

Called by: worker loop (after search_engine)
Depends on: beautifulsoup4
"""

import re
from dataclasses import dataclass

from loguru import logger


@dataclass
class IcsSighting:
    """A single vendor listing parsed from ICsource results HTML."""

    part_number: str = ""
    manufacturer: str = ""
    date_code: str = ""
    description: str = ""        # from Comments column
    quantity: int | None = None
    price: str = ""              # raw price string
    vendor_name: str = ""        # company name
    vendor_email: str = ""       # from mailto link
    vendor_phone: str = ""       # from .clicktocall
    vendor_company_id: str = ""  # from OpenProfile(ID)
    in_stock: bool = False       # from Stock checkmark column
    uploaded_date: str = ""      # from .divDateGroup


def parse_quantity(text: str) -> int | None:
    """Parse ICS quantity string to int. Handles commas, '+' suffix, empty."""
    if not text:
        return None
    cleaned = text.strip().rstrip("+").replace(",", "")
    try:
        return int(cleaned)
    except (ValueError, TypeError):
        return None


def _extract_company_info(block) -> dict:
    """Extract company info from a .flex or company info block.

    Returns dict with keys: name, email, phone, company_id.
    """
    info = {"name": "", "email": "", "phone": "", "company_id": ""}

    # Company name — usually in a bold/link element
    name_el = block.find("a", href=re.compile(r"OpenProfile", re.I))
    if name_el:
        info["name"] = name_el.get_text(strip=True)
        # Extract company ID from OpenProfile(123)
        href = name_el.get("href", "") or name_el.get("onclick", "")
        match = re.search(r"OpenProfile\((\d+)\)", href)
        if match:
            info["company_id"] = match.group(1)

    # Email from mailto link
    mailto = block.find("a", href=re.compile(r"^mailto:", re.I))
    if mailto:
        email = mailto.get("href", "").replace("mailto:", "").strip()
        info["email"] = email.split("?")[0]  # Strip any ?subject= params

    # Phone from .clicktocall element
    phone_el = block.find(class_=re.compile(r"clicktocall", re.I))
    if phone_el:
        info["phone"] = phone_el.get_text(strip=True)

    return info


def parse_results_html(html: str) -> list[IcsSighting]:
    """Parse ICsource results HTML into a list of IcsSighting instances.

    The HTML structure has:
    - .divDateGroup headers for upload date grouping
    - .flex blocks with company info (name, email, phone)
    - .browseMatchItem rows with listing data columns

    This parser is defensive — malformed rows are skipped with a warning.
    """
    from bs4 import BeautifulSoup

    if not html or not html.strip():
        return []

    soup = BeautifulSoup(html, "html.parser")
    sightings = []
    current_date = ""
    current_company = {"name": "", "email": "", "phone": "", "company_id": ""}

    # Strategy: iterate through all elements, tracking context
    # Look for date groups, company blocks, and result rows
    all_elements = soup.find_all(True)

    for el in all_elements:
        classes = " ".join(el.get("class", []))

        # Date group header
        if "divDateGroup" in classes:
            current_date = el.get_text(strip=True)
            continue

        # Company info block — contains OpenProfile links
        if el.find("a", href=re.compile(r"OpenProfile", re.I)) and "flex" in classes:
            current_company = _extract_company_info(el)
            continue

        # Result row
        if "browseMatchItem" in classes:
            try:
                cells = el.find_all("td")
                if len(cells) < 5:
                    continue

                # Extract cell texts
                cell_texts = [c.get_text(strip=True) for c in cells]

                # ICsource columns: Part#, Comments, Qty, Price, MFG, D/C, Stock
                part_number = cell_texts[0] if len(cell_texts) > 0 else ""
                description = cell_texts[1] if len(cell_texts) > 1 else ""
                qty_text = cell_texts[2] if len(cell_texts) > 2 else ""
                price = cell_texts[3] if len(cell_texts) > 3 else ""
                manufacturer = cell_texts[4] if len(cell_texts) > 4 else ""
                date_code = cell_texts[5] if len(cell_texts) > 5 else ""

                # Stock column — check for checkmark image or text
                in_stock = False
                if len(cells) > 6:
                    stock_cell = cells[6]
                    if stock_cell.find("img") or "✓" in stock_cell.get_text():
                        in_stock = True

                sighting = IcsSighting(
                    part_number=part_number,
                    manufacturer=manufacturer,
                    date_code=date_code,
                    description=description,
                    quantity=parse_quantity(qty_text),
                    price=price,
                    vendor_name=current_company["name"],
                    vendor_email=current_company["email"],
                    vendor_phone=current_company["phone"],
                    vendor_company_id=current_company["company_id"],
                    in_stock=in_stock,
                    uploaded_date=current_date,
                )
                sightings.append(sighting)

            except (IndexError, AttributeError) as e:
                logger.debug("ICS parser: skipping malformed row: {}", e)
                continue

    logger.info("ICS parser: extracted {} sightings from HTML", len(sightings))
    return sightings
