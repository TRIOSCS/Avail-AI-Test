"""test_contacts_ui_compact.py — TDD tests for compact contacts table UI.

Verifies:
  1. Contacts grouped-list renders compact table rows (not card markup).
  2. Phone renders as tel: link with data-outreach-log attributes.
  3. Email renders as Outlook compose deeplink with data-outreach-log.
  4. Per-row expand drawer contains wechat/teams/notes fields.
  5. Edit modal surfaces all known contact fields, including blanks.

Called by: pytest
Depends on: app.template_env.templates (Jinja2 env), app.models.crm stubs
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

from app.template_env import templates

ENV = templates.env


def _make_contact(**kwargs):
    """Build a simple namespace object mimicking SiteContact for template rendering."""
    defaults = {
        "id": 1,
        "full_name": "Alice Chen",
        "first_name": "Alice",
        "last_name": "Chen",
        "title": "Senior Buyer",
        "email": "alice@acme.com",
        "phone": "+15551234567",
        "wechat_id": None,
        "linkedin_url": None,
        "notes": None,
        "is_primary": False,
        "is_priority": False,
        "is_archived": False,
        "do_not_contact": False,
        "contact_role": "buyer_po",
        "last_outbound_at": None,
        "last_reply_at": None,
        "last_activity_at": None,
        "customer_site_id": 10,
        "secondary_email": None,
        "secondary_phone": None,
        "reports_to": None,
        "reports_to_id": None,
        "custom_fields": {},
        "enrichment_source": None,
        "contact_status": "new",
        "phone_verified": False,
        "email_verified": False,
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _make_site(**kwargs):
    """Build a simple namespace object mimicking CustomerSite."""
    defaults = {
        "id": 10,
        "site_name": "Acme HQ",
        "city": "San Jose",
        "last_outbound_at": None,
        "contact_name": None,
        "contact_email": None,
        "contact_phone": None,
        "contact_title": None,
        "site_type": "hq",
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _make_company(**kwargs):
    """Build a simple namespace object mimicking Company."""
    defaults = {
        "id": 42,
        "name": "Acme Electronics",
        "domain": "acme.com",
        "website": "https://acme.com",
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _render_grouped_list(contact_rows, company=None, now_utc=None, roles=None):
    """Render the _contacts_grouped_list.html template with given data."""
    company = company or _make_company()
    now_utc = now_utc or datetime.now(timezone.utc)
    roles = roles or ("specifier", "buyer_po", "ap_payer", "logistics", "exec", "other")
    tpl = ENV.get_template("htmx/partials/customers/tabs/_contacts_grouped_list.html")
    return tpl.render(
        company=company,
        contact_rows=contact_rows,
        now_utc=now_utc,
        roles=roles,
        active_sites=[],
    )


def _render_contact_form(contact=None, site=None, company=None, mode="edit", sites=None, roles=None):
    """Render the _contact_form.html template."""
    company = company or _make_company()
    roles = roles or ("specifier", "buyer_po", "ap_payer", "logistics", "exec", "other")
    tpl = ENV.get_template("htmx/partials/customers/tabs/_contact_form.html")
    ctx = {
        "company": company,
        "contact": contact,
        "site": site,
        "mode": mode,
        "roles": roles,
    }
    if sites is not None:
        ctx["sites"] = sites
    return tpl.render(**ctx)


class TestContactsRenderCompactColumns:
    """The grouped list must render a table with column headers, not card markup."""

    def test_contacts_renders_table_structure(self):
        """Grouped list renders <table> with a thead row."""
        contact = _make_contact()
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "<table" in html or "<thead" in html, "Expected table structure in contacts list"
        assert "<thead" in html

    def test_contacts_renders_name_column_header(self):
        contact = _make_contact()
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        # Column header for Name
        assert "Name" in html

    def test_contacts_renders_phone_column_header(self):
        contact = _make_contact()
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "Phone" in html

    def test_contacts_renders_email_column_header(self):
        contact = _make_contact()
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "Email" in html

    def test_contacts_renders_last_contact_column_header(self):
        contact = _make_contact()
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        # "Last" appears in "Last Contact" header
        assert "Last" in html

    def test_contacts_no_card_class(self):
        """New compact layout must not use the old card div classes."""
        contact = _make_contact()
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "contact-card" not in html

    def test_contacts_renders_compact_table_class(self):
        """Table must use the .compact-table design-system class."""
        contact = _make_contact()
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "compact-table" in html

    def test_contacts_renders_contact_name(self):
        contact = _make_contact(full_name="Alice Chen")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "Alice Chen" in html

    def test_primary_star_shown_for_primary_contact(self):
        """Primary contacts must show the ★ star indicator."""
        contact = _make_contact(is_primary=True)
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "★" in html

    def test_empty_state_renders_when_no_rows(self):
        """Empty contact_rows must render the empty state, not a table."""
        html = _render_grouped_list([])
        assert "<table" not in html or "No contacts" in html


class TestPhoneRendersTelLink:
    """Phone must render as a tel: link with outreach logging attributes."""

    def test_phone_renders_tel_href(self):
        contact = _make_contact(phone="+15551234567")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "tel:+15551234567" in html

    def test_phone_has_data_outreach_log(self):
        contact = _make_contact(phone="+15551234567")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "data-outreach-log" in html

    def test_phone_has_channel_phone(self):
        contact = _make_contact(phone="+15551234567")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        # Single or double quotes are both valid HTML for attributes
        assert "data-channel='phone'" in html or 'data-channel="phone"' in html

    def test_no_phone_no_tel_link(self):
        contact = _make_contact(phone=None)
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "tel:" not in html


class TestEmailRendersOutlookCompose:
    """Email must render as Outlook compose deeplink with outreach logging."""

    def test_email_renders_outlook_deeplink(self):
        contact = _make_contact(email="alice@acme.com")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "outlook.office.com/mail/deeplink/compose" in html

    def test_email_has_data_outreach_log(self):
        contact = _make_contact(email="alice@acme.com")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "data-outreach-log" in html

    def test_email_has_channel_email(self):
        contact = _make_contact(email="alice@acme.com")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        # Single or double quotes are both valid HTML for attributes
        assert "data-channel='email'" in html or 'data-channel="email"' in html

    def test_email_opens_in_new_tab(self):
        contact = _make_contact(email="alice@acme.com")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert 'target="_blank"' in html

    def test_no_email_no_outlook_link(self):
        contact = _make_contact(email=None)
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "outlook.office.com" not in html


class TestExpandDrawerContainsWechatTeamsNotes:
    """Per-row expand drawer must expose wechat, teams, and notes fields."""

    def test_expand_drawer_exists_with_alpine_toggle(self):
        """The row expand mechanism uses Alpine x-show or similar."""
        contact = _make_contact(wechat_id="alice_wechat", notes="Some notes")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        # Alpine expand toggle must be present
        assert "x-show" in html or "x-data" in html

    def test_expand_drawer_contains_wechat_when_set(self):
        contact = _make_contact(wechat_id="alice_wechat")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "alice_wechat" in html or "WeChat" in html

    def test_expand_drawer_contains_teams_deeplink(self):
        contact = _make_contact(email="alice@acme.com")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "teams.microsoft.com" in html

    def test_expand_drawer_contains_notes_when_set(self):
        contact = _make_contact(notes="Call on Tuesdays")
        site = _make_site()
        rows = [{"contact": contact, "site": site, "legacy": False, "cadence": "new"}]
        html = _render_grouped_list(rows)
        assert "Call on Tuesdays" in html


class TestEditModalSurfacesBlankFields:
    """Edit form must show all known contact fields, including blank ones."""

    def test_edit_form_shows_wechat_field(self):
        contact = _make_contact(wechat_id=None)
        site = _make_site()
        html = _render_contact_form(contact=contact, site=site, mode="edit")
        assert "wechat_id" in html or "WeChat" in html

    def test_edit_form_shows_linkedin_field(self):
        contact = _make_contact(linkedin_url=None)
        site = _make_site()
        html = _render_contact_form(contact=contact, site=site, mode="edit")
        assert "linkedin_url" in html or "LinkedIn" in html

    def test_edit_form_shows_notes_field(self):
        contact = _make_contact(notes=None)
        site = _make_site()
        html = _render_contact_form(contact=contact, site=site, mode="edit")
        assert "notes" in html or "Notes" in html

    def test_edit_form_shows_secondary_email_field(self):
        contact = _make_contact(secondary_email=None)
        site = _make_site()
        html = _render_contact_form(contact=contact, site=site, mode="edit")
        assert "secondary_email" in html or "Secondary Email" in html

    def test_edit_form_shows_secondary_phone_field(self):
        contact = _make_contact(secondary_phone=None)
        site = _make_site()
        html = _render_contact_form(contact=contact, site=site, mode="edit")
        assert "secondary_phone" in html or "Secondary Phone" in html

    def test_edit_form_shows_phone_field(self):
        contact = _make_contact(phone=None)
        site = _make_site()
        html = _render_contact_form(contact=contact, site=site, mode="edit")
        assert "phone" in html or "Phone" in html

    def test_edit_form_shows_email_field(self):
        contact = _make_contact(email=None)
        site = _make_site()
        html = _render_contact_form(contact=contact, site=site, mode="edit")
        assert "email" in html or "Email" in html

    def test_edit_form_shows_title_field(self):
        contact = _make_contact(title=None)
        site = _make_site()
        html = _render_contact_form(contact=contact, site=site, mode="edit")
        assert "title" in html or "Title" in html

    def test_edit_form_prefills_existing_values(self):
        contact = _make_contact(email="alice@acme.com", wechat_id="alice_wechat")
        site = _make_site()
        html = _render_contact_form(contact=contact, site=site, mode="edit")
        assert "alice@acme.com" in html
        assert "alice_wechat" in html
