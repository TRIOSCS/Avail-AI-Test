/**
 * rfq-vendor-modal.test.ts — Vitest unit tests for the rfqVendorModal Alpine component.
 *
 * Mirrors the logic of Alpine.data('rfqVendorModal', ...) in app/static/htmx_app.js
 * (the established convention in alpine-components.test.ts is to re-implement the data
 * factory's logic and test it directly, rather than booting Alpine).
 *
 * Guards the sightings "Send RFQ" modal regressions:
 *  - selectedVendors seeded from the server-provided pre-selected names
 *  - toggleVendor add/remove
 *  - _form() serialises MULTIPLE requirement_ids / vendor_names as REPEATED keys
 *    (the original used Object.fromEntries(FormData), which silently collapsed
 *    duplicate keys to a single value)
 *  - _toast() sets the toast store fields directly (show is a boolean, not a method)
 *  - confirmSend/loadPreview no-op when nothing is selected or the body is empty
 *
 * Called by: npx vitest run
 * Depends on: vitest, jsdom
 */

import { describe, it, expect, beforeEach } from 'vitest';

// Mirror of the rfqVendorModal() factory's pure logic (network calls stubbed out so
// we can exercise the guards and serialisation without Alpine/htmx/fetch).
function createRfqVendorModal(suggestedNames: string[], requirementIds: number[]) {
  return {
    step: 'compose',
    // Plain reactive object keyed by vendor name (mirrors the factory + sightingSelection store).
    selectedVendors: Object.fromEntries((suggestedNames || []).map((n) => [n, true])) as Record<string, boolean>,
    requirementIds: requirementIds || [],
    emailBody: '',
    previewing: false,
    sending: false,
    // test-only spies
    $store: { toast: { message: '', type: 'info', show: false } },
    _previewCalled: false,
    _sendCalled: false,

    get selectedCount() {
      return Object.keys(this.selectedVendors).length;
    },
    isSelected(name: string) {
      return !!this.selectedVendors[name];
    },
    toggleVendor(name: string) {
      if (this.selectedVendors[name]) delete this.selectedVendors[name];
      else this.selectedVendors[name] = true;
    },

    _form(): FormData {
      const form = new FormData();
      this.requirementIds.forEach((id) => form.append('requirement_ids', String(id)));
      Object.keys(this.selectedVendors).forEach((v) => form.append('vendor_names', v));
      form.append('email_body', this.emailBody);
      return form;
    },

    _toast(message: string, type: string) {
      this.$store.toast.message = message;
      this.$store.toast.type = type;
      this.$store.toast.show = true;
    },

    _sendOutcome(sent: number, total: number) {
      if (sent === 0) {
        return { type: 'error', delivered: false, message: 'Send failed — no RFQs were delivered' };
      }
      if (sent < total) {
        return {
          type: 'warning',
          delivered: true,
          message: 'Sent to ' + sent + ' of ' + total + ' vendors — ' + (total - sent) + ' failed',
        };
      }
      return {
        type: 'success',
        delivered: true,
        message: 'RFQ sent to ' + sent + ' vendor' + (sent === 1 ? '' : 's'),
      };
    },

    loadPreview() {
      if (this.selectedCount === 0 || !this.emailBody || this.previewing) return;
      this._previewCalled = true;
    },

    confirmSend() {
      if (this.selectedCount === 0 || !this.emailBody || this.sending) return;
      this._sendCalled = true;
    },
  };
}

