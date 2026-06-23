"""The Broker Forum (TBF) results HTML parser — PHASE 1 STUB.

Parses the HTML returned by a TBF search into structured TbfSighting
dataclass instances. The TbfSighting shape is final; the per-row column
selectors are unknown until a logged-in capture exists, so
``parse_results_html`` returns ``[]`` for now.

Phase 2: capture authenticated results HTML on the host, encode the real
column selectors below, and add tests/fixtures/tbf_*.html assertions.

Called by: worker loop (after search_engine)
Depends on: beautifulsoup4
"""

from dataclasses import dataclass

from loguru import logger


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


def parse_results_html(html: str) -> list[TbfSighting]:
    """Parse TBF results HTML into a list of TbfSighting instances.

    PHASE 1: returns ``[]`` — the real per-row column selectors require a
    logged-in capture that does not exist yet. The defensive per-row skip
    structure below is the Phase-2 scaffold.

    This parser is defensive — malformed rows are skipped with a warning once
    selectors are encoded.
    """
    from bs4 import BeautifulSoup  # noqa: F401  (kept so the dep is wired for Phase 2)

    if not html or not html.strip():
        return []

    sightings: list[TbfSighting] = []

    # TODO(phase2): real selector from logged-in capture.
    # Iterate the results container, extract one TbfSighting per listing row,
    # wrapping each row body in try/except (IndexError, AttributeError) and
    # logging a debug skip on malformed rows. Until the capture exists this
    # returns no rows so the worker ships DORMANT.
    logger.debug("TBF parser: phase-1 stub — selectors not yet encoded, returning 0 sightings")
    return sightings
