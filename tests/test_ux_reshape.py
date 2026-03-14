"""
tests/test_ux_reshape.py — Tests for UX reshape phases 2-10.

Validates: Coverage columns, blocker indicators, deal board, nerve center,
proactive board enhancements, buy plan execution board, intake integration.

Called by: pytest
Depends on: app/templates/index.html, app/static/app.js, app/static/crm.js, app/static/styles.css
"""

import subprocess

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def index_html():
    with open("app/templates/index.html", "r") as f:
        return f.read()


@pytest.fixture(scope="module")
def app_js():
    with open("app/static/app.js", "r") as f:
        return f.read()


@pytest.fixture(scope="module")
def crm_js():
    with open("app/static/crm.js", "r") as f:
        return f.read()


@pytest.fixture(scope="module")
def styles_css():
    with open("app/static/styles.css", "r") as f:
        return f.read()


# ── JS Syntax ─────────────────────────────────────────────────────────────


class TestJSSyntax:
    def test_app_js_parses(self):
        result = subprocess.run(["node", "-c", "app/static/app.js"], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"

    def test_crm_js_parses(self):
        result = subprocess.run(["node", "-c", "app/static/crm.js"], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"


# ── Phase 2: Coverage Columns & Blocker Indicators ─────────────────────


class TestCoverageColumns:
    def test_coverage_sort_case(self, app_js):
        assert "case 'coverage':" in app_js

    def test_coverage_header_in_sales(self, app_js):
        assert "sortReqList('coverage')" in app_js

    def test_coverage_bar_in_sales_cells(self, app_js):
        assert "_covPct" in app_js

    def test_coverage_bar_in_sourcing_cells(self, app_js):
        assert "_srcCovPct" in app_js

    def test_blocker_chip_in_rows(self, app_js):
        assert "blocker-chip" in app_js
        assert "blockerChip" in app_js

    def test_blocker_chip_css(self, styles_css):
        assert ".blocker-chip" in styles_css
        assert ".blocker-chip.warn" in styles_css


# ── Phase 3: Requirement Workspace ────────────────────────────────────


class TestRequirementWorkspace:
    def test_status_strip_in_drilldown(self, app_js):
        """Drill-down includes status strip with coverage metrics."""
        assert "renderStatusStrip(statusItems)" in app_js

    def test_blocker_strip_in_drilldown(self, app_js):
        """Drill-down includes blocker strip for deadline urgency."""
        assert "renderBlockerStrip(blockers)" in app_js

    def test_mobile_drilldown_status_strip(self, app_js):
        """Mobile drill-down includes status strip."""
        # renderStatusStrip is called in mobile drill-down
        assert "renderStatusStrip([" in app_js


# ── Phase 4: Material Item Workspace ──────────────────────────────────


class TestMaterialWorkspace:
    def test_material_uses_obj_header(self, app_js):
        assert "renderObjHeader({" in app_js

    def test_material_uses_status_strip(self, app_js):
        # Used for material popup stats
        assert "renderStatusStrip([" in app_js

    def test_material_ai_card(self, app_js):
        """Material popup includes AI supply intelligence card."""
        assert "Supply Intelligence" in app_js
        assert "_supplyHealth" in app_js


# ── Phase 5: Deal Board ──────────────────────────────────────────────


class TestDealBoard:
    def test_deals_pill_in_html(self, index_html):
        assert 'data-view="deals"' in index_html

    def test_render_deal_board_function(self, app_js):
        assert "function _renderDealBoard()" in app_js

    def test_deal_stages_defined(self, app_js):
        for stage in ["gathering", "rfq-out", "offers-in", "quoting", "closing"]:
            assert f"key: '{stage}'" in app_js

    def test_deal_board_css(self, styles_css):
        assert ".deal-board" in styles_css
        assert ".deal-col" in styles_css
        assert ".deal-card" in styles_css
        assert ".deal-card-bar" in styles_css

    def test_deal_view_in_setMainView(self, app_js):
        assert "'deals'" in app_js
        assert "_renderDealBoard()" in app_js

    def test_deal_board_responsive(self, styles_css):
        """Deal board goes vertical on mobile."""
        assert ".deal-board { flex-direction: column" in styles_css or ".deal-board{flex-direction:column" in styles_css


# ── Phase 6: Nerve Center ────────────────────────────────────────────


class TestNerveCenter:
    def test_nerve_feed_css(self, styles_css):
        assert ".nerve-feed" in styles_css
        assert ".nerve-feed-item" in styles_css
        assert ".nerve-feed-deadline" in styles_css
        assert ".nerve-feed-offers" in styles_css
        assert ".nerve-feed-quote" in styles_css


# ── Phase 7: Proactive Opportunity Board ──────────────────────────────


class TestProactiveBoard:
    def test_proactive_uses_status_strip(self, crm_js):
        assert "renderStatusStrip" in crm_js

    def test_proactive_ai_card(self, crm_js):
        assert "Opportunity Insight" in crm_js
        assert "renderAiCard" in crm_js


# ── Phase 8: Buy Plan Execution Board ────────────────────────────────


class TestBuyPlanExecution:
    def test_buyplan_summary_stats(self, crm_js):
        """Buy plans list has summary status strip with color/label helpers."""
        assert "_bpStatusColor" in crm_js
        assert "_bpStatusLabel" in crm_js

    def test_buyplan_blocker_strip(self, crm_js):
        """Buy plans show halted status and issue flagging UI."""
        assert "halted" in crm_js
        assert "openFlagIssue" in crm_js

    def test_buyplan_uses_status_strip(self, crm_js):
        assert "renderStatusStrip" in crm_js


# ── Phase 9: Intake Integration ──────────────────────────────────────


class TestIntakeIntegration:
    def test_intake_views_include_buyplans(self, app_js):
        assert "'view-buyplans'" in app_js
        assert "intakeViews" in app_js

    def test_intake_materials_search_fallback(self, app_js):
        """Intake on materials view searches for MPNs instead of adding requirements."""
        assert "materialSearch" in app_js
        assert "Searching" in app_js
