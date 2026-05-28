"""test_materials_tab_render.py — Render each materials-tab partial with a concrete row
so the quantity/price format filters actually execute.

Purpose: catch printf-vs-str.format mismatches in templates that the empty-list
    coverage in test_template_compilation cannot reach (because `{% for %}`
    over an empty list never runs the loop body where the buggy filter lives).
Called by: pytest.
Depends on: jinja2, the shared template_env.

Regression background: 2026-05-27 prod 500 on /v2/partials/materials/1/tab/vendors
caused by `"{:,}"|format(qty)` — Jinja2's `format` filter is printf-style, so
`"{:,}"` has zero `%s`/`%d` placeholders, raising `TypeError: not all arguments
converted during string formatting`. The fix swaps the filter for a direct
`.format()` method call. These tests pin that contract.
"""

from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


@pytest.fixture(scope="module")
def jinja_env() -> Environment:
    """A Jinja2 env pointed at app/templates with the same autoescape policy."""
    return Environment(
        loader=FileSystemLoader("app/templates"),
        autoescape=select_autoescape(["html"]),
    )


def _ns(**kw):
    return SimpleNamespace(**kw)


@pytest.mark.parametrize(
    "template,ctx",
    [
        (
            "htmx/partials/materials/tabs/vendors.html",
            {
                "vendors": [
                    _ns(
                        vendor_name="ACME",
                        is_authorized=True,
                        last_price=1.2345,
                        last_qty=12_345,
                        last_currency="USD",
                        first_seen=None,
                        last_seen=None,
                        times_seen=3,
                        vendor_sku="SKU-1",
                    )
                ],
                "card": _ns(id=1),
            },
        ),
        (
            "htmx/partials/materials/tabs/customers.html",
            {
                "customers": [
                    _ns(
                        company=_ns(name="BigCo"),
                        purchase_count=4,
                        total_quantity=99_999,
                        avg_unit_price=2.5678,
                        last_purchased_at=None,
                    )
                ],
                "card": _ns(id=1),
            },
        ),
        (
            "htmx/partials/materials/tabs/sourcing.html",
            {
                "requirements": [
                    _ns(
                        id=1,
                        requisition_id=10,
                        primary_mpn="MPN-1",
                        target_qty=5_000,
                        target_price=0.4321,
                        created_at=None,
                        # sourcing_status is used as a dict key (st_colors.get)
                        # so it must be hashable — a plain string is fine.
                        sourcing_status="open",
                        requisition=_ns(id=10, title="Req 10"),
                    )
                ],
                "card": _ns(id=1),
            },
        ),
        (
            "htmx/partials/materials/tabs/price_history.html",
            {
                "sightings": [
                    _ns(
                        vendor_name="ACME",
                        price=9.8765,
                        quantity=1_000,
                        currency="USD",
                        source="bb",
                        seen_at=None,
                    )
                ],
                "card": _ns(id=1),
            },
        ),
    ],
    ids=["vendors", "customers", "sourcing", "price_history"],
)
def test_materials_tab_renders_with_concrete_row(jinja_env, template, ctx):
    """Renders the tab partial with a row carrying non-null qty + price.

    If a future template change re-introduces `"{:,}"|format(X)` (printf filter
    over a str.format string), `X` truthy will raise TypeError here.
    """
    try:
        jinja_env.get_template(template).render(**ctx)
    except TypeError as e:
        pytest.fail(
            f"TypeError rendering {template} with a non-null qty/price row: {e}. "
            f'Check the template for `"{{:,}}"|format(...)` (printf filter over a '
            f'str.format string) — use `"{{:,}}".format(...)` instead.'
        )
