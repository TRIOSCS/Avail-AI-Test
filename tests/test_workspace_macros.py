"""tests/test_workspace_macros.py — Tests for the copy_chip / age_chip shared macros
(Phase 0.4, app/templates/htmx/partials/shared/_macros.html).

Renders through the app's real Jinja2 environment (app.template_env.templates.env) so
filter/global registration (timeago, fmtdate, now) is exercised, not mocked.
"""

import re
from datetime import UTC, datetime, timedelta

from app.template_env import templates

_IMPORT = "{% from 'htmx/partials/shared/_macros.html' import copy_chip, age_chip %}"


def _render(snippet: str, **context) -> str:
    return templates.env.from_string(_IMPORT + snippet).render(**context)


class TestCopyChip:
    def test_renders_button_with_value(self):
        html = _render("{{ copy_chip('SO-12345') }}")
        assert '<button type="button"' in html
        assert 'data-copy-value="SO-12345"' in html
        assert ">SO-12345</span>" in html  # label defaults to value

    def test_custom_label(self):
        html = _render("{{ copy_chip('PO-9', label='PO PO-9') }}")
        assert 'data-copy-value="PO-9"' in html
        assert ">PO PO-9</span>" in html

    def test_secure_context_guard_and_prompt_fallback(self):
        html = _render("{{ copy_chip('SO-1') }}")
        assert "navigator.clipboard && window.isSecureContext" in html
        assert "window.prompt(" in html  # HTTP / old-browser fallback

    def test_copied_flash_and_toast_fields_set_directly(self):
        html = _render("{{ copy_chip('SO-1') }}")
        assert ">Copied</span>" in html
        assert 'x-data="{ copied: false }"' in html
        # $store.toast fields are SET directly; show is a boolean, never a call.
        assert "$store.toast.show = true" in html
        assert "$store.toast.show(" not in html
        assert "$store.toast.message = 'Copied ' + v" in html

    def test_no_literal_double_quote_inside_double_quoted_attributes(self):
        # CLAUDE.md Alpine rule: a literal " inside a double-quoted attribute
        # closes it early and silently kills the whole component.
        html = _render("{{ copy_chip('SO-1') }}")
        for match in re.finditer(r'@click="([^"]*)"', html):
            handler = match.group(1)
            assert "&quot;" not in handler  # only single-quoted JS strings allowed
        # The @click attribute must terminate at the handler's closing brace,
        # not mid-JS — i.e. the extracted handler contains the full fallback.
        handler = re.search(r'@click="([^"]*)"', html).group(1)
        assert "window.prompt(" in handler

    def test_value_is_escaped_not_interpolated_into_js(self):
        html = _render("{{ copy_chip(value) }}", value='SO-1"onmouseover="alert(1)')
        assert 'data-copy-value="SO-1&#34;onmouseover=&#34;alert(1)"' in html
        assert '"onmouseover="alert' not in html  # autoescape holds

    def test_falsy_value_renders_nothing(self):
        assert _render("{{ copy_chip(None) }}").replace(_IMPORT, "").strip() == ""
        assert _render("{{ copy_chip('') }}").strip() == ""


class TestAgeChip:
    def test_fresh_is_emerald(self):
        dt = datetime.now(UTC) - timedelta(hours=2)
        html = _render("{{ age_chip(dt) }}", dt=dt)
        assert "bg-emerald-50" in html
        assert "2h ago" in html  # timeago filter output

    def test_amber_at_default_threshold(self):
        dt = datetime.now(UTC) - timedelta(days=4)
        html = _render("{{ age_chip(dt) }}", dt=dt)
        assert "bg-amber-50" in html
        assert "4d ago" in html

    def test_red_at_default_threshold(self):
        dt = datetime.now(UTC) - timedelta(days=8)
        html = _render("{{ age_chip(dt) }}", dt=dt)
        assert "bg-rose-50" in html

    def test_custom_thresholds(self):
        dt = datetime.now(UTC) - timedelta(days=2)
        html = _render("{{ age_chip(dt, amber_days=1, red_days=10) }}", dt=dt)
        assert "bg-amber-50" in html
        html = _render("{{ age_chip(dt, amber_days=1, red_days=2) }}", dt=dt)
        assert "bg-rose-50" in html

    def test_naive_datetime_assumed_utc(self):
        # SQLite rows hand back naive datetimes — must not raise, same bucket.
        dt = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
        html = _render("{{ age_chip(dt) }}", dt=dt)
        assert "bg-rose-50" in html

    def test_absolute_date_in_title(self):
        dt = datetime(2026, 7, 10, 9, 30, tzinfo=UTC)
        html = _render("{{ age_chip(dt) }}", dt=dt)
        assert 'title="Jul 10, 09:30"' in html  # |fmtdate default format

    def test_none_renders_nothing(self):
        assert _render("{{ age_chip(None) }}").strip() == ""
