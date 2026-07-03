"""Tests for the canonical `activity_row(a)` Jinja macro (Phase B1).

Phase B1 consolidates the five divergent per-row activity-timeline renderings
(requisition / parts / sightings / vendors / customers) into ONE canonical
macro `activity_row(a)` in shared/_macros.html using the rich icon style.

These tests:
  * render the macro directly and assert the icon + actor + summary + channel
    badge + relative-time contract;
  * assert vendor attribution uses VendorCard.display_name (VendorCard has no
    `name` attribute);
  * assert the shared activity_timeline.html and vendors/contact_timeline.html
    surfaces now emit the canonical icon markup (the h-8 w-8 rounded icon from
    activity_icon) that they did NOT emit before the migration.

Depends on: app.template_env.templates (canonical Jinja env with custom filters).
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.template_env import templates

UTC_NOW = datetime.now(timezone.utc)


def _render_row(a) -> str:
    """Render the activity_row macro directly via a tiny wrapper template."""
    tmpl = templates.env.from_string(
        '{% from "htmx/partials/shared/_macros.html" import activity_row %}{{ activity_row(a) }}'
    )
    return tmpl.render(a=a)


def _vendor_activity(**overrides):
    """A vendor-linked activity SimpleNamespace with sensible defaults."""
    base = dict(
        activity_type="email_sent",
        channel="email",
        contact_name=None,
        vendor_card_id=7,
        vendor_card=SimpleNamespace(display_name="Arrow Electronics"),
        summary="Sent RFQ for STM32F407 to vendor",
        notes=None,
        direction=None,
        details=None,
        occurred_at=UTC_NOW,
        created_at=UTC_NOW,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Macro contract ────────────────────────────────────────────────────────


def test_activity_row_renders_icon_actor_summary_time():
    """The macro renders the unified icon, actor, summary, channel badge and a relative-
    time string — and never stringifies a dict."""
    a = _vendor_activity()
    html = _render_row(a)

    # Actor (vendor display_name) and summary text.
    assert "Arrow Electronics" in html
    assert "Sent RFQ for STM32F407 to vendor" in html
    # Unified icon (svg inside the h-8 w-8 rounded circle from activity_icon).
    assert "<svg" in html
    assert "h-8 w-8" in html
    # Channel badge — capitalized.
    assert "Email" in html
    # Relative-time string from the |timeago filter.
    assert "ago" in html or "just now" in html
    # Never stringify a dict anywhere.
    assert "{'" not in html
    assert "': '" not in html


def test_activity_row_vendor_uses_display_name_not_name():
    """Vendor attribution reads VendorCard.display_name (VendorCard has no `name`); a
    fallback `name` attribute must be ignored."""
    a = _vendor_activity(
        vendor_card=SimpleNamespace(display_name="Mouser Inc"),
    )
    html = _render_row(a)
    assert "Mouser Inc" in html


def test_activity_row_falls_back_to_contact_name_without_vendor():
    """With no vendor_card link, the actor falls back to contact_name."""
    a = _vendor_activity(
        vendor_card_id=None,
        vendor_card=None,
        contact_name="Jane Buyer",
    )
    html = _render_row(a)
    assert "Jane Buyer" in html


def test_activity_row_hides_system_channel_badge():
    """A 'system' channel is not surfaced as a pill badge."""
    a = _vendor_activity(channel="system")
    html = _render_row(a)
    assert "System" not in html


# ── Surface migration: shared/activity_timeline.html (parts + sightings) ──


def test_shared_timeline_now_renders_canonical_icon_markup():
    """The shared timeline (sightings + parts) now emits the canonical h-8 w-8 rounded
    icon from activity_icon — it previously rendered a small w-2 h-2 dot instead."""
    tmpl = templates.env.get_template("htmx/partials/shared/activity_timeline.html")
    a = _vendor_activity(summary="Vendor replied with stock")
    html = tmpl.render(activities=[a])

    assert "h-8 w-8" in html
    assert "<svg" in html
    assert "Vendor replied with stock" in html
    assert "Arrow Electronics" in html
    # The old per-row dot marker is gone.
    assert "w-2 h-2 rounded-full bg-brand-500" not in html


def test_shared_timeline_keeps_empty_state():
    """The shared timeline preserves its empty state after migration."""
    tmpl = templates.env.get_template("htmx/partials/shared/activity_timeline.html")
    html = tmpl.render(activities=[])
    assert "No activity yet" in html


# ── Surface migration: vendors/contact_timeline.html ──────────────────────


def test_contact_timeline_now_renders_canonical_icon_markup():
    """The vendor contact timeline now emits the canonical icon markup."""
    tmpl = templates.env.get_template("htmx/partials/vendors/contact_timeline.html")
    a = _vendor_activity(summary="Called about pricing", channel="phone")
    contact = SimpleNamespace(full_name="Dana Vendor", email="dana@arrow.com")
    html = tmpl.render(activities=[a], contact=contact, vendor_id=1)

    assert "h-8 w-8" in html
    assert "<svg" in html
    assert "Called about pricing" in html
    # Header chrome is preserved.
    assert "Dana Vendor" in html


def test_contact_timeline_keeps_empty_state():
    """The vendor contact timeline preserves its empty state after migration."""
    tmpl = templates.env.get_template("htmx/partials/vendors/contact_timeline.html")
    contact = SimpleNamespace(full_name="Dana Vendor", email="dana@arrow.com")
    html = tmpl.render(activities=[], contact=contact, vendor_id=1)
    assert "No activity recorded for this contact." in html


# ── Direction pill + call-outcome badge (little-thing #4) ──────────────────


def test_direction_pill_outbound_shows_out():
    """Outbound call_logged row renders an 'Out' direction pill (slate)."""
    a = _vendor_activity(
        activity_type="call_logged",
        channel="phone",
        direction="outbound",
        details={"call_outcome": "connected"},
    )
    html = _render_row(a)
    assert "Out" in html
    assert "bg-slate-50" in html
    assert "text-slate-600" in html


def test_direction_pill_outbound_with_connected_outcome():
    """Outbound call_logged row with connected outcome renders both 'Out' pill and
    'Connected' badge."""
    a = _vendor_activity(
        activity_type="call_logged",
        channel="phone",
        direction="outbound",
        details={"call_outcome": "connected"},
    )
    html = _render_row(a)
    assert "Out" in html
    assert "Connected" in html
    assert "bg-emerald-50" in html


def test_direction_pill_inbound_shows_in():
    """Inbound call row renders an 'In' direction pill (emerald)."""
    a = _vendor_activity(
        activity_type="call_logged",
        channel="phone",
        direction="inbound",
        details=None,
    )
    html = _render_row(a)
    assert "In" in html
    # inbound direction uses emerald
    assert "bg-emerald-50" in html


def test_call_outcome_left_message():
    """left_message outcome renders 'Left msg' badge (sky)."""
    a = _vendor_activity(
        activity_type="call_logged",
        direction="outbound",
        details={"call_outcome": "left_message"},
    )
    html = _render_row(a)
    assert "Left msg" in html
    assert "bg-sky-50" in html


def test_call_outcome_voicemail():
    """Voicemail outcome renders 'Voicemail' badge (amber)."""
    a = _vendor_activity(
        activity_type="call_logged",
        direction="outbound",
        details={"call_outcome": "voicemail"},
    )
    html = _render_row(a)
    assert "Voicemail" in html
    assert "bg-amber-50" in html


def test_call_outcome_no_answer():
    """no_answer outcome renders 'No answer' badge (gray)."""
    a = _vendor_activity(
        activity_type="call_logged",
        direction="outbound",
        details={"call_outcome": "no_answer"},
    )
    html = _render_row(a)
    assert "No answer" in html
    assert "bg-gray-50" in html


def test_no_direction_no_details_renders_cleanly():
    """A row with no direction and None details renders no direction pill and no outcome
    badge."""
    a = _vendor_activity(direction=None, details=None)
    html = _render_row(a)
    # Must not crash and must not emit direction/outcome markup
    assert "bg-slate-50" not in html
    assert "Connected" not in html
    assert "Left msg" not in html
    assert "Voicemail" not in html
    assert "No answer" not in html
    # The row itself renders normally
    assert "Arrow Electronics" in html


def test_meeting_row_outbound_shows_out_pill():
    """A meeting activity with direction outbound surfaces the 'Out' direction pill."""
    a = _vendor_activity(
        activity_type="meeting_logged",
        channel="manual",
        direction="outbound",
        details=None,
    )
    html = _render_row(a)
    assert "Out" in html
    assert "bg-slate-50" in html
    # No outcome badge when details is None
    assert "Connected" not in html
