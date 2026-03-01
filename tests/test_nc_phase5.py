"""Tests for NC Phase 5: Search Engine, Results Parser, Sighting Writer.

Called by: pytest
Depends on: conftest.py, nc_worker modules
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.models import NcSearchQueue, Requirement, Sighting
from app.services.nc_worker.result_parser import NcSighting, parse_quantity, parse_results_html
from app.services.nc_worker.search_engine import build_search_url
from app.services.nc_worker.sighting_writer import save_nc_sightings


# ── Search Engine Tests ──────────────────────────────────────────────


def test_build_search_url_basic():
    """Build URL with simple MPN."""
    url = build_search_url("STM32F103C8T6")
    assert "PartsSearched%5B0%5D.PartNumber=STM32F103C8T6" in url
    assert "Filters=true&Filters=false" in url  # ASP.NET checkbox binding
    assert "PSA=true&PSA=false" in url
    assert "SearchLogic=Begins" in url


def test_build_search_url_special_chars():
    """Build URL with special characters in MPN."""
    url = build_search_url("ADP3338AKCZ-3.3")
    assert "ADP3338AKCZ-3.3" in url


def test_build_search_url_spaces():
    """Build URL with spaces in MPN (gets URL-encoded)."""
    url = build_search_url("LM 317T")
    assert "LM%20317T" in url


# ── parse_quantity Tests ─────────────────────────────────────────────


def test_parse_quantity_plain():
    assert parse_quantity("208") == 208


def test_parse_quantity_comma():
    assert parse_quantity("10,000") == 10000


def test_parse_quantity_plus():
    assert parse_quantity("80,000+") == 80000


def test_parse_quantity_empty():
    assert parse_quantity("") is None
    assert parse_quantity(None) is None


def test_parse_quantity_whitespace():
    assert parse_quantity("  5,000  ") == 5000


def test_parse_quantity_invalid():
    assert parse_quantity("N/A") is None


# ── parse_results_html Tests ────────────────────────────────────────


SAMPLE_HTML = """
<table>
  <tr><td colspan="14" class="region-header">The Americas</td></tr>
  <tr><td colspan="14">In-Stock Inventory</td></tr>
  <tr><td>Part Number</td><td></td><td></td><td>Mfr</td><td>DC</td><td>Description</td><td>Uploaded</td><td>Ctr</td><td>Qty</td><td></td><td></td><td></td><td>Supplier</td><td></td></tr>
  <tr>
    <td>STM32F103C8T6</td>
    <td></td>
    <td></td>
    <td>STMicroelectronics</td>
    <td>2024+</td>
    <td>ARM MCU 64KB Flash</td>
    <td>01/15/2026</td>
    <td>US</td>
    <td>10,000</td>
    <td></td>
    <td></td>
    <td></td>
    <td>Arrow Electronics</td>
    <td></td>
  </tr>
  <tr>
    <td>STM32F103C8T6</td>
    <td></td>
    <td></td>
    <td>STMicroelectronics</td>
    <td>2023+</td>
    <td>ARM MCU 64KB Flash</td>
    <td>02/01/2026</td>
    <td>CN</td>
    <td>5,000+</td>
    <td></td>
    <td></td>
    <td></td>
    <td>Shenzhen Parts Co</td>
    <td></td>
  </tr>
  <tr><td colspan="14" class="region-header">Europe</td></tr>
  <tr><td colspan="14">Brokered Inventory Listings</td></tr>
  <tr>
    <td>STM32F103C8T6</td>
    <td></td>
    <td></td>
    <td>ST</td>
    <td></td>
    <td>MCU</td>
    <td>12/20/2025</td>
    <td>IL</td>
    <td>208</td>
    <td></td>
    <td></td>
    <td></td>
    <td>Euro Broker GmbH</td>
    <td></td>
  </tr>
</table>
"""


def test_parse_results_html_full():
    """Parse sample HTML with multiple regions and inventory types."""
    sightings = parse_results_html(SAMPLE_HTML)
    assert len(sightings) == 3

    s1 = sightings[0]
    assert s1.part_number == "STM32F103C8T6"
    assert s1.manufacturer == "STMicroelectronics"
    assert s1.date_code == "2024+"
    assert s1.country == "US"
    assert s1.quantity == 10000
    assert s1.vendor_name == "Arrow Electronics"
    assert s1.region == "The Americas"
    assert s1.inventory_type == "in_stock"

    s2 = sightings[1]
    assert s2.quantity == 5000
    assert s2.country == "CN"

    s3 = sightings[2]
    assert s3.region == "Europe"
    assert s3.inventory_type == "brokered"
    assert s3.quantity == 208


def test_parse_results_html_empty():
    """Empty HTML returns empty list."""
    assert parse_results_html("") == []
    assert parse_results_html("   ") == []


def test_parse_results_html_no_data_rows():
    """HTML with no data rows returns empty list."""
    html = "<table><tr><td>No results found</td></tr></table>"
    assert parse_results_html(html) == []


# ── Sighting Writer Tests ───────────────────────────────────────────


def test_save_nc_sightings(db_session, test_requisition):
    """save_nc_sightings creates sighting records from NcSighting list."""
    req = test_requisition.requirements[0]
    queue_item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM317T",
        normalized_mpn="LM317T",
        status="searching",
    )
    db_session.add(queue_item)
    db_session.commit()

    nc_sightings = [
        NcSighting(
            part_number="LM317T",
            manufacturer="Texas Instruments",
            date_code="2024+",
            description="Voltage Regulator",
            uploaded_date="01/15/2026",
            country="US",
            quantity=5000,
            vendor_name="Arrow Electronics",
            region="The Americas",
            inventory_type="in_stock",
        ),
        NcSighting(
            part_number="LM317T",
            manufacturer="TI",
            quantity=1000,
            vendor_name="Broker Co",
            region="Europe",
            inventory_type="brokered",
        ),
    ]

    created = save_nc_sightings(db_session, queue_item, nc_sightings)
    assert created == 2

    sightings = db_session.query(Sighting).filter(
        Sighting.requirement_id == req.id,
        Sighting.source_type == "netcomponents",
    ).all()
    assert len(sightings) == 2
    assert sightings[0].source_searched_at is not None
    assert sightings[0].vendor_name_normalized is not None


def test_save_nc_sightings_dedup(db_session, test_requisition):
    """Duplicate vendor+mpn+qty combos are not created twice."""
    req = test_requisition.requirements[0]
    queue_item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM317T",
        normalized_mpn="LM317T",
        status="searching",
    )
    db_session.add(queue_item)
    db_session.commit()

    nc_sightings = [
        NcSighting(part_number="LM317T", quantity=5000, vendor_name="Arrow"),
        NcSighting(part_number="LM317T", quantity=5000, vendor_name="Arrow"),  # duplicate
    ]

    created = save_nc_sightings(db_session, queue_item, nc_sightings)
    assert created == 1  # Only one created, duplicate skipped


def test_save_nc_sightings_empty_vendor_skipped(db_session, test_requisition):
    """Sightings with empty vendor_name are skipped."""
    req = test_requisition.requirements[0]
    queue_item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM317T",
        normalized_mpn="LM317T",
        status="searching",
    )
    db_session.add(queue_item)
    db_session.commit()

    nc_sightings = [
        NcSighting(part_number="LM317T", quantity=5000, vendor_name=""),
    ]

    created = save_nc_sightings(db_session, queue_item, nc_sightings)
    assert created == 0
