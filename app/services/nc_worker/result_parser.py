"""NetComponents results HTML parser.

Parses the HTML table returned by /search/getresult into structured
NcSighting dataclass instances. Tracks region headers and inventory
type sub-headers as context for each row.

Called by: worker loop (after search_engine)
Depends on: beautifulsoup4
"""

import re
from dataclasses import dataclass, field

from loguru import logger


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


def parse_quantity(text: str) -> int | None:
    """Parse NC quantity string to int. Handles commas, '+' suffix, empty."""
    if not text:
        return None
    cleaned = text.strip().rstrip("+").replace(",", "")
    try:
        return int(cleaned)
    except (ValueError, TypeError):
        return None


def parse_results_html(html: str) -> list[NcSighting]:
    """Parse NC results HTML into a list of NcSighting instances.

    The HTML structure has:
    - Region headers (blue rows): "The Americas", "Europe", "Asia"
    - Sub-headers: "In-Stock Inventory", "Brokered Inventory Listings"
    - Data rows with columns: Part | icons | Mfr | DC | Desc | Uploaded | Ctr | Qty | icons | Supplier

    This parser is defensive — malformed rows are skipped with a warning.
    """
    from bs4 import BeautifulSoup

    if not html or not html.strip():
        return []

    soup = BeautifulSoup(html, "html.parser")
    sightings = []
    current_region = ""
    current_inventory_type = "in_stock"

    # Find all table rows
    rows = soup.find_all("tr")
    if not rows:
        # Try div-based layout as fallback
        rows = soup.find_all("div", class_=re.compile(r"row|result", re.I))

    for row in rows:
        text = row.get_text(strip=True).lower()

        # Region header detection
        if any(r in text for r in ["the americas", "americas"]):
            current_region = "The Americas"
            continue
        if "europe" in text:
            current_region = "Europe"
            continue
        if "asia" in text:
            current_region = "Asia"
            continue

        # Inventory type sub-header detection
        if "in-stock" in text or "in stock" in text:
            current_inventory_type = "in_stock"
            continue
        if "brokered" in text:
            current_inventory_type = "brokered"
            continue

        # Skip header/nav rows
        if "part number" in text and "supplier" in text:
            continue

        # Try to parse as a data row
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        try:
            # Extract cell text, stripping whitespace
            cell_texts = [c.get_text(strip=True) for c in cells]

            # Map columns based on NC table structure
            # Columns: Part | (icons) | Mfr | DC | Desc | Uploaded | Ctr | Qty | (icons) | Supplier
            part_number = cell_texts[0] if len(cell_texts) > 0 else ""
            manufacturer = cell_texts[2] if len(cell_texts) > 2 else ""
            date_code = cell_texts[3] if len(cell_texts) > 3 else ""
            description = cell_texts[4] if len(cell_texts) > 4 else ""
            uploaded_date = cell_texts[5] if len(cell_texts) > 5 else ""
            country = cell_texts[6] if len(cell_texts) > 6 else ""
            qty_text = cell_texts[7] if len(cell_texts) > 7 else ""
            vendor_name = cell_texts[9] if len(cell_texts) > 9 else (cell_texts[-1] if cell_texts else "")

            # Check for sponsor badge
            is_sponsor = bool(row.find(class_=re.compile(r"sponsor", re.I)))

            # Check for authorized badge
            is_authorized = bool(
                row.find(class_=re.compile(r"authorized|auth-badge", re.I))
                or row.find("img", alt=re.compile(r"authorized", re.I))
            )

            sighting = NcSighting(
                part_number=part_number,
                manufacturer=manufacturer,
                date_code=date_code,
                description=description,
                uploaded_date=uploaded_date,
                country=country,
                quantity=parse_quantity(qty_text),
                vendor_name=vendor_name,
                region=current_region,
                inventory_type=current_inventory_type,
                is_sponsor=is_sponsor,
                is_authorized=is_authorized,
            )
            sightings.append(sighting)

        except (IndexError, AttributeError) as e:
            logger.debug("NC parser: skipping malformed row: {}", e)
            continue

    logger.info("NC parser: extracted {} sightings from HTML", len(sightings))
    return sightings
