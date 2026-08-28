"""tests/test_quote_copy_table_header.py — Copy Table header matches the real cell
order.

The Copy Table button copies the first 6 <td>s of each quote line row; its header line
must label them in the SAME order (MPN, Description, Manufacturer, Qty, Cost, Sell). The
pre-fix header omitted Description, shifting every label one column left — Cost values
pasted under "Sell" (a customer-facing cost leak when the paste left the building).

Also guards the "Copy for customer" sibling button (B5, Decision E): it must copy ONLY
cells 0,1,2,3,5 (MPN/Description/Manufacturer/Qty/Sell, labeled "Price") — no Cost, no
Margin — and both buttons must use the real global showToast() (htmx_app.js:84-89)
instead of the dead `CustomEvent('toast')` nothing ever listened for.
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


def test_customer_copy_button_omits_cost_and_margin():
    detail = DETAIL.read_text()
    # Isolate the customer-copy button's own @click block via its header-line
    # marker so we're not accidentally matching the internal Copy Table button's
    # "Cost" column (or the "Cost Price" / "Margin %" <th> labels elsewhere on
    # the page).
    header_marker = "let text = 'MPN\\tDescription\\tManufacturer\\tQty\\tPrice\\n';"
    assert header_marker in detail
    start = detail.index(header_marker)
    end = detail.index("Copy for customer", start)
    customer_block = detail[start:end]
    assert "Cost" not in customer_block
    assert "Margin" not in customer_block


def test_neither_copy_button_uses_dead_custom_event_toast():
    detail = DETAIL.read_text()
    assert "CustomEvent('toast'" not in detail
    assert 'CustomEvent("toast"' not in detail


def test_both_copy_buttons_call_the_real_show_toast():
    detail = DETAIL.read_text()
    assert detail.count("showToast(") >= 2
