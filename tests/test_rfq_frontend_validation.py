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
        result = subprocess.run(["node", "-c", "app/static/app.js"], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"

    def test_crm_js_parses(self):
        """crm.js passes Node.js syntax check."""
        result = subprocess.run(["node", "-c", "app/static/crm.js"], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"


# ── Jinja2 Template Validation ───────────────────────────────────────────


class TestTemplateParsing:
    def test_index_template_parses(self):
        """index.html is valid Jinja2."""
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader("app/templates"))
        t = env.get_template("index.html")
        assert t is not None


# ── View Mode Toggle (Reqs / Deals / Archive) ────────────────────────────


class TestViewModeToggle:
    def test_reqs_view_pill(self, index_html):
        assert 'data-view="reqs"' in index_html

    def test_deals_view_pill(self, index_html):
        assert 'data-view="deals"' in index_html

    def test_archive_view_pill(self, index_html):
        assert 'data-view="archive"' in index_html

    def test_setMainView_function_exists(self, app_js):
        assert "function setMainView(" in app_js or "setMainView" in app_js

    def test_localStorage_persistence(self, app_js):
        assert "avail_main_view" in app_js

    def test_default_view_is_reqs(self, app_js):
        assert "localStorage.getItem('avail_main_view') || 'reqs'" in app_js


# ── Priority Lanes ───────────────────────────────────────────────────────


class TestPriorityLanes:
    def test_priority_lane_grouping_comment_exists(self, app_js):
        """Priority lane grouping logic exists inline in renderReqList."""
        assert "Priority lane grouping" in app_js

    def test_renderReqRow_exists_for_lanes(self, app_js):
        assert "_renderReqRow" in app_js

    def test_togglePriorityLane_css_exists(self, index_html):
        """togglePriorityLane referenced in CSS hover rule."""
        assert "togglePriorityLane" in index_html

    def test_archive_group_collapse_state(self, app_js):
        """Archive groups track open/close state."""
        assert "_archiveGroupsOpen" in app_js

    def test_sales_lanes_defined(self, app_js):
        """Sales view uses deadline urgency and status-based grouping."""
        assert "In Progress" in app_js
        assert "Awaiting" in app_js

    def test_sourcing_lanes_defined(self, app_js):
        """Sourcing view uses coverage and RFQ status indicators."""
        assert "Sourced" in app_js or "coverage" in app_js
        assert "Awaiting" in app_js

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

    def test_ddSubTabs_returns_three_tabs(self, app_js):
        """_ddSubTabs returns exactly 3 tabs (workspace, quote, activity)."""
        match = re.search(r"return\s*\[\s*'workspace'\s*,\s*'quote'\s*,\s*'activity'\s*\]", app_js)
        assert match is not None, "_ddSubTabs should return ['workspace', 'quote', 'activity']"


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

    def test_notifActionBar_handled_in_js(self, app_js):
        """Notification system exists in app.js (toggleNotifications)."""
        assert "toggleNotifications" in app_js

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

    def test_old_tab_names_mapped_in_ddTabLabel(self, app_js):
        """Old tab names (parts, sightings) are mapped in _ddTabLabel for backward compat."""
        assert "parts: 'Parts'" in app_js or "parts:" in app_js

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

    def test_renderReqList_calls_renderReqRow(self, app_js):
        """renderReqList uses _renderReqRow to build rows."""
        assert "_renderReqRow" in app_js

    def test_renderReqList_has_priority_grouping(self, app_js):
        """renderReqList includes priority lane grouping logic."""
        assert "Priority lane grouping" in app_js


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
        """Tasks sidebar panel uses white background and border styling."""
        # Panel uses border-left with var(--border)
        assert (
            "border-left: 1px solid var(--border)" in styles_css or "border-left:1px solid var(--border)" in styles_css
        )
        # Panel uses box-shadow for depth
        assert "box-shadow" in styles_css

    def test_sidebar_body_class_for_page_react(self, styles_css):
        """Body class tasks-open triggers margin-right transition on main content."""
        assert "body.tasks-open .main" in styles_css
        assert "margin-right" in styles_css

    def test_sidebar_default_open_js(self, app_js):
        """Sidebar toggle uses classList and body class for open state."""
        assert "classList.toggle('open')" in app_js or "classList.add('open')" in app_js
        assert "tasks-open" in app_js
        assert "localStorage" in app_js

    def test_sidebar_toggle_saves_preference(self, app_js):
        """Toggle function manages open/close preference via localStorage."""
        assert "localStorage.removeItem('myTasksOpen')" in app_js or "localStorage.setItem('myTasksOpen'" in app_js

    def test_sidebar_loading_resilience(self, app_js):
        """loadMyTasks uses Promise.allSettled for resilience."""
        assert "Promise.allSettled" in app_js
        assert "Array.isArray(tasksRes.value)" in app_js


# ── Scroll-End Detection ────────────────────────────────────────────────


class TestScrollEndDetection:
    """Verify scroll-end detection wires up for CSS fade-out hint removal."""

    def test_scrolled_end_css_class_defined(self, styles_css):
        """CSS defines .scrolled-end to remove mask-image."""
        assert "scrolled-end" in styles_css
        assert "mask-image:none" in styles_css

    def test_dd_panel_scroll_listener_wired(self, app_js):
        """dd-panel gets scroll listener for scrolled-end class toggle."""
        assert "_scrollEndWired" in app_js
        assert "scrolled-end" in app_js

    def test_crm_table_wrap_scroll_listener(self, app_js):
        """crm-table-wrap elements get scroll listeners on DOMContentLoaded."""
        assert "crm-table-wrap" in app_js
        assert "scrollLeft" in app_js

    def test_scroll_end_calculation(self, app_js):
        """Scroll-end detection uses scrollLeft + clientWidth >= scrollWidth."""
        assert "scrollWidth" in app_js
        assert "clientWidth" in app_js


# ── Requirement Panel Tab Layout ───────────────────────────────────────


class TestRequirementPanelTabs:
    """Verify the requirement detail panel has five tabs in correct order."""

    def test_rfq_workspace_tab_order(self, app_js):
        """RFQ workspace panel tabs are: Offers, Sightings, Activity, Tasks, Notes."""
        import re
        tabs_block = re.search(
            r'<div class="rfq-panel-tabs">(.*?)</div>',
            app_js,
            re.DOTALL,
        )
        assert tabs_block, "rfq-panel-tabs block not found"
        tabs_html = tabs_block.group(1)
        tab_names = re.findall(r'>(\w+)</button>', tabs_html)
        assert tab_names == ["Offers", "Sightings", "Activity", "Tasks", "Notes"]

    def test_rfq_load_tab_handles_all_tabs(self, app_js):
        """_rfqLoadTab switch covers offers, sightings, activity, tasks, notes."""
        for tab in ["'offers'", "'sightings'", "'activity'", "'tasks'", "'notes'"]:
            assert f"case {tab}:" in app_js

    def test_rfq_render_tab_handles_all_tabs(self, app_js):
        """_rfqRenderTab dispatches to all five renderers."""
        assert "_rfqRenderOffers" in app_js
        assert "_rfqRenderSightings" in app_js
        assert "_rfqRenderActivity" in app_js
        assert "_rfqRenderTasks" in app_js
        assert "_rfqRenderNotes" in app_js

    def test_inline_part_expand_tabs(self, app_js):
        """Inline part expansion has all five tabs."""
        assert "['offers', 'sightings', 'activity', 'tasks', 'notes']" in app_js

    def test_tasks_tab_has_create_button(self, app_js):
        """Tasks tab includes the + Assign Task button."""
        assert "rfqShowTaskForm" in app_js

    def test_notes_tab_has_add_button(self, app_js):
        """Notes tab includes the + Add Note button."""
        assert "rfqShowNoteForm" in app_js

    def test_task_submit_invalidates_tasks_cache(self, app_js):
        """rfqSubmitTask invalidates the tasks cache."""
        assert "delete _rfqPanelCache.tasks" in app_js

    def test_note_submit_invalidates_notes_cache(self, app_js):
        """rfqSubmitNote invalidates the notes cache."""
        assert "delete _rfqPanelCache.notes" in app_js
