"""
tests/test_form_friction_fixes.py — Tests for form friction reduction changes.

Covers:
 - Q1: Auto-name requisition (frontend logic verified via template presence)
 - Q3: Sticky vendor on mobile offer form (sessionStorage pattern)
 - Q5: Payment terms dropdown (template + schema compatibility)
 - Bulk owner dropdown (replaces raw ID prompt)
 - Call duration mm:ss formatter (replaces raw seconds)
 - Centralized margin calculation helper
 - Email format validation

Called by: pytest
Depends on: app/schemas/crm.py, app/templates/index.html, app/static/crm.js, app/static/app.js
"""

import re
from pathlib import Path

import pytest
from app.schemas.crm import SiteCreate, SiteUpdate


# ── Q5: Payment Terms Dropdown ──────────────────────────────────────────

EXPECTED_PAYMENT_OPTIONS = [
    "Net 15", "Net 30", "Net 45", "Net 60", "Net 90",
    "COD", "CIA", "2/10 Net 30", "Due on Receipt", "Prepaid",
]

EXPECTED_SHIPPING_OPTIONS = [
    "FOB Origin", "FOB Destination", "Prepaid",
    "Collect", "DDP", "EXW", "FCA", "CIF",
]


class TestPaymentTermsDropdown:
    """Verify the HTML template has <select> dropdowns for payment/shipping terms."""

    @pytest.fixture
    def template_html(self) -> str:
        path = Path(__file__).parent.parent / "app" / "templates" / "index.html"
        return path.read_text()

    def test_payment_terms_is_select(self, template_html: str) -> None:
        """asSitePayTerms should be a <select>, not an <input>."""
        assert '<select id="asSitePayTerms"' in template_html
        assert '<input id="asSitePayTerms"' not in template_html

    def test_shipping_terms_is_select(self, template_html: str) -> None:
        """asSiteShipTerms should be a <select>, not an <input>."""
        assert '<select id="asSiteShipTerms"' in template_html
        assert '<input id="asSiteShipTerms"' not in template_html

    def test_payment_options_present(self, template_html: str) -> None:
        """All standard payment term options exist in the template."""
        for opt in EXPECTED_PAYMENT_OPTIONS:
            assert f"<option>{opt}</option>" in template_html, f"Missing option: {opt}"

    def test_shipping_options_present(self, template_html: str) -> None:
        """All standard shipping term options exist in the template."""
        for opt in EXPECTED_SHIPPING_OPTIONS:
            assert f"<option>{opt}</option>" in template_html, f"Missing option: {opt}"

    def test_schema_accepts_dropdown_values(self) -> None:
        """SiteCreate schema accepts all dropdown payment_terms values."""
        for term in EXPECTED_PAYMENT_OPTIONS:
            site = SiteCreate(site_name="Test", payment_terms=term)
            assert site.payment_terms == term

    def test_schema_accepts_custom_value(self) -> None:
        """Schema still allows custom free-text values (no enum restriction)."""
        site = SiteCreate(site_name="Test", payment_terms="Custom Terms 123")
        assert site.payment_terms == "Custom Terms 123"

    def test_schema_accepts_empty_value(self) -> None:
        """Schema allows empty/null payment_terms."""
        site = SiteCreate(site_name="Test", payment_terms=None)
        assert site.payment_terms is None

    def test_site_update_accepts_terms(self) -> None:
        """SiteUpdate schema accepts payment and shipping terms."""
        update = SiteUpdate(payment_terms="Net 60", shipping_terms="FOB Destination")
        assert update.payment_terms == "Net 60"
        assert update.shipping_terms == "FOB Destination"


# ── Q5: Quote Terms Dropdown in CRM JS ─────────────────────────────────


