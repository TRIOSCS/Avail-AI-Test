/**
 * offer_forms.js — offer-entry Alpine.data components: offerQualification
 * (condition-driven chip panels + note preview + completeness meter, mirroring
 * the server's compose_note/_items_for) and attachmentsPanel (dropzone +
 * busy state for the unified attachments panel).
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: alpinejs.
 */

import Alpine from 'alpinejs';

// ── offerQualification: condition-driven offer form (chip panels + note preview + meter) ──
// Rendered by sightings/offer_form_modal.html and requisitions/add_offer_form.html.
// x-data attribute on the <form> must be SINGLE-quoted with |tojson so that prefill
// values containing quotes or special chars cannot break Alpine init.
//
// noteText() mirrors server compose_note() byte-for-byte:
//   - Chip values are sent as-is (e.g. "Tape & Reel"); server normalizes via
//     normalize_packaging() then humanizes via _PKG_DISPLAY. The JS _pkgDisplay map
//     replicates that two-step so preview == stored note for all six chips.
//
// _items() mirrors server _items_for() per condition:
//   new:        [manufacturer, package_type(=any non-empty packaging), date_code] — no images
//   new_no_pkg: [packaging, images=false, date_code]
//   pulls:      [packaging, usage, images=false, part_condition]
//   refurb:     [refurbished_by, refurb_process, images=false] + cert_doc if third_party
Alpine.data('offerQualification', (prefill) => ({
  condition: (prefill && prefill.condition) || '',
  packaging: (prefill && prefill.packaging) || '',
  usage: (prefill && prefill.usage) || '',
  refurbished_by: (prefill && prefill.refurbished_by) || '',
  cert_doc: (prefill && prefill.cert_doc) || '',
  refurb_process: (prefill && prefill.refurb_process) || '',
  part_condition: (prefill && prefill.part_condition) || '',
  manufacturer: (prefill && prefill.manufacturer) || '',
  date_code: (prefill && prefill.date_code) || '',
  _pkgChips: ['Tape & Reel', 'Reels', 'Trays', 'Tubes', 'Antistatic bags', 'Boxes'],
  // Map chip value → humanized display label, mirroring normalize_packaging + _PKG_DISPLAY on the server.
  // normalize_packaging("Tape & Reel") → "reel"; _PKG_DISPLAY["reel"] → "Reels"
  // normalize_packaging("Reels")       → "reel"; _PKG_DISPLAY["reel"] → "Reels"
  // normalize_packaging("Trays")       → "tray"; _PKG_DISPLAY["tray"] → "Trays"
  // normalize_packaging("Tubes")       → "tube"; _PKG_DISPLAY["tube"] → "Tubes"
  // normalize_packaging("Antistatic bags") → "bag"; _PKG_DISPLAY["bag"] → "Antistatic bags"
  // normalize_packaging("Boxes")       → "box";  _PKG_DISPLAY["box"]  → "Boxes"
  _pkgDisplay: {
    'Tape & Reel': 'Reels',
    'Reels': 'Reels',
    'Trays': 'Trays',
    'Tubes': 'Tubes',
    'Antistatic bags': 'Antistatic bags',
    'Boxes': 'Boxes',
  },
  essentialsMet() {
    const c = this.condition;
    if (!c) return true; // unset is allowed to save
    if (c === 'new') return !!this.manufacturer.trim();
    if (c === 'new_no_pkg') return this._pkgOk();
    if (c === 'pulls') return this._pkgOk() && (this.usage === 'boards' || this.usage === 'systems');
    if (c === 'refurb') return (this.refurbished_by === 'supplier' || this.refurbished_by === 'third_party') && !!this.refurb_process.trim();
    return true;
  },
  _pkgOk() { return this._pkgChips.includes(this.packaging); },
  // Returns the display label for the current packaging chip, mirroring server _PKG_DISPLAY.
  _pkgLabel() { return this._pkgDisplay[this.packaging] || this.packaging; },
  noteText() {
    const c = this.condition;
    const pkg = this._pkgLabel();
    if (c === 'new') return "New — parts are in the original manufacturer's packaging.";
    if (c === 'new_no_pkg') {
      return pkg
        ? `New, no original manufacturer packaging. Packaged in ${pkg}.`
        : 'New, no original manufacturer packaging.';
    }
    if (c === 'pulls') {
      const u = this.usage === 'boards' ? 'boards' : this.usage === 'systems' ? 'systems' : '';
      let n;
      if (pkg && u) n = `Pulls — packaged in ${pkg}, pulled from ${u}.`;
      else if (pkg) n = `Pulls — packaged in ${pkg}.`;
      else if (u) n = `Pulls — pulled from ${u}.`;
      else n = 'Pulls.';
      const pc = this.part_condition.trim();
      return pc ? `${n} Condition: ${pc}.` : n;
    }
    if (c === 'refurb') {
      const who = this.refurbished_by === 'supplier' ? 'the supplier'
        : this.refurbished_by === 'third_party' ? 'a third party' : '';
      let n = who ? `Refurbished by ${who}.` : 'Refurbished.';
      const proc = this.refurb_process.trim();
      if (proc) n += ` Process: ${proc}.`;
      if (this.refurbished_by === 'third_party') {
        if (this.cert_doc === 'yes') n += ' Certifying document on file.';
        else if (this.cert_doc === 'no') n += ' No certifying document.';
      }
      return n;
    }
    return '';
  },
  // Mirrors server _items_for(condition, data, has_images) with has_images=false (no attachments at entry).
  _items() {
    const c = this.condition;
    const pkgOk = this._pkgOk();
    const dcOk = !!this.date_code.trim();
    // For condition=new the server counts package_type as any non-empty packaging string
    // (free-text in "More details"), NOT chip-membership — mirror bool(_s(data,"packaging")).
    if (c === 'new') return [!!this.manufacturer.trim(), !!this.packaging.trim(), dcOk];
    if (c === 'new_no_pkg') return [pkgOk, false, dcOk];
    if (c === 'pulls') return [pkgOk, this.usage === 'boards' || this.usage === 'systems', false, !!this.part_condition.trim()];
    if (c === 'refurb') {
      const a = [
        this.refurbished_by === 'supplier' || this.refurbished_by === 'third_party',
        !!this.refurb_process.trim(),
        false,
      ];
      if (this.refurbished_by === 'third_party') a.push(this.cert_doc === 'yes' || this.cert_doc === 'no');
      return a;
    }
    return [];
  },
  meterTotal() { return this._items().length; },
  meterFilled() { return this._items().filter(Boolean).length; },
}));

