"""test_quote_source_badge.py — Render tests for the Proactive badge in quote detail.

Covers:
- quote.source == 'proactive' → "Proactive" badge rendered
- quote.source != 'proactive' (None or other) → badge absent
"""

import os

import pytest

os.environ["TESTING"] = "1"

from jinja2 import Environment

from app.template_env import templates


@pytest.fixture(scope="module")
def jinja_env() -> Environment:
    # P5.1: quotes/detail.html now imports shared/_macros.html (for the
    # lazy_body macro) — that file's other macros use custom filters
    # (|timeago, etc.) that Jinja validates at import/compile time, not just
    # call time, so a bare Environment(loader=FileSystemLoader(...)) with no
    # filters registered fails to even compile the template. Reuse the app's
    # real singleton template environment (app.template_env.templates.env) so
    # this test always has the same filters/globals production rendering does.
    return templates.env


class _FakeQuote:
    """Minimal duck-typed Quote for template rendering."""

    def __init__(self, source=None, status="draft"):
        self.id = 1
        self.quote_number = "Q-2026-9999"
        self.revision = 1
        self.status = status
        self.source = source
        self.customer_site = None
        self.requisition = None
        self.created_by = None
        self.created_at = None
        self.subtotal = None
        self.total_cost = None
        self.total_margin_pct = None
        self.followup_alert_sent_at = None


class TestProactiveBadge:
    """The Proactive badge in quotes/detail.html."""

    def _render(self, env: Environment, source=None, status="won") -> str:
        tpl = env.get_template("htmx/partials/quotes/detail.html")
        quote = _FakeQuote(source=source, status=status)
        return tpl.render(quote=quote, lines=[], offers=[])

    def test_badge_shown_for_proactive_source(self, jinja_env):
        html = self._render(jinja_env, source="proactive")
        assert "Proactive" in html

    def test_badge_absent_for_null_source(self, jinja_env):
        html = self._render(jinja_env, source=None)
        # The word "Proactive" should not appear (except possibly in the title)
        # Check the badge span itself is absent
        assert "text-violet-700" not in html

    def test_badge_absent_for_manual_source(self, jinja_env):
        html = self._render(jinja_env, source="manual")
        assert "text-violet-700" not in html
