/**
 * rfq_vendor_modal.js — the sightings "Send RFQ" vendor-selection + compose
 * modal Alpine.data component: vendor picking/inline create with normalized-name
 * dedup, datasheet attachment opt-in, preview, skip remediation, and the
 * send + post-send refresh flow.
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: htmx.org, alpinejs, ./globals.js (csrfToken).
 */

import htmx from 'htmx.org';
import Alpine from 'alpinejs';
import { csrfToken } from './globals.js';

// ── rfqVendorModal: sightings "Send RFQ" vendor-selection + compose modal ──
// Rendered by app/templates/htmx/partials/sightings/vendor_modal.html. The server
// passes the pre-selected vendor normalized-names and the requirement ids through a
// SINGLE-quoted x-data attribute via |tojson — kept out of an inline x-data because
// |tojson emits double quotes that would close a double-quoted attribute and break
// Alpine init (see CLAUDE.md Alpine-quoting anti-pattern).
Alpine.data('rfqVendorModal', (suggestedNames, requirementIds) => ({
  step: 'compose',
  // Selection state as a plain reactive object keyed by vendor name (NOT a Set) — matches
  // the sightingSelection store and the project's Alpine-reactivity guidance: Alpine tracks
  // object key add/delete reliably, Set mutations less so.
  selectedVendors: Object.fromEntries((suggestedNames || []).map((n) => [n, true])),
  requirementIds: requirementIds || [],
  // Opt-in datasheet attachment ids (array of integers). Included in _form() so the
  // send-inquiry route can resolve + fetch + encode them. Same list sent to EVERY vendor.
  selectedDatasheetIds: [],
  emailBody: '',
  previewing: false,
  sending: false,

  // ── Any-vendor picker + inline create (bulk composer spec Part 2 §3/§4) ──
  // P5.2: the dropdown itself is a server-rendered hx-get (see vendor_modal.html
  // + sightings.sightings_vendor_search) — vendorQuery only drives the input's
  // x-model + the local vsOpen visibility flag now; there is no client-side
  // vendorResults array or searchVendors() fetch anymore.
  vendorQuery: '',
  addingVendor: false,
  addingVendorBusy: false,
  newVendorName: '',
  newVendorWebsite: '',
  newVendorEmail: '',

  get selectedCount() {
    return Object.keys(this.selectedVendors).length;
  },
  isSelected(name) {
    return !!this.selectedVendors[name];
  },
  toggleVendor(name) {
    if (this.selectedVendors[name]) delete this.selectedVendors[name];
    else this.selectedVendors[name] = true;
  },
  // Server-returned composer rows (composer_vendor_row.html) x-init through here so
  // they arrive CHECKED — runtime-added keys flow into vendor_names via _form().
  selectVendor(name) {
    this.selectedVendors[name] = true;
  },

  // Toggle a datasheet id in/out of selectedDatasheetIds (opt-in attachment list).
  toggleDatasheet(id) {
    const idx = this.selectedDatasheetIds.indexOf(id);
    if (idx >= 0) this.selectedDatasheetIds.splice(idx, 1);
    else this.selectedDatasheetIds.push(id);
  },

  async pickVendor(name) {
    this.vendorQuery = '';
    await this._addComposerVendor({ vendor_name: name });
  },

  async createVendor() {
    if (!this.newVendorName.trim() || this.addingVendorBusy) return;
    this.addingVendorBusy = true;
    try {
      const ok = await this._addComposerVendor({
        vendor_name: this.newVendorName.trim(),
        website: this.newVendorWebsite.trim(),
        email: this.newVendorEmail.trim(),
      });
      if (ok) {
        this.newVendorName = '';
        this.newVendorWebsite = '';
        this.newVendorEmail = '';
        this.addingVendor = false;
      }
    } finally {
      this.addingVendorBusy = false;
    }
  },

  // "Add contact" on a non-contactable (cardless / emailless) suggested row: reveal the
  // existing inline "Add new vendor" form pre-filled with this vendor's display name and
  // focus the email input — the buyer types the known email and the existing
  // composer-vendor POST (createVendor) creates the card + VendorContact. No new endpoint.
  // Only seed the name when the field is empty so a half-typed manual entry survives a
  // click on this action (L2 — don't clobber in-progress input). $nextTick waits for
  // x-show to mount the form before focusing the (now-visible) input.
  addContactFor(name) {
    if (!this.newVendorName.trim()) this.newVendorName = name || '';
    this.addingVendor = true;
    this.$nextTick(() => this.$refs.newVendorEmail?.focus());
  },

  // Fast-path dedup: true when `name` matches a selection key case-insensitively.
  // Keys are server-NORMALIZED names (suffixes stripped) while picker/typed names
  // are display names, so this only catches exact/case matches — the authoritative
  // check in _addComposerVendor re-tests the server's normalized name from the row.
  _isVendorSelected(name) {
    const q = (name || '').trim().toLowerCase();
    return Object.keys(this.selectedVendors).some((k) => k.toLowerCase() === q);
  },

  // Extract the server-normalized vendor name from a composer_vendor_row.html
  // payload. Both row branches carry a data-vendor-norm attribute (excluded rows
  // have no x-init, so the attribute is their ONLY carrier); the x-init
  // selectVendor("<normalized>") parse stays as a fallback for selectable rows.
  // Parsed detached via DOMParser (no script execution, no insert).
  _rowVendorName(html) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const norm = doc.querySelector('[data-vendor-norm]')?.getAttribute('data-vendor-norm');
    if (norm) return norm;
    const xInit = doc.querySelector('[x-init]')?.getAttribute('x-init') || '';
    const m = xInit.match(/selectVendor\(("(?:[^"\\]|\\.)*")\)/);
    if (!m) return null;
    try {
      return JSON.parse(m[1]);
    } catch {
      return null;
    }
  },

  // True when #rfq-added-vendors already holds a row for this normalized name —
  // the dedupe for EXCLUDED rows, which never join selectedVendors (disabled
  // checkbox) and would otherwise stack duplicates on repeated picks.
  _containerHasVendor(norm) {
    const container = document.querySelector('#rfq-added-vendors');
    if (!container) return false;
    return Array.from(container.querySelectorAll('[data-vendor-norm]')).some(
      (el) => el.getAttribute('data-vendor-norm') === norm,
    );
  },

  // POST to composer-vendor and append the returned row into the stable-id
  // #rfq-added-vendors sub-container INSIDE this x-data wrapper (explicit container —
  // swapping the wrapper would re-init rfqVendorModal and wipe selection state).
  // Raw fetch + manual insert (mirrors confirmSend / customerPicker.lookupCompany)
  // so a server 4xx is DETECTED: htmx.ajax resolves on HTTP errors, which used to
  // clear the inline create form on a 400. Returns true only when the vendor ended
  // up selected (row appended, or already present — duplicate picks skip the
  // append, INCLUDING excluded rows via the container check, so #rfq-added-vendors
  // never shows the same vendor twice); false on any error so createVendor keeps
  // the typed values.
  async _addComposerVendor(fields) {
    // Bare picks only: a createVendor submission carrying an email/website must
    // reach the server even when the name matches a selection — the server
    // attaches the typed email/domain to the existing card; skipping here would
    // silently discard the input.
    if (!fields.email && !fields.website && this._isVendorSelected(fields.vendor_name)) {
      this._toast('Vendor already added', 'info');
      return true;
    }
    const form = new FormData();
    form.append('vendor_name', fields.vendor_name);
    if (fields.website) form.append('website', fields.website);
    if (fields.email) form.append('email', fields.email);
    this.requirementIds.forEach((id) => form.append('requirement_ids', id));
    const spinner = document.querySelector('#rfq-added-vendors-spinner');
    spinner?.classList.add('htmx-request');
    try {
      // starlette_csrf requires the x-csrftoken header on POST (mirrors confirmSend).
      const resp = await fetch('/v2/partials/sightings/composer-vendor', {
        method: 'POST',
        headers: { 'x-csrftoken': csrfToken() },
        body: form,
      });
      if (!resp.ok) {
        // The server emits the repo JSON error format ({"error": ...}). A 4xx
        // reason is actionable user input ("invalid website — ...") — surface it
        // verbatim; 5xx bodies are internals, keep the generic try-again.
        let reason = '';
        try {
          reason = (await resp.json()).error || '';
        } catch {
          /* non-JSON / empty body — fall through to the generic message */
        }
        console.error('[rfqVendorModal] add vendor failed: HTTP ' + resp.status, reason);
        const msg = resp.status < 500 && reason
          ? 'Could not add vendor: ' + reason
          : 'Could not add vendor — please try again';
        this._toast(msg, 'error');
        return false;
      }
      const html = await resp.text();
      // Authoritative dedup on the server-normalized name: picking
      // "Mouser Electronics, Inc." when "mouser electronics" is already selected
      // would append a duplicate row while selection state stays unchanged.
      // Excluded rows never enter selectedVendors, so they dedupe against the
      // rows already in the container instead.
      const normalized = this._rowVendorName(html);
      if (normalized && (this.selectedVendors[normalized] || this._containerHasVendor(normalized))) {
        this._toast('Vendor already added', 'info');
        return true;
      }
      const container = document.querySelector('#rfq-added-vendors');
      // Server HTML is trusted (same-origin, auth-protected endpoint).
      container.insertAdjacentHTML('beforeend', html);
      htmx.process(container);
      // The appended row carries Alpine directives (x-init='selectVendor(...)',
      // :checked, @change) that bind to THIS rfqVendorModal x-data scope. htmx.process
      // only wires htmx attributes, not Alpine's, and relying on Alpine 3's
      // MutationObserver is exactly the unreliable path the afterSwap handler warns
      // about — explicitly initTree the new node so the row arrives CHECKED and its
      // checkbox is live (matches the lead-drawer workaround).
      const addedRow = container.lastElementChild;
      if (addedRow && typeof Alpine !== 'undefined' && typeof Alpine.initTree === 'function') {
        Alpine.initTree(addedRow);
      }
      return true;
    } catch (err) {
      console.error('[rfqVendorModal] add vendor failed', err);
      this._toast('Could not add vendor — please try again', 'error');
      return false;
    } finally {
      spinner?.classList.remove('htmx-request');
    }
  },

  // Build a FormData with REPEATED keys for the multi-valued fields. (Object.fromEntries
  // on a FormData silently collapses duplicate keys to the last value — that would send
  // only one requirement_id / vendor_name.) htmx.ajax accepts a FormData for `values`
  // as-is, and fetch sends it directly.
  _form() {
    const form = new FormData();
    this.requirementIds.forEach((id) => form.append('requirement_ids', id));
    Object.keys(this.selectedVendors).forEach((v) => form.append('vendor_names', v));
    form.append('email_body', this.emailBody);
    // Opt-in datasheet attachment ids (integers). Empty selection → no fields posted
    // → server treats as no attachments (regression-safe).
    this.selectedDatasheetIds.forEach((id) => form.append('datasheet_ids', id));
    return form;
  },

  _toast(message, type) {
    // Toast store is { message, type, show } — set fields directly; show is a boolean.
    this.$store.toast.message = message;
    this.$store.toast.type = type;
    this.$store.toast.show = true;
  },

  async loadPreview() {
    if (this.selectedCount === 0 || !this.emailBody || this.previewing) return;
    this.previewing = true;
    try {
      await htmx.ajax('POST', '/v2/partials/sightings/preview-inquiry', {
        target: this.$refs.previewContent,
        swap: 'innerHTML',
        indicator: this.$refs.previewContent,
        values: this._form(),
      });
      // preview_inquiry.html contains Alpine x-data / x-model / @rfq-email-fixed.window
      // directives for the inline fix-email mini-form. htmx.ajax swaps innerHTML but does
      // not run Alpine on new nodes — the afterSwap handler only covers its hardcoded id
      // allowlist (lead-drawer-content, rfq-affinity-section, settings-content). previewContent
      // has no id, so we must explicitly initTree here to bind the fix-email component.
      if (this.$refs.previewContent && typeof Alpine !== 'undefined' && typeof Alpine.initTree === 'function') {
        Alpine.initTree(this.$refs.previewContent);
      }
      this.step = 'preview';
    } catch (err) {
      console.error('[rfqVendorModal] preview failed', err);
      this._toast('Preview failed — please try again', 'error');
    } finally {
      this.previewing = false;
    }
  },

  // One-click skip remediation from the preview step: attach a contact email to a
  // previously-skipped (no-email) vendor then re-run preview so the vendor resolves.
  // POSTs to the existing composer-vendor endpoint (which creates/updates the
  // VendorContact). On non-ok response, shows a toast and keeps the inline form open
  // by NOT calling loadPreview(). On success, selectVendor() ensures the vendor is
  // in selectedVendors, then loadPreview() refreshes the preview panel in-place
  // (no modal close or wrapper re-init — the preview container is a stable-id swap).
  async fixVendorEmail(vendorName, email) {
    if (!email || !vendorName) return;
    const form = new FormData();
    form.append('vendor_name', vendorName);
    form.append('email', email);
    this.requirementIds.forEach((id) => form.append('requirement_ids', id));
    try {
      const resp = await fetch('/v2/partials/sightings/composer-vendor', {
        method: 'POST',
        headers: { 'x-csrftoken': csrfToken() },
        body: form,
      });
      if (!resp.ok) {
        let reason = '';
        try { reason = (await resp.json()).error || ''; } catch { /* non-JSON body */ }
        const msg = resp.status < 500 && reason
          ? 'Could not add email: ' + reason
          : 'Could not add email — please try again';
        this._toast(msg, 'error');
        return; // keep the form open with the typed value
      }
      // Ensure the vendor is in selectedVendors so it is included in the re-preview POST.
      this.selectVendor(vendorName);
      // Signal the nested x-data scope to clear its fixEmail input (success path only).
      window.dispatchEvent(new CustomEvent('rfq-email-fixed'));
      await this.loadPreview();
    } catch (err) {
      console.error('[rfqVendorModal] fixVendorEmail failed', err);
      this._toast('Could not add email — please try again', 'error');
    }
  },

  async confirmSend() {
    if (this.selectedCount === 0 || !this.emailBody || this.sending) return;
    this.sending = true;
    const count = this.selectedCount;
    try {
      // Raw fetch so we can read the result headers below. starlette_csrf requires the
      // x-csrftoken header on POST.
      const resp = await fetch('/v2/partials/sightings/send-inquiry', {
        method: 'POST',
        headers: { 'x-csrftoken': csrfToken() },
        body: this._form(),
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      // The route returns 200 even on a partial/total send failure, so report the TRUE
      // outcome from the X-RFQ-* headers rather than assuming success.
      const sent = parseInt(resp.headers.get('X-RFQ-Sent') || '0', 10);
      const total = parseInt(resp.headers.get('X-RFQ-Total') || String(count), 10);
      const skipped = parseInt(resp.headers.get('X-RFQ-Skipped') || '0', 10);
      // X-RFQ-Unavailable = vendors dropped by the send-time unavailability re-check.
      // They are NOT delivery failures — without subtracting them they'd be
      // misattributed to the 'failed' bucket (total - sent - skipped).
      const unavailable = parseInt(resp.headers.get('X-RFQ-Unavailable') || '0', 10);
      // X-RFQ-Datasheets-Dropped = oversized datasheets silently dropped before send.
      const datasheetsDropped = parseInt(resp.headers.get('X-RFQ-Datasheets-Dropped') || '0', 10);
      const outcome = this._sendOutcome(sent, total, skipped, unavailable, datasheetsDropped);
      this._toast(outcome.message, outcome.type);
      if (!outcome.delivered) return; // nothing sent — keep the modal open to retry
      this._refreshSightings();
      this.$dispatch('close-modal');
    } catch (err) {
      console.error('[rfqVendorModal] send failed', err);
      this._toast('Send failed — please try again', 'error');
    } finally {
      this.sending = false;
    }
  },

  // Map the server's sent/total/skipped/unavailable/datasheetsDropped counts to a toast.
  // `delivered` is false only when nothing went out, so the caller can keep the modal open
  // for a retry. `skipped` = vendors with no contact email; `unavailable` = vendors dropped
  // by the send-time unavailability re-check; `datasheetsDropped` = attachments silently
  // dropped for exceeding the ~3 MB Graph simple-send cap (largest-first).
  _sendOutcome(sent, total, skipped = 0, unavailable = 0, datasheetsDropped = 0) {
    if (sent === 0) {
      return { type: 'error', delivered: false, message: 'Send failed — no RFQs were delivered' };
    }
    let baseMsg;
    if (sent < total) {
      const failed = total - sent - skipped - unavailable;
      const reasons = [];
      if (failed > 0) reasons.push(failed + ' failed');
      if (skipped > 0) reasons.push(skipped + ' had no email');
      if (unavailable > 0) reasons.push(unavailable + ' marked unavailable');
      baseMsg = 'Sent to ' + sent + ' of ' + total + ' vendors' + (reasons.length ? ' — ' + reasons.join(', ') : '');
    } else {
      baseMsg = 'RFQ sent to ' + sent + ' vendor' + (sent === 1 ? '' : 's');
    }
    if (datasheetsDropped > 0) {
      baseMsg += ' (' + datasheetsDropped + ' attachment' + (datasheetsDropped === 1 ? '' : 's') + ' dropped — too large)';
    }
    return {
      type: sent < total ? 'warning' : 'success',
      delivered: true,
      message: baseMsg,
    };
  },

  // A successful send can change BOTH the open detail panel (status pill auto-advances
  // OPEN→SOURCING, new "RFQ sent" activity rows) and the requirements list. Refresh
  // whichever is on screen.
  _refreshSightings() {
    // Best-effort refresh of the open panel + list after a successful send. htmx.ajax
    // rejects only on network/timeout/target errors (HTTP 4xx/5xx are surfaced by the
    // global htmx:responseError toast registered above), so this .catch covers the
    // connection-failure case with a clearer "you already sent" message.
    const onRefreshError = (err) => {
      console.error('[rfqVendorModal] post-send refresh failed', err);
      this._toast('Sent — refresh the page to see updated status', 'warning');
    };
    const selectedReqId = Alpine.store('sightingSelection')?.selectedReqId;
    if (selectedReqId) {
      htmx.ajax('GET', '/v2/partials/sightings/' + selectedReqId + '/detail', {
        target: '#sightings-detail',
        swap: 'innerHTML',
        indicator: '#sightings-detail-skeleton',
      }).catch(onRefreshError);
    }
    const table = document.getElementById('sightings-table');
    const tableUrl = table && table.getAttribute('hx-get');
    if (tableUrl) {
      htmx.ajax('GET', tableUrl, {
        target: '#sightings-table',
        swap: 'innerHTML',
        indicator: '#sightings-load-spinner',
      }).catch(onRefreshError);
    }
  },
}));
