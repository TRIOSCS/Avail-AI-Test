"""Tests for the shared streaming-CSV export helper (app/utils/csv_export.py)."""

import csv
import io

from app.utils.csv_export import safe_cell, stream_csv


def test_safe_cell_none_and_plain():
    assert safe_cell(None) == ""
    assert safe_cell("ABC123") == "ABC123"
    assert safe_cell(42) == "42"


def test_safe_cell_neutralises_formula_injection():
    for trigger in ("=", "+", "-", "@"):
        assert safe_cell(f"{trigger}cmd()") == f"'{trigger}cmd()"
    # A hyphen inside (not leading) is untouched.
    assert safe_cell("PART-123") == "PART-123"


async def test_stream_csv_headers_and_rows():
    resp = stream_csv("sightings_export.csv", ["MPN", "Qty"], [["ABC", 5], ["=EVIL", 2]])
    assert resp.media_type == "text/csv"
    assert resp.headers["content-disposition"] == 'attachment; filename="sightings_export.csv"'
    chunks = []
    async for c in resp.body_iterator:
        chunks.append(c)
    text = "".join(chunks)
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["MPN", "Qty"]
    assert rows[1] == ["ABC", "5"]
    # Formula-injection cell is quoted.
    assert rows[2][0] == "'=EVIL"
