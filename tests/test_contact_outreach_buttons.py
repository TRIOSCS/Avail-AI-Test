"""Render tests for the outreach_btn macro in _contact_macros.html.

Verifies:
  1. Email buttons use the Outlook on the web compose URL (not mailto:) with target="_blank".
  2. WeChat copy=True renders a <button> with data-outreach-log, data-value, navigator.clipboard
     @click, and NO weixin:// href attribute.
  3. Phone (tel:) and Teams (https://teams.microsoft.com) buttons are unchanged.

Called by: pytest
Depends on: app.template_env.templates (Jinja2 env with urlencode filter + cadence_state global)
"""

from app.template_env import templates

ENV = templates.env

_MACRO_IMPORT = '{% from "htmx/partials/customers/_contact_macros.html" import outreach_btn %}'
_PHONE_ICON = "M3 5a2 2 0 012-2h3.28"
_EMAIL_ICON = "M3 8l7.89 5.26"
_TEAMS_ICON = "M8 12h.01M12 12h.01"
_WECHAT_ICON = "M17 8h2a2 2 0 012 2"


def _render_outreach_btn(**kwargs) -> str:
    """Render outreach_btn with given kwargs; supply sensible defaults for required
    args."""
    defaults = {
        "label": "Email",
        "href": "https://outlook.office.com/mail/deeplink/compose?to=test%40acme.com",
        "channel": "email",
        "value": "test@acme.com",
        "company_id": 1,
        "site_id": 2,
        "contact_id": 3,
        "contact_name": "Alice",
        "icon_path": _EMAIL_ICON,
    }
    defaults.update(kwargs)
    args = ", ".join(
        f"{k}={v!r}" if not isinstance(v, bool) else f"{k}={'true' if v else 'false'}" for k, v in defaults.items()
    )
    tpl = ENV.from_string(f"{_MACRO_IMPORT}{{{{ outreach_btn({args}) }}}}")
    return tpl.render().strip()


class TestEmailOutreachBtn:
    """Email button must use Outlook compose URL with target=_blank."""

    def test_email_href_contains_outlook_deeplink(self):
        html = _render_outreach_btn(
            href="https://outlook.office.com/mail/deeplink/compose?to=alice%40acme.com",
        )
        assert "outlook.office.com/mail/deeplink/compose?to=" in html

    def test_email_href_no_mailto(self):
        html = _render_outreach_btn(
            href="https://outlook.office.com/mail/deeplink/compose?to=alice%40acme.com",
        )
        assert "mailto:" not in html

    def test_email_opens_new_tab(self):
        html = _render_outreach_btn(
            href="https://outlook.office.com/mail/deeplink/compose?to=alice%40acme.com",
            new_tab=True,
        )
        assert 'target="_blank"' in html
        assert 'rel="noopener noreferrer"' in html

    def test_email_keeps_data_outreach_log(self):
        html = _render_outreach_btn(
            href="https://outlook.office.com/mail/deeplink/compose?to=alice%40acme.com",
            new_tab=True,
        )
        assert "data-outreach-log" in html
        assert 'data-channel="email"' in html
        assert 'data-value="test@acme.com"' in html

    def test_email_urlencode_in_template(self):
        """Render the macro call as it appears in the template (using urlencode
        filter)."""
        tpl = ENV.from_string(
            _MACRO_IMPORT + "{{ outreach_btn('Email',"
            " 'https://outlook.office.com/mail/deeplink/compose?to=' ~ email|urlencode,"
            " 'email', email, 1, 2, 3, 'Bob',"
            " '" + _EMAIL_ICON + "', new_tab=true) }}"
        )
        html = tpl.render(email="bob@supplier.com").strip()
        assert "outlook.office.com/mail/deeplink/compose?to=" in html
        assert "bob%40supplier.com" in html or "bob@supplier.com" in html
        assert 'target="_blank"' in html
        assert "mailto:" not in html


class TestWeChatCopyBtn:
    """WeChat copy=True must render a <button> with clipboard @click — not an <a>."""

    def _render_wechat(self, wechat_id: str = "alice_wechat") -> str:
        tpl = ENV.from_string(
            _MACRO_IMPORT + "{{ outreach_btn('WeChat',"
            " 'weixin://dl/chat?' ~ wid|urlencode,"
            " 'wechat', wid, 1, 2, 3, 'Alice',"
            " '" + _WECHAT_ICON + "', copy=true) }}"
        )
        return tpl.render(wid=wechat_id).strip()

    def test_wechat_copy_renders_button_not_anchor(self):
        html = self._render_wechat()
        assert "<button" in html
        assert "<a href=" not in html

    def test_wechat_copy_has_data_outreach_log(self):
        html = self._render_wechat()
        assert "data-outreach-log" in html

    def test_wechat_copy_has_data_value(self):
        html = self._render_wechat("mywechatid")
        assert 'data-value="mywechatid"' in html

    def test_wechat_copy_has_navigator_clipboard_click(self):
        html = self._render_wechat()
        assert "navigator.clipboard" in html
        assert "navigator.clipboard.writeText" in html

    def test_wechat_copy_has_toast_store(self):
        html = self._render_wechat()
        assert "$store.toast" in html
        assert "$store.toast.show=true" in html

    def test_wechat_copy_no_weixin_href(self):
        """The button form must NOT have href=weixin: (it has no href at all)."""
        html = self._render_wechat()
        assert 'href="weixin' not in html

    def test_wechat_copy_has_channel_wechat(self):
        html = self._render_wechat()
        assert 'data-channel="wechat"' in html


class TestPhoneAndTeamsUnchanged:
    """Phone (tel:) and Teams (https://teams.microsoft.com) must be unaffected."""

    def test_phone_uses_tel_scheme(self):
        html = _render_outreach_btn(
            label="Call",
            href="tel:+15551234567",
            channel="phone",
            value="+15551234567",
            icon_path=_PHONE_ICON,
        )
        assert 'href="tel:+15551234567"' in html
        assert "<a " in html

    def test_teams_uses_https_deeplink_and_new_tab(self):
        tpl = ENV.from_string(
            _MACRO_IMPORT + "{{ outreach_btn('Teams',"
            " 'https://teams.microsoft.com/l/chat/0/0?users=' ~ email|urlencode,"
            " 'teams', email, 1, 2, 3, 'Bob',"
            " '" + _TEAMS_ICON + "', new_tab=true) }}"
        )
        html = tpl.render(email="bob@supplier.com").strip()
        assert "teams.microsoft.com" in html
        assert 'target="_blank"' in html
        assert "<a " in html

    def test_default_copy_false_renders_anchor(self):
        """Passing copy=False (default) keeps the <a> form."""
        html = _render_outreach_btn(
            href="https://outlook.office.com/mail/deeplink/compose?to=x%40y.com",
            new_tab=False,
            copy=False,
        )
        assert "<a " in html
        assert "<button" not in html