class TestQuoteTermsDropdown:
    """Verify crm.js renders <select> for quote payment/shipping terms."""

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    def test_quote_terms_is_select(self, crm_js: str) -> None:
        """qtTerms should be rendered as <select>, not <input>."""
        assert '<select id="qtTerms"' in crm_js
        assert 'id="qtTerms" value="' not in crm_js  # no input with value attr

    def test_quote_shipping_is_select(self, crm_js: str) -> None:
        """qtShip should be rendered as <select>, not <input>."""
        assert '<select id="qtShip"' in crm_js

    def test_setSelectOrAdd_helper_exists(self, crm_js: str) -> None:
        """The _setSelectOrAdd helper for custom dropdown values exists."""
        assert "function _setSelectOrAdd(id, val)" in crm_js

    def test_quote_terms_initialized(self, crm_js: str) -> None:
        """After rendering, dropdown values are set via _setSelectOrAdd."""
        assert "_setSelectOrAdd('qtTerms'" in crm_js
        assert "_setSelectOrAdd('qtShip'" in crm_js


# ── Q1: Auto-name Requisitions ──────────────────────────────────────────


class TestAutoNameRequisition:
    """Verify the selectSite function auto-populates requisition name."""

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    def test_auto_name_logic_in_selectSite(self, crm_js: str) -> None:
        """selectSite() should contain auto-name logic when name is empty."""
        # Find the selectSite function
        match = re.search(r"function selectSite\(.*?\{(.+?)\n\}", crm_js, re.DOTALL)
        assert match, "selectSite function not found"
        body = match.group(1)
        assert "nrName" in body, "Should reference the name input"
        assert "toLocaleString" in body or "month" in body.lower() or "Mon" in body, \
            "Should format month for auto-name"

    def test_auto_name_only_when_empty(self, crm_js: str) -> None:
        """Auto-name should only fire when name field is empty (no overwrite)."""
        match = re.search(r"function selectSite\(.*?\{(.+?)\n\}", crm_js, re.DOTALL)
        assert match
        body = match.group(1)
        assert "!nrName.value.trim()" in body, \
            "Should check that name is empty before auto-populating"


# ── Q3: Sticky Vendor on Mobile Offer Form ──────────────────────────────


class TestStickyVendor:
    """Verify the mobile offer form remembers the last vendor."""

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    def test_vendor_saved_to_session_storage(self, crm_js: str) -> None:
        """After offer submit, vendor is saved to sessionStorage."""
        assert "sessionStorage.setItem('lastOfferVendor'" in crm_js

    def test_vendor_restored_from_session_storage(self, crm_js: str) -> None:
        """On form open, vendor is read from sessionStorage."""
        assert "sessionStorage.getItem('lastOfferVendor')" in crm_js

    def test_vendor_card_id_saved(self, crm_js: str) -> None:
        """Vendor card ID is also persisted for autocomplete resolution."""
        assert "sessionStorage.setItem('lastOfferVendorCardId'" in crm_js
        assert "sessionStorage.getItem('lastOfferVendorCardId')" in crm_js

    def test_vendor_prefilled_in_input(self, crm_js: str) -> None:
        """The moVendor input value is pre-filled from session storage."""
        assert 'id="moVendor" type="text" placeholder="Vendor name" value="' in crm_js


# ── Q5: App.js Quote Terms Dropdown ─────────────────────────────────────


class TestAppJsQuoteTerms:
    """Verify app.js renders <select> for drill-down quote terms."""

    @pytest.fixture
    def app_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "app.js"
        return path.read_text()

    def test_ddq_terms_uses_helper(self, app_js: str) -> None:
        """ddqTerms should use _termsSelectHtml helper for rendering."""
        assert "_termsSelectHtml('ddqTerms-'" in app_js or "_termsSelectHtml(\"ddqTerms-\"" in app_js

    def test_ddq_ship_uses_helper(self, app_js: str) -> None:
        """ddqShip should use _termsSelectHtml helper for rendering."""
        assert "_termsSelectHtml('ddqShip-'" in app_js or "_termsSelectHtml(\"ddqShip-\"" in app_js

    def test_wire_terms_select_for_drafts(self, app_js: str) -> None:
        """Draft quotes wire up _wireTermsSelect for payment/shipping."""
        assert "_wireTermsSelect(" in app_js

    def test_terms_select_html_helper_defined(self, app_js: str) -> None:
        """_termsSelectHtml helper function exists."""
        assert "_termsSelectHtml" in app_js


