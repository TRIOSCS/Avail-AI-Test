"""
test_htmx_mobile.py — Tests for Phase 3 Task 11: Mobile Optimization.
Verifies responsive-table classes on list partials, data-label attributes on row
templates, viewport meta tag in base.html, mobile nav rendering, drawer mobile
classes, and htmx_mobile.css content.
Called by: pytest
Depends on: app/templates/, app/static/htmx_mobile.css
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

import pytest
from jinja2 import Environment, FileSystemLoader


@pytest.fixture()
def jinja_env():
    """Jinja2 environment pointing at the app templates directory."""
    return Environment(loader=FileSystemLoader("app/templates"))


@pytest.fixture()
def mobile_css():
    """Read the htmx_mobile.css file content."""
    with open("app/static/htmx_mobile.css") as f:
        return f.read()


class TestMobileMetaTags:
    """Verify base.html includes viewport meta tag and mobile CSS."""

    def test_viewport_meta_tag_present(self, jinja_env):
        template = jinja_env.get_template("base.html")
        html = template.render()
        assert 'name="viewport"' in html
        assert "width=device-width" in html
        assert "initial-scale=1.0" in html

    def test_htmx_mobile_css_included(self, jinja_env):
        template = jinja_env.get_template("base.html")
        html = template.render()
        assert "htmx_mobile.css" in html


class TestMobileNavRenders:
    """Mobile nav partial contains correct links and structure."""

    def test_mobile_nav_has_four_main_items(self, jinja_env):
        template = jinja_env.get_template("partials/shared/mobile_nav.html")
        html = template.render()
        assert "/requisitions" in html
        assert "/companies" in html
        assert "/vendors" in html
        assert "/quotes" in html

    def test_mobile_nav_has_more_button(self, jinja_env):
        template = jinja_env.get_template("partials/shared/mobile_nav.html")
        html = template.render()
        assert "More" in html

    def test_mobile_nav_more_menu_contains_overflow_items(self, jinja_env):
        template = jinja_env.get_template("partials/shared/mobile_nav.html")
        html = template.render()
        assert "/buy-plans" in html
        assert "/prospecting" in html
        assert "/settings" in html

    def test_mobile_nav_has_mobile_nav_class(self, jinja_env):
        template = jinja_env.get_template("partials/shared/mobile_nav.html")
        html = template.render()
        assert 'class="mobile-nav"' in html

    def test_mobile_nav_items_have_htmx_attrs(self, jinja_env):
        template = jinja_env.get_template("partials/shared/mobile_nav.html")
        html = template.render()
        assert 'hx-boost="true"' in html
        assert 'hx-target="#main-content"' in html

    def test_mobile_nav_max_five_visible_items(self, jinja_env):
        """4 nav links + 1 More button = 5 items max visible."""
        template = jinja_env.get_template("partials/shared/mobile_nav.html")
        html = template.render()
        # Count mobile-nav-item occurrences (4 links + 1 button)
        assert html.count('class="mobile-nav-item"') == 5


class TestResponsiveTableClasses:
    """All list partials include the responsive-table class on their tables."""

    @pytest.mark.parametrize(
        "template_path",
        [
            "partials/requisitions/list.html",
            "partials/companies/list.html",
            "partials/vendors/list.html",
            "partials/quotes/list.html",
            "partials/buy_plans/list.html",
        ],
    )
    def test_list_partial_has_responsive_table(self, template_path):
        """Each list template must have 'responsive-table' on its data-table."""
        with open(f"app/templates/{template_path}") as f:
            content = f.read()
        assert "responsive-table" in content, (
            f"{template_path} missing responsive-table class"
        )


class TestDataLabelAttributes:
    """Row templates include data-label attributes for mobile card layout."""

    @pytest.mark.parametrize(
        "template_path,expected_labels",
        [
            (
                "partials/requisitions/req_row.html",
                ["Name", "Customer", "Status", "Parts", "Created"],
            ),
            (
                "partials/companies/company_row.html",
                ["Name", "Owner", "Sites", "Open Reqs"],
            ),
            (
                "partials/vendors/vendor_row.html",
                ["Name", "Health Score", "Contacts", "Last Activity"],
            ),
            (
                "partials/quotes/quote_row.html",
                ["Quote #", "Customer", "Lines", "Total", "Status", "Date"],
            ),
            (
                "partials/buy_plans/buy_plan_row.html",
                ["Name", "Customer", "Lines", "Total", "Status", "Submitted By", "Date"],
            ),
        ],
    )
    def test_row_template_has_data_labels(self, template_path, expected_labels):
        with open(f"app/templates/{template_path}") as f:
            content = f.read()
        for label in expected_labels:
            assert f'data-label="{label}"' in content, (
                f"{template_path} missing data-label=\"{label}\""
            )


class TestMobileCssContent:
    """Verify htmx_mobile.css contains key responsive rules."""

    def test_has_responsive_table_media_query(self, mobile_css):
        assert "@media (max-width: 768px)" in mobile_css

    def test_hides_thead_on_mobile(self, mobile_css):
        assert ".responsive-table thead" in mobile_css
        assert "display: none" in mobile_css

    def test_cards_from_table_rows(self, mobile_css):
        assert ".responsive-table tr" in mobile_css
        assert "display: block" in mobile_css

    def test_data_label_pseudo_element(self, mobile_css):
        assert "attr(data-label)" in mobile_css

    def test_touch_target_min_height(self, mobile_css):
        assert "min-height: 44px" in mobile_css

    def test_drawer_slides_from_bottom(self, mobile_css):
        assert "translateY(100%)" in mobile_css

    def test_modal_fullscreen_on_mobile(self, mobile_css):
        assert ".modal-box" in mobile_css

    def test_sidebar_hidden_on_mobile(self, mobile_css):
        assert ".sidebar" in mobile_css

    def test_mobile_more_menu_styles(self, mobile_css):
        assert ".mobile-more-menu" in mobile_css
        assert ".mobile-more-item" in mobile_css

    def test_bottom_nav_fixed_position(self, mobile_css):
        assert ".mobile-nav" in mobile_css
        assert "position: fixed" in mobile_css
        assert "bottom: 0" in mobile_css

    def test_drawer_back_button(self, mobile_css):
        assert ".drawer-back-btn" in mobile_css

    def test_drawer_swipe_handle(self, mobile_css):
        """Drawer has a swipe handle indicator via ::before pseudo-element."""
        assert ".drawer-header::before" in mobile_css
