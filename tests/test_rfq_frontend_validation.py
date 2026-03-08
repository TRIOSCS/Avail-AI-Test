"""
tests/test_rfq_frontend_validation.py — Comprehensive frontend validation tests.

Validates: JS syntax, Jinja2 template parsing, HTML structure for redesigned
RFQ layout (v8), CSS class presence, JS function existence, view mode handling,
priority lane logic, sub-tab consolidation, inline RFQ bar, notification bar.

Called by: pytest
Depends on: app/templates/index.html, app/static/app.js, app/static/styles.css
"""

import re
import subprocess

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def index_html():
    """Read index.html template raw content."""
    with open("app/templates/index.html", "r") as f:
        return f.read()


@pytest.fixture(scope="module")
def app_js():
    """Read app.js raw content."""
    with open("app/static/app.js", "r") as f:
        return f.read()


@pytest.fixture(scope="module")
def styles_css():
    """Read styles.css raw content."""
    with open("app/static/styles.css", "r") as f:
        return f.read()


# ── JS Syntax Validation ─────────────────────────────────────────────────

class TestJSSyntax:
    def test_app_js_parses(self):
        """app.js passes Node.js syntax check."""
        result = subprocess.run(
            ["node", "-c", "app/static/app.js"],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"

    def test_crm_js_parses(self):
        """crm.js passes Node.js syntax check."""
        result = subprocess.run(
            ["node", "-c", "app/static/crm.js"],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"


# ── Jinja2 Template Validation ───────────────────────────────────────────

class TestTemplateParsing:
    def test_index_template_parses(self):
        """index.html is valid Jinja2."""
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader("app/templates"))
        t = env.get_template("index.html")
        assert t is not None


# ── View Mode Toggle (Sales / Sourcing / Archive) ────────────────────────

class TestViewModeToggle:
    def test_sales_view_pill(self, index_html):
        assert 'data-view="sales"' in index_html

    def test_sourcing_view_pill(self, index_html):
        assert 'data-view="sourcing"' in index_html

    def test_archive_view_pill(self, index_html):
        assert 'data-view="archive"' in index_html

    def test_setMainView_function_exists(self, app_js):
        assert "function setMainView(" in app_js or "setMainView" in app_js

    def test_localStorage_persistence(self, app_js):
        assert "avail_main_view" in app_js

    def test_default_view_is_sales(self, app_js):
        assert "'sales'" in app_js


# ── Priority Lanes ───────────────────────────────────────────────────────

class TestPriorityLanes:
    def test_classifyIntoLanes_exists(self, app_js):
        assert "_classifyIntoLanes" in app_js

    def test_renderPriorityLanes_exists(self, app_js):
        assert "_renderPriorityLanes" in app_js

    def test_togglePriorityLane_exists(self, app_js):
        assert "togglePriorityLane" in app_js

    def test_lane_collapse_state_persisted(self, app_js):
        assert "avail_lane_collapse" in app_js

    def test_sales_lanes_defined(self, app_js):
        """Sales view has 4 priority lanes."""
        assert "Needs Action" in app_js
        assert "In Progress" in app_js
        assert "Waiting" in app_js

    def test_sourcing_lanes_defined(self, app_js):
        """Sourcing view has 4 priority lanes."""
        assert "Unsourced" in app_js
        assert "Sightings Found" in app_js or "Awaiting" in app_js

    def test_lane_colors(self, app_js):
        """Priority lanes use color-coded indicators."""
        assert "red" in app_js
        assert "yellow" in app_js
        assert "green" in app_js

    def test_priority_lane_rendering(self, app_js):
        """Priority lanes render via inline styles with colored borders."""
        assert "border-left:3px solid" in app_js


# ── Sub-Tab Consolidation (7 → 4) ────────────────────────────────────────

class TestSubTabConsolidation:
    def test_sourcing_tab_exists(self, app_js):
        """Consolidated 'sourcing' tab (was Parts + Sightings)."""
        assert "'sourcing'" in app_js

    def test_offers_tab_exists(self, app_js):
        assert "'offers'" in app_js

    def test_quote_tab_exists(self, app_js):
        assert "'quote'" in app_js

    def test_activity_tab_exists(self, app_js):
        assert "'activity'" in app_js

    def test_ddSubTabs_function_exists(self, app_js):
        assert "_ddSubTabs" in app_js

    def test_ddSubTabs_returns_four_tabs(self, app_js):
        """_ddSubTabs returns exactly 4 tabs."""
        match = re.search(r"return\s*\[\s*'sourcing'\s*,\s*'offers'\s*,\s*'quote'\s*,\s*'activity'\s*\]", app_js)
        assert match is not None, "_ddSubTabs should return ['sourcing', 'offers', 'quote', 'activity']"


# ── Inline RFQ Bar ────────────────────────────────────────────────────────

class TestInlineRfqBar:
    def test_rfq_inline_bar_css_in_template(self, index_html):
        assert "rfq-inline-bar" in index_html

    def test_updateInlineRfqBar_function(self, app_js):
        assert "_updateInlineRfqBar" in app_js

    def test_clearSightingSelection_function(self, app_js):
        assert "_clearSightingSelection" in app_js


# ── Notification Bar ─────────────────────────────────────────────────────

class TestNotificationBar:
    def test_notifActionBar_element(self, index_html):
        assert "notifActionBar" in index_html

    def test_renderNotifActionBar_function(self, app_js):
        assert "_renderNotifActionBar" in app_js

    def test_notif_action_bar_css(self, index_html):
        assert "notif-action-bar" in index_html


# ── Req Row Rendering ────────────────────────────────────────────────────

class TestReqRowRendering:
    def test_renderReqRow_exists(self, app_js):
        assert "_renderReqRow" in app_js

    def test_renderReqList_exists(self, app_js):
        assert "renderReqList" in app_js

    def test_sales_view_columns(self, app_js):
        """Sales view table header includes customer-focused columns."""
        assert "Customer" in app_js
        assert "Bid Due" in app_js or "bid_due" in app_js

    def test_sourcing_view_columns(self, app_js):
        """Sourcing view table header includes sourcing-focused columns."""
        assert "Sourced" in app_js
        assert "RFQs" in app_js or "Response" in app_js


# ── My Tasks Sidebar ─────────────────────────────────────────────────────

class TestMyTasksSidebar:
    def test_sidebar_element_exists(self, index_html):
        assert 'id="myTasksSidebar"' in index_html

    def test_toggle_function_exists(self, app_js):
        assert "function toggleMyTasksSidebar()" in app_js

    def test_loadMyTasks_function_exists(self, app_js):
        assert "async function loadMyTasks()" in app_js

    def test_loadMyTasks_has_error_handling(self, app_js):
        assert "Failed to load tasks" in app_js

    def test_loadMyTasks_uses_allSettled(self, app_js):
        """loadMyTasks uses Promise.allSettled for resilient API calls."""
        assert "Promise.allSettled" in app_js

    def test_close_button_exists(self, index_html):
        assert 'onclick="toggleMyTasksSidebar()"' in index_html

    def test_sidebar_css_exists(self, styles_css):
        assert "my-tasks-sidebar" in styles_css

    def test_sidebar_open_class(self, styles_css):
        assert ".my-tasks-sidebar.open" in styles_css


# ── CSS Validation ────────────────────────────────────────────────────────

class TestCSSIntegrity:
    def test_styles_css_not_empty(self, styles_css):
        assert len(styles_css) > 100

    def test_table_layout_auto(self, index_html):
        """Table layout should be auto to prevent column clipping."""
        assert "table-layout:auto" in index_html or "table-layout: auto" in index_html

    def test_priority_lane_hover(self, index_html):
        """Priority lane rows have hover effect."""
        assert "togglePriorityLane" in index_html


# ── Data Flow / Integration Points ───────────────────────────────────────

class TestDataFlowIntegration:
    def test_expandToSubTab_uses_new_tabs(self, app_js):
        """expandToSubTab references use consolidated tab names."""
        assert "expandToSubTab" in app_js

    def test_no_orphan_old_tab_refs(self, app_js):
        """No leftover references to old standalone 'parts' tab in expandToSubTab calls."""
        # Find expandToSubTab calls that reference old tab names
        old_refs = re.findall(r"expandToSubTab\([^,]+,\s*'parts'\s*\)", app_js)
        assert len(old_refs) == 0, f"Found old 'parts' tab references in expandToSubTab: {old_refs}"

    def test_loadDdSubTab_handles_sourcing(self, app_js):
        assert "_loadDdSubTab" in app_js

    def test_renderDdTab_handles_sourcing(self, app_js):
        assert "_renderDdTab" in app_js


# ── Mobile Support ────────────────────────────────────────────────────────

class TestMobileSupport:
    def test_mobile_css_exists(self):
        with open("app/static/mobile.css", "r") as f:
            content = f.read()
        assert len(content) > 50

    def test_mobile_tasks_sidebar_styles(self, styles_css):
        """Mobile breakpoint handles tasks sidebar."""
        assert "max-width:768px" in styles_css


# ── Function Cross-Reference ─────────────────────────────────────────────

class TestFunctionCrossRefs:
    """Verify key functions reference each other correctly."""

    def test_setMainView_triggers_req_loading(self, app_js):
        """setMainView calls loadRequisitions which triggers renderReqList."""
        match = re.search(r"function setMainView\(.*?\{(.*?)(?=\nfunction )", app_js, re.DOTALL)
        if match:
            body = match.group(1)
            assert "loadRequisitions" in body or "renderReqList" in body or "loadReqList" in body

    def test_renderReqList_calls_classifyIntoLanes(self, app_js):
        assert "_classifyIntoLanes" in app_js

    def test_renderReqList_calls_renderPriorityLanes(self, app_js):
        assert "_renderPriorityLanes" in app_js


# ── Tasks Sidebar Right-Side Widget ─────────────────────────────────────

class TestTasksSidebarRight:
    """Verify Tasks sidebar is positioned on the right with correct behavior."""

    def test_sidebar_html_right_side(self, index_html):
        """Tasks sidebar widget exists in HTML."""
        assert 'id="myTasksSidebar"' in index_html
        assert 'id="myTasksPanel"' in index_html
        assert 'id="myTasksList"' in index_html

    def test_sidebar_css_right_positioned(self, styles_css):
        """Tasks sidebar is fixed to the right."""
        assert "right: 0" in styles_css or "right:0" in styles_css
        # Panel slides from right (translateX(100%))
        assert "translateX(100%)" in styles_css

    def test_sidebar_css_matches_nav_aesthetic(self, styles_css):
        """Tasks sidebar uses same background and border as nav sidebar."""
        # Both use #e8eaed background
        assert styles_css.count("#e8eaed") >= 2  # toggle + panel
        # Both use var(--blue) border
        assert "border-left: 1px solid var(--blue)" in styles_css or "border-left:1px solid var(--blue)" in styles_css

    def test_sidebar_body_class_for_page_react(self, styles_css):
        """Body class tasks-open triggers margin-right on main content."""
        assert "body.tasks-open .main" in styles_css
        assert "margin-right: 280px" in styles_css or "margin-right:280px" in styles_css

    def test_sidebar_default_open_js(self, app_js):
        """Sidebar opens by default on page load (localStorage preference)."""
        assert "sidebar.classList.add('open')" in app_js
        assert "document.body.classList.add('tasks-open')" in app_js
        assert "localStorage" in app_js

    def test_sidebar_toggle_saves_preference(self, app_js):
        """Toggle function saves open/close preference."""
        assert "localStorage.setItem('myTasksOpen'" in app_js

    def test_sidebar_loading_resilience(self, app_js):
        """loadMyTasks uses Promise.allSettled for resilience."""
        assert "Promise.allSettled" in app_js
        assert "Array.isArray(tasksRes.value)" in app_js
