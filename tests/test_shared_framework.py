"""
tests/test_shared_framework.py — Tests for shared UX framework primitives.

Validates: Context Panel, Universal Intake Bar, Object Page Components,
AI Summary Cards, Status Strips, Blocker Strips, Thread Items.

Called by: pytest
Depends on: app/templates/index.html, app/static/app.js, app/static/styles.css
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
def styles_css():
    with open("app/static/styles.css", "r") as f:
        return f.read()


# ── JS Syntax ─────────────────────────────────────────────────────────────


class TestJSSyntaxAfterFramework:
    def test_app_js_still_parses(self):
        """app.js passes Node.js syntax check after framework additions."""
        result = subprocess.run(["node", "-c", "app/static/app.js"], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"


# ── Context Panel ─────────────────────────────────────────────────────────


class TestContextPanel:
    def test_ctx_panel_html_exists(self, index_html):
        assert 'id="ctxPanel"' in index_html

    def test_ctx_toggle_button_exists(self, index_html):
        assert 'id="ctxToggle"' in index_html

    def test_ctx_tabs_exist(self, index_html):
        for tab in ["summary", "thread", "tasks", "files", "history"]:
            assert f'data-ctx-tab="{tab}"' in index_html, f"Missing ctx tab: {tab}"

    def test_ctx_body_placeholder(self, index_html):
        assert 'id="ctxBody"' in index_html

    def test_ctx_compose_area(self, index_html):
        assert 'id="ctxCompose"' in index_html
        assert 'id="ctxComposeInput"' in index_html

    def test_toggleContextPanel_function(self, app_js):
        assert "function toggleContextPanel()" in app_js

    def test_switchCtxTab_function(self, app_js):
        assert "function switchCtxTab(" in app_js

    def test_bindContextPanel_function(self, app_js):
        assert "function bindContextPanel(" in app_js

    def test_unbindContextPanel_function(self, app_js):
        assert "function unbindContextPanel()" in app_js

    def test_ctx_binds_on_drilldown(self, app_js):
        """Context panel binds when a requisition drill-down opens."""
        assert "bindContextPanel('requisition'" in app_js

    def test_ctx_unbinds_on_drilldown_close(self, app_js):
        """Context panel unbinds when drill-down closes."""
        assert "unbindContextPanel()" in app_js

    def test_ctx_panel_css(self, styles_css):
        assert ".ctx-panel" in styles_css
        assert ".ctx-panel.open" in styles_css
        assert "body.ctx-open .main" in styles_css

    def test_ctx_tabs_css(self, styles_css):
        assert ".ctx-tab" in styles_css
        assert ".ctx-tab.active" in styles_css

    def test_ctx_thread_compose_css(self, styles_css):
        assert ".thread-compose" in styles_css
        assert ".thread-item" in styles_css

    def test_ctx_hides_legacy_tasks(self, app_js):
        """Context panel hides legacy Tasks sidebar when open."""
        assert "myTasksSidebar" in app_js
        assert "tasks-open" in app_js


# ── Object Page Components ────────────────────────────────────────────────


class TestObjectPageComponents:
    def test_renderObjHeader_function(self, app_js):
        assert "function renderObjHeader(" in app_js

    def test_renderStatusStrip_function(self, app_js):
        assert "function renderStatusStrip(" in app_js

    def test_renderBlockerStrip_function(self, app_js):
        assert "function renderBlockerStrip(" in app_js

    def test_renderAiCard_function(self, app_js):
        assert "function renderAiCard(" in app_js

    def test_obj_header_css(self, styles_css):
        assert ".obj-header" in styles_css
        assert ".obj-header-title" in styles_css

    def test_status_strip_css(self, styles_css):
        assert ".status-strip" in styles_css
        assert ".status-strip-value" in styles_css

    def test_blocker_strip_css(self, styles_css):
        assert ".blocker-strip" in styles_css
        assert ".blocker-strip:empty" in styles_css

    def test_ai_card_css(self, styles_css):
        assert ".ai-card" in styles_css
        assert ".ai-card-confidence" in styles_css
        assert ".ai-card-action" in styles_css


# ── Follow-up Items ───────────────────────────────────────────────────────


class TestFollowupItems:
    def test_followup_css(self, styles_css):
        assert ".followup-item" in styles_css
        assert ".followup-check" in styles_css
        assert ".followup-check.done" in styles_css

    def test_thread_item_tags(self, styles_css):
        for tag in ["question", "decision", "blocker", "action"]:
            assert f".thread-item-tag.{tag}" in styles_css, f"Missing thread tag: {tag}"


# ── Action Bar ────────────────────────────────────────────────────────────


class TestActionBar:
    def test_action_bar_css(self, styles_css):
        assert ".action-bar" in styles_css
        assert "sticky" in styles_css


# ── Responsive ────────────────────────────────────────────────────────────


class TestContextPanelResponsive:
    def test_mobile_ctx_panel_full_width(self, styles_css):
        """Context panel goes full-width on mobile."""
        assert ".ctx-panel { width: 100vw" in styles_css or ".ctx-panel{width:100vw" in styles_css

    def test_mobile_intake_bar_margins(self, styles_css):
        """Intake bar has smaller margins on mobile."""
        assert ".intake-bar" in styles_css


# ── Window Exports ────────────────────────────────────────────────────────


class TestWindowExports:
    def test_context_panel_exported(self, app_js):
        """Context panel functions are exported to window."""
        for fn in ["toggleContextPanel", "switchCtxTab", "bindContextPanel", "unbindContextPanel"]:
            assert fn in app_js

    def test_intake_bar_exported(self, app_js):
        """Intake bar functions are exported to window."""
        for fn in ["showIntakeBar", "hideIntakeBar", "_intakeUpload", "_intakeClose", "_intakeConfirm"]:
            assert fn in app_js

    def test_shared_helpers_exported(self, app_js):
        """Shared page helper functions are exported to window."""
        for fn in ["renderObjHeader", "renderStatusStrip", "renderBlockerStrip", "renderAiCard"]:
            assert fn in app_js