# ── Bulk Owner Dropdown ────────────────────────────────────────────────


class TestBulkOwnerDropdown:
    """Verify bulk owner assign uses a dropdown, not a raw ID prompt."""

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    def test_no_prompt_input_for_owner(self, crm_js: str) -> None:
        """bulkAssignOwner should NOT use promptInput."""
        # Extract just the function body
        idx = crm_js.index("async function bulkAssignOwner()")
        chunk = crm_js[idx:idx + 2000]
        assert "promptInput" not in chunk

    def test_uses_select_dropdown(self, crm_js: str) -> None:
        """bulkAssignOwner should create a <select> for user selection."""
        idx = crm_js.index("async function bulkAssignOwner()")
        chunk = crm_js[idx:idx + 2000]
        assert "_bulkOwnerSelect" in chunk

    def test_fetches_user_list(self, crm_js: str) -> None:
        """bulkAssignOwner should use the cached user list."""
        idx = crm_js.index("async function bulkAssignOwner()")
        chunk = crm_js[idx:idx + 2000]
        assert "_userListCache" in chunk


# ── Call Duration Formatter ────────────────────────────────────────────


class TestCallDurationFormatter:
    """Verify duration inputs use min/sec instead of raw seconds."""

    @pytest.fixture
    def template_html(self) -> str:
        path = Path(__file__).parent.parent / "app" / "templates" / "index.html"
        return path.read_text()

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    @pytest.fixture
    def app_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "app.js"
        return path.read_text()

    def test_no_raw_seconds_input(self, template_html: str) -> None:
        """Template should not have raw seconds input for duration."""
        assert 'id="lcDuration"' not in template_html
        assert 'id="vlcDuration"' not in template_html

    def test_min_sec_inputs_present(self, template_html: str) -> None:
        """Template should have min + sec split inputs."""
        assert 'id="lcDurMin"' in template_html
        assert 'id="lcDurSec"' in template_html
        assert 'id="vlcDurMin"' in template_html
        assert 'id="vlcDurSec"' in template_html

    def test_crm_js_reads_min_sec(self, crm_js: str) -> None:
        """CRM JS should convert min/sec to total seconds."""
        assert "lcDurMin" in crm_js
        assert "lcDurSec" in crm_js

    def test_app_js_reads_min_sec(self, app_js: str) -> None:
        """App JS should convert min/sec for vendor log call."""
        assert "vlcDurMin" in app_js
        assert "vlcDurSec" in app_js


# ── Centralized Margin Calculation ─────────────────────────────────────


class TestCentralizedMargin:
    """Verify margin calculation uses a shared helper."""

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    @pytest.fixture
    def app_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "app.js"
        return path.read_text()

    def test_calcMarginPct_defined(self, crm_js: str) -> None:
        """calcMarginPct helper function exists."""
        assert "function calcMarginPct(sell, cost)" in crm_js

    def test_marginColor_defined(self, crm_js: str) -> None:
        """marginColor helper function exists."""
        assert "function marginColor(pct)" in crm_js

    def test_calcMarginPct_exported(self, crm_js: str) -> None:
        """calcMarginPct is exposed to window scope."""
        assert "calcMarginPct," in crm_js.split("Object.assign(window")[1]

    def test_crm_uses_helper(self, crm_js: str) -> None:
        """CRM JS should use calcMarginPct instead of inline formula."""
        assert "calcMarginPct(item.sell_price, cost)" in crm_js
        assert "calcMarginPct(totalSell, totalCost)" in crm_js

    def test_app_js_uses_helper(self, app_js: str) -> None:
        """App JS should reference window.calcMarginPct."""
        assert "window.calcMarginPct" in app_js


# ── Email Validation ───────────────────────────────────────────────────


class TestEmailValidation:
    """Verify email format validation is added."""

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    def test_isValidEmail_defined(self, crm_js: str) -> None:
        """isValidEmail helper function exists."""
        assert "function isValidEmail(email)" in crm_js

    def test_quote_send_validates_email(self, crm_js: str) -> None:
        """confirmSendQuote validates email format."""
        assert "isValidEmail(toEmail)" in crm_js

    def test_site_contact_validates_email(self, crm_js: str) -> None:
        """saveSiteContact validates email format."""
        assert "isValidEmail(data.email)" in crm_js

    def test_isValidEmail_exported(self, crm_js: str) -> None:
        """isValidEmail is exposed to window scope."""
        assert "isValidEmail," in crm_js.split("Object.assign(window")[1]