describe('rfqVendorModal', () => {
  let m: ReturnType<typeof createRfqVendorModal>;

  beforeEach(() => {
    m = createRfqVendorModal(['arrow electronics', 'mouser'], [10, 11]);
  });

  describe('initial state', () => {
    it('seeds selectedVendors from the pre-selected names', () => {
      expect(m.selectedCount).toBe(2);
      expect(m.isSelected('arrow electronics')).toBe(true);
      expect(m.isSelected('mouser')).toBe(true);
    });

    it('seeds an empty selection when no names are provided', () => {
      const empty = createRfqVendorModal([], []);
      expect(empty.selectedCount).toBe(0);
    });

    it('starts on the compose step', () => {
      expect(m.step).toBe('compose');
    });
  });

  describe('toggleVendor', () => {
    it('removes a vendor that is selected', () => {
      m.toggleVendor('mouser');
      expect(m.isSelected('mouser')).toBe(false);
      expect(m.selectedCount).toBe(1);
    });

    it('adds a vendor that is not selected', () => {
      m.toggleVendor('digikey');
      expect(m.isSelected('digikey')).toBe(true);
      expect(m.selectedCount).toBe(3);
    });
  });

  describe('_form serialisation', () => {
    it('emits a repeated key for every requirement id', () => {
      m.emailBody = 'hi';
      const form = m._form();
      expect(form.getAll('requirement_ids')).toEqual(['10', '11']);
    });

    it('emits a repeated key for every selected vendor (no duplicate-key collapse)', () => {
      m.emailBody = 'hi';
      const form = m._form();
      // The original Object.fromEntries(FormData) bug would have dropped all but one.
      expect(form.getAll('vendor_names').sort()).toEqual(['arrow electronics', 'mouser']);
    });

    it('reflects toggles in the serialised vendor list', () => {
      m.toggleVendor('mouser'); // remove
      m.toggleVendor('digikey'); // add
      const form = m._form();
      expect(form.getAll('vendor_names').sort()).toEqual(['arrow electronics', 'digikey']);
    });

    it('includes the email body', () => {
      m.emailBody = 'please quote';
      expect(m._form().get('email_body')).toBe('please quote');
    });
  });

  describe('_toast', () => {
    it('sets message/type/show directly (show is a boolean, not a method)', () => {
      m._toast('RFQ sent to 2 vendors', 'success');
      expect(m.$store.toast.message).toBe('RFQ sent to 2 vendors');
      expect(m.$store.toast.type).toBe('success');
      expect(m.$store.toast.show).toBe(true);
    });
  });

  describe('_sendOutcome (true outcome from server X-RFQ-* counts)', () => {
    it('reports full success when all vendors were sent', () => {
      const o = m._sendOutcome(3, 3);
      expect(o.type).toBe('success');
      expect(o.delivered).toBe(true);
      expect(o.message).toBe('RFQ sent to 3 vendors');
    });

    it('uses singular wording for one vendor', () => {
      expect(m._sendOutcome(1, 1).message).toBe('RFQ sent to 1 vendor');
    });

    it('warns on a PARTIAL failure (the route still returns HTTP 200)', () => {
      const o = m._sendOutcome(1, 3);
      expect(o.type).toBe('warning');
      expect(o.delivered).toBe(true);
      expect(o.message).toBe('Sent to 1 of 3 vendors — 2 failed');
    });

    it('errors and keeps the modal open when NOTHING was delivered', () => {
      const o = m._sendOutcome(0, 3);
      expect(o.type).toBe('error');
      expect(o.delivered).toBe(false);
      expect(o.message).toBe('Send failed — no RFQs were delivered');
    });
  });

  describe('guards', () => {
    it('confirmSend is a no-op with no vendors selected', () => {
      const empty = createRfqVendorModal([], [10]);
      empty.emailBody = 'hi';
      empty.confirmSend();
      expect(empty._sendCalled).toBe(false);
    });

    it('confirmSend is a no-op with an empty body', () => {
      m.confirmSend();
      expect(m._sendCalled).toBe(false);
    });

    it('confirmSend proceeds with vendors + body', () => {
      m.emailBody = 'hi';
      m.confirmSend();
      expect(m._sendCalled).toBe(true);
    });

    it('loadPreview is a no-op with an empty body', () => {
      m.loadPreview();
      expect(m._previewCalled).toBe(false);
    });

    it('loadPreview proceeds with vendors + body', () => {
      m.emailBody = 'hi';
      m.loadPreview();
      expect(m._previewCalled).toBe(true);
    });
  });
});
