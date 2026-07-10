"""Render tests for the CRM template-macro dedup (refactor/crm-template-macros).

The customer CRM partials used to hand-roll badge / icon / dual-clock markup that
the canonical macros in shared/_macros.html already provide, and the hand-rolled
copies had DRIFTED (e.g. quote status "sent" rendered amber on the quotes tab but
brand on the activity timeline). This suite locks in the centralization:

  * quote_status_badge / account_type_badge are thin wrappers over status_badge
    that keep the labels identical and unify the drifted colors;
  * cadence_clocks is the single dual-clock render shared by cadence_hero and the
    customer Account Cadence card;
  * the customer activity tab renders the canonical activity_icon (no inline
    icon_map copy), the quotes tab and detail card use the wrappers.

Macros are rendered directly via the canonical Jinja env (mirrors
test_activity_row_macro.py); the route-level surfaces are exercised in
test_crm_views.py against the authenticated TestClient.

Depends on: app.template_env.templates
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.template_env import templates

UTC_NOW = datetime.now(UTC)


def _render(macro: str, call: str, **ctx) -> str:
    tmpl = templates.env.from_string(f'{{% from "htmx/partials/shared/_macros.html" import {macro} %}}{{{{ {call} }}}}')
    return tmpl.render(**ctx)


# ── quote_status_badge ──────────────────────────────────────────────────────


def test_quote_status_badge_keeps_label_capitalized():
    """The badge label is the capitalized status (identical to the old span)."""
    assert "Sent" in _render("quote_status_badge", 'quote_status_badge("sent")')
    assert "Won" in _render("quote_status_badge", 'quote_status_badge("won")')
    assert "Lost" in _render("quote_status_badge", 'quote_status_badge("lost")')


def test_quote_status_badge_none_falls_back_to_draft():
    """A null status renders as Draft (matches the old `q.status or 'draft'`)."""
    assert "Draft" in _render("quote_status_badge", "quote_status_badge(None)")


def test_quote_status_badge_sent_is_brand_not_amber():
    """DRIFT FIX: 'sent' is brand everywhere now. The quotes tab previously rendered
    'sent' amber while the activity timeline rendered it brand — they are unified to
    the canonical brand color."""
    html = _render("quote_status_badge", 'quote_status_badge("sent")')
    assert "text-brand-700" in html
    assert "text-amber-700" not in html


def test_quote_status_badge_covers_rfq_statuses():
    """The same wrapper colors RFQ-contact statuses (the activity timeline renders RFQ
    and quote rows side by side)."""
    assert "text-rose-700" in _render("quote_status_badge", 'quote_status_badge("declined")')
    assert "text-amber-700" in _render("quote_status_badge", 'quote_status_badge("opened")')


# ── account_type_badge ──────────────────────────────────────────────────────


def test_account_type_badge_preserves_type_label():
    """Account-type label is preserved verbatim (case-sensitive type values)."""
    assert "Customer" in _render("account_type_badge", 'account_type_badge("Customer")')
    assert "Prospect" in _render("account_type_badge", 'account_type_badge("Prospect")')
    assert "Competitor" in _render("account_type_badge", 'account_type_badge("Competitor")')


def test_account_type_badge_colors_match_canonical_map():
    """Customer→emerald, Prospect→brand, Competitor→rose."""
    assert "emerald" in _render("account_type_badge", 'account_type_badge("Customer")')
    assert "brand" in _render("account_type_badge", 'account_type_badge("Prospect")')
    assert "rose" in _render("account_type_badge", 'account_type_badge("Competitor")')


# ── cadence_clocks ──────────────────────────────────────────────────────────


def test_cadence_clocks_renders_both_clock_labels():
    co = SimpleNamespace(last_outbound_at=UTC_NOW, last_reply_at=UTC_NOW)
    html = _render("cadence_clocks", "cadence_clocks(co, now_utc)", co=co, now_utc=UTC_NOW)
    assert "Last Out" in html
    assert "Last Reply" in html


def test_cadence_clocks_never_and_dash_for_nulls():
    """No outbound → 'Never'; no reply → em-dash (honest empty states preserved)."""
    co = SimpleNamespace(last_outbound_at=None, last_reply_at=None)
    html = _render("cadence_clocks", "cadence_clocks(co, now_utc)", co=co, now_utc=UTC_NOW)
    assert "Never" in html
    assert "—" in html


def test_cadence_hero_delegates_to_cadence_clocks():
    """cadence_hero renders the dual clocks via the shared macro (no duplicated clock
    markup) — badge + next-best-touch + both clock labels all present."""
    co = SimpleNamespace(last_outbound_at=UTC_NOW, last_reply_at=None)
    html = _render(
        "cadence_hero",
        'cadence_hero(co, "due", "Call this week", now_utc)',
        co=co,
        now_utc=UTC_NOW,
    )
    assert "Due" in html
    assert "Call this week" in html
    assert "Last Out" in html
    assert "Last Reply" in html


# ── activity_icon import in the customer activity tab ────────────────────────


def test_activity_tab_uses_canonical_activity_icon():
    """The customer activity tab uses the canonical activity_row macro (which itself
    uses activity_icon) and no longer carries an inline icon_map dict (the old hand-
    rolled copy).

    The import line must reference both macros.
    """
    src = templates.env.loader.get_source(templates.env, "htmx/partials/customers/tabs/activity_tab.html")[0]
    # activity_row is the canonical row macro — it wraps activity_icon internally
    assert "import activity_row" in src, "template must import the canonical activity_row macro"
    # activity_icon must also be imported (used for section headers); it appears on the
    # same {% from ... import activity_row, activity_icon %} line, so check as a token.
    assert "activity_icon" in src, "template must reference activity_icon (for section headers)"
    assert "icon_map" not in src, "inline icon_map duplicate should be gone"


def test_vendor_activity_tab_is_type_sectioned():
    """The vendor activity tab mirrors the account tab: it imports both canonical macros
    (activity_row for rows, activity_icon for the per-type section headers) and iterates
    the pre-bucketed `sections` dict rather than a single flat `activities` loop."""
    src = templates.env.loader.get_source(templates.env, "htmx/partials/vendors/tabs/activity_tab.html")[0]
    assert "import activity_row" in src, "template must import the canonical activity_row macro"
    assert "activity_icon" in src, "template must reference activity_icon (for section headers)"
    # Type-sectioned: drives off the route-built `sections` dict + _section_meta loop.
    assert "sections[section_label]" in src, "template must render per-type sections from the sections dict"
    assert "_section_meta" in src, "template must use the section metadata loop (Calls/Emails/...)"
    assert "icon_map" not in src, "no inline icon_map duplicate"
