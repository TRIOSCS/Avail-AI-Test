"""tests/test_quote_copy_table_header.py — Copy Table header matches the real cell
order.

The Copy Table button copies the first 6 <td>s of each quote line row; its header line
must label them in the SAME order (MPN, Description, Manufacturer, Qty, Cost, Sell). The
pre-fix header omitted Description, shifting every label one column left — Cost values
pasted under "Sell" (a customer-facing cost leak when the paste left the building).
"""

from pathlib import Path

DETAIL = Path("app/templates/htmx/partials/quotes/detail.html")
LINE_ROW = Path("app/templates/htmx/partials/quotes/line_row.html")


def test_copy_header_matches_line_row_cells():
    detail = DETAIL.read_text()
    assert "let text = 'MPN\\tDescription\\tManufacturer\\tQty\\tCost\\tSell\\n';" in detail
    # The shifted pre-fix header must never come back.
    assert "'MPN\\tManufacturer\\tQty\\tCost\\tSell\\tMargin %" not in detail
    # Sanity: line_row's display cells still start MPN → Description → Manufacturer
    # → Qty → Cost → Sell (cost/sell recognizable by their format calls, in order).
    line_row = LINE_ROW.read_text()
    cost_pos = line_row.find("line.cost_price|float")
    sell_pos = line_row.find("line.sell_price|float")
    mfr_pos = line_row.find("line.manufacturer")
    assert -1 < mfr_pos < cost_pos < sell_pos