# ── Stale Modal Data Prevention ────────────────────────────────────────


class TestStaleModalPrevention:
    """Verify modals clear fields on open to prevent stale data."""

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    def test_vendor_modal_clears_fields(self, crm_js: str) -> None:
        """openNewVendorModal should clear all vendor contact fields."""
        idx = crm_js.index("function openNewVendorModal()")
        chunk = crm_js[idx:idx + 500]
        assert "vcFullName" in chunk
        assert "vcEmail" in chunk
        assert "vcPhone" in chunk
        assert "vcCardId" in chunk

    def test_vendor_modal_resets_hidden_ids(self, crm_js: str) -> None:
        """openNewVendorModal should reset hidden card/contact IDs."""
        idx = crm_js.index("function openNewVendorModal()")
        chunk = crm_js[idx:idx + 500]
        assert "'vcCardId', 'value', ''" in chunk
        assert "'vcContactId', 'value', ''" in chunk


# ── Phone Validation ──────────────────────────────────────────────────


class TestPhoneValidation:
    """Verify phone format validation is applied."""

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    @pytest.fixture
    def app_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "app.js"
        return path.read_text()

    def test_isValidPhone_defined(self, crm_js: str) -> None:
        """isValidPhone helper function exists."""
        assert "function isValidPhone(phone)" in crm_js

    def test_site_contact_validates_phone(self, crm_js: str) -> None:
        """saveSiteContact validates phone format."""
        assert "isValidPhone(data.phone)" in crm_js

    def test_vendor_contact_validates_phone(self, app_js: str) -> None:
        """saveVendorContact validates phone format."""
        assert "isValidPhone(body.phone)" in app_js

    def test_vendor_contact_validates_email(self, app_js: str) -> None:
        """saveVendorContact validates email format."""
        assert "isValidEmail(body.email)" in app_js

    def test_isValidPhone_exported(self, crm_js: str) -> None:
        """isValidPhone is exposed to window scope."""
        assert "isValidPhone," in crm_js.split("Object.assign(window")[1]


# ── Phone Input Type ──────────────────────────────────────────────────


class TestPhoneInputType:
    """Verify phone inputs have type=tel for mobile UX."""

    @pytest.fixture
    def template_html(self) -> str:
        path = Path(__file__).parent.parent / "app" / "templates" / "index.html"
        return path.read_text()

    def test_ec_phone_has_type_tel(self, template_html: str) -> None:
        """Edit company phone input should have type=tel."""
        assert 'id="ecPhone" type="tel"' in template_html


# ── Styled Delete Confirmation ─────────────────────────────────────────


class TestStyledDeleteConfirmation:
    """Verify destructive actions use styled modals, not browser confirm()."""

    @pytest.fixture
    def app_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "app.js"
        return path.read_text()

    def test_no_browser_confirm_in_delete_task(self, app_js: str) -> None:
        """_deleteTask should NOT use browser confirm()."""
        idx = app_js.index("function _deleteTask(")
        chunk = app_js[idx:idx + 800]
        assert "confirm(" not in chunk

    def test_uses_confirmAction(self, app_js: str) -> None:
        """_deleteTask should use confirmAction styled modal."""
        idx = app_js.index("function _deleteTask(")
        chunk = app_js[idx:idx + 800]
        assert "confirmAction(" in chunk


# ── guardBtn() Loading States ──────────────────────────────────────────


