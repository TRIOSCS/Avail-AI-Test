"""test_quote_source_badge.py — Render tests for the Proactive badge in quote detail.

Covers:
- quote.source == 'proactive' → "Proactive" badge rendered
- quote.source != 'proactive' (None or other) → badge absent
"""

import os

import pytest

os.environ["TESTING"] = "1"

from jinja2 import Environment, FileSystemLoader

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE_DIR = os.path.join(_REPO_ROOT, "app", "templates")


@pytest.fixture(scope="module")
def jinja_env():
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=True,
    )
    return env


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
        assert "bg-violet-100" not in html

    def test_badge_absent_for_manual_source(self, jinja_env):
        html = self._render(jinja_env, source="manual")
        assert "bg-violet-100" not in html