/**
 * attachmentsPanel — Alpine.js component for the unified file-attachments panel.
 *
 * Owns the dropzone hover state, a friendly busy state during upload, and the
 * drop handler that assigns dropped files to the picker input and submits the
 * form. The form itself is plain HTMX (multipart POST → attachments:changed);
 * this factory only decorates it with interaction state.
 *
 * Called by: partials/shared/_attachments.html (attachments_panel macro)
 * Depends on: Alpine.js, HTMX. Error toasts are surfaced by the global
 *             htmx:responseError handler (reads body.error) — no per-panel wiring.
 */
Alpine.data('attachmentsPanel', () => ({
  dragging: false,
  busy: false,
  busyLabel: 'Uploading…',

  init() {
    // The dropzone form is this component's root (<div> wraps it); listen on the
    // root so both the upload form and the list container's requests are seen.
    // Only the multipart upload toggles the busy state.
    this.$el.addEventListener('htmx:beforeRequest', (e) => {
      if (e.target && e.target.tagName === 'FORM') this.busy = true;
    });
    this.$el.addEventListener('htmx:afterRequest', (e) => {
      if (e.target && e.target.tagName === 'FORM') this.busy = false;
    });
  },

  onDrop(evt) {
    this.dragging = false;
    const files = evt.dataTransfer && evt.dataTransfer.files;
    if (!files || !files.length) return;
    this.$refs.fileInput.files = files;
    this.$refs.fileInput.closest('form').requestSubmit();
  },
}));