class TestGuardBtnProtection:
    """Verify all save functions use guardBtn() to prevent double-submit."""

    @pytest.fixture
    def crm_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "crm.js"
        return path.read_text()

    @pytest.fixture
    def app_js(self) -> str:
        path = Path(__file__).parent.parent / "app" / "static" / "app.js"
        return path.read_text()

    @pytest.mark.parametrize("func_name", [
        "saveEditCompany",
        "saveSiteContact",
        "saveLogCall",
        "saveLogNote",
        "addSite",
    ])
    def test_crm_save_uses_guardBtn(self, crm_js: str, func_name: str) -> None:
        """CRM save functions should use guardBtn()."""
        idx = crm_js.index(f"function {func_name}(")
        chunk = crm_js[idx:idx + 2000]
        assert "guardBtn(" in chunk, f"{func_name} missing guardBtn()"

    @pytest.mark.parametrize("func_name", [
        "saveVendorContact",
        "saveVendorLogCall",
        "saveVendorLogNote",
    ])
    def test_app_save_uses_guardBtn(self, app_js: str, func_name: str) -> None:
        """App save functions should use guardBtn()."""
        idx = app_js.index(f"function {func_name}(")
        chunk = app_js[idx:idx + 2000]
        assert "guardBtn(" in chunk, f"{func_name} missing guardBtn()"


# ── Required Field Indicators ─────────────────────────────────────────


class TestRequiredFieldIndicators:
    """Verify required fields use consistent red asterisk pattern."""

    @pytest.fixture
    def template_html(self) -> str:
        path = Path(__file__).parent.parent / "app" / "templates" / "index.html"
        return path.read_text()

    RED_ASTERISK = '<span style="color:var(--red)">*</span>'

    @pytest.mark.parametrize("field_label", [
        "Company Name",   # newCompanyModal
        "Name",           # newReqModal
        "Site Name",      # addSiteModal
        "Phone",          # logCallModal
        "Note",           # logNoteModal + vendorLogNoteModal
        "Email",          # vendorContactModal
    ])
    def test_required_field_has_red_asterisk(self, template_html: str, field_label: str) -> None:
        """Required fields should use the red asterisk <span> pattern."""
        pattern = f"{field_label} {self.RED_ASTERISK}"
        assert pattern in template_html, f"Missing red asterisk for: {field_label}"

    def test_no_inline_asterisk_pattern(self, template_html: str) -> None:
        """No labels should use inline 'Label *' pattern without styling."""
        # These old patterns should be replaced with styled spans
        for old_pattern in ['>Phone *<', '>Name *<', '>Note *<', '>Email *<']:
            assert old_pattern not in template_html, f"Found unstyled asterisk: {old_pattern}"


# ── Enter Key Handlers ────────────────────────────────────────────────


class TestEnterKeyHandlers:
    """Verify primary form fields have Enter key submit handlers."""

    @pytest.fixture
    def template_html(self) -> str:
        path = Path(__file__).parent.parent / "app" / "templates" / "index.html"
        return path.read_text()

    def test_edit_company_enter_handler(self, template_html: str) -> None:
        """ecName should have Enter key handler to save."""
        assert 'id="ecName"' in template_html
        # Find the line with ecName and check for onkeydown
        idx = template_html.index('id="ecName"')
        chunk = template_html[idx - 200:idx + 200]
        assert "saveEditCompany()" in chunk

    def test_add_site_enter_handler(self, template_html: str) -> None:
        """asSiteName should have Enter key handler to save."""
        idx = template_html.index('id="asSiteName"')
        chunk = template_html[idx - 200:idx + 200]
        assert "addSite()" in chunk

    def test_log_note_ctrl_enter(self, template_html: str) -> None:
        """lnNotes textarea should support Ctrl+Enter to save."""
        idx = template_html.index('id="lnNotes"')
        chunk = template_html[idx - 200:idx + 400]
        assert "ctrlKey" in chunk or "metaKey" in chunk

    def test_vendor_log_note_ctrl_enter(self, template_html: str) -> None:
        """vlnNotes textarea should support Ctrl+Enter to save."""
        idx = template_html.index('id="vlnNotes"')
        chunk = template_html[idx - 200:idx + 400]
        assert "ctrlKey" in chunk or "metaKey" in chunk

    def test_edit_company_name_required(self, template_html: str) -> None:
        """ecName should have required attribute."""
        idx = template_html.index('id="ecName"')
        chunk = template_html[idx - 200:idx + 200]
        assert "required" in chunk
