/**
 * requisitions.js — requisition-flow Alpine.data components: customerPicker
 * (customer/site typeahead + quick-create), unifiedReqModal (paste/upload AI
 * parse + part rows editor), and quoteBuilderTab (inline quote builder with
 * live margin + guardrails).
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: htmx.org, alpinejs.
 */

import htmx from 'htmx.org';
import Alpine from 'alpinejs';

/**
 * customerPicker — Alpine.js component for customer/company typeahead selection.
 * Supports searching existing customers, selecting a site, and quick-creating
 * a new customer via the company lookup endpoint.
 *
 * P5.2: the dropdown itself is a server-rendered hx-get (GET
 * /v2/partials/requisitions/customer-typeahead, swapped into #customer-typeahead-results
 * by unified_modal.html's search input) — there is no more client-side
 * companies/filtered array or fetchCompanies() preload; select()/selectById()/
 * clear() are unchanged (called from the swapped-in results' @click, or from the
 * customer-created listener below).
 *
 * Usage: x-data="customerPicker()" on a container div.
 * The container must include a <div data-lookup-result></div> for lookup results.
 *
 * Called by: requisitions/unified_modal.html
 * Depends on: /v2/partials/requisitions/customer-typeahead, /v2/partials/customers/lookup
 */
Alpine.data('customerPicker', () => ({
    query: '',
    open: false,
    selectedSiteId: '',
    selectedName: '',
    addNew: false,
    newName: '',
    newLocation: '',
    lookingUp: false,
    _onCustomerCreated: null,
    init() {
        // Listen for customer-created event from quick-create
        this._onCustomerCreated = (e) => {
            this.selectById(e.detail.siteId, e.detail.displayName);
        };
        document.addEventListener('customer-created', this._onCustomerCreated);
    },
    destroy() {
        if (this._onCustomerCreated) {
            document.removeEventListener('customer-created', this._onCustomerCreated);
        }
    },
    select(company, site) {
        this.selectedSiteId = site.id || '';
        this.selectedName = site.id ? company.name + ' \u2014 ' + site.site_name : company.name;
        this.open = false;
        this.query = '';
        this.addNew = false;
    },
    selectById(siteId, displayName) {
        this.selectedSiteId = siteId;
        this.selectedName = displayName;
        this.addNew = false;
    },
    clear() {
        this.selectedSiteId = '';
        this.selectedName = '';
        this.query = '';
    },
    async lookupCompany() {
        this.lookingUp = true;
        const resultEl = this.$el.querySelector('[data-lookup-result]');
        if (!resultEl) {
            console.error('customerPicker: [data-lookup-result] element not found');
            this.lookingUp = false;
            return;
        }
        try {
            const formData = new FormData();
            formData.append('company_name', this.newName);
            formData.append('location', this.newLocation);
            const resp = await fetch('/v2/partials/customers/lookup', { method: 'POST', body: formData });
            if (!resp.ok) {
                resultEl.textContent = `Lookup failed (${resp.status}). Try again.`;
                resultEl.classList.add('text-xs', 'text-rose-500');
                this.lookingUp = false;
                return;
            }
            // Server HTML is trusted (same-origin, auth-protected endpoint)
            resultEl.replaceChildren();
            resultEl.insertAdjacentHTML('afterbegin', await resp.text());
            htmx.process(resultEl);
        } catch (e) {
            console.error('Customer lookup failed:', e);
            resultEl.textContent = 'Lookup failed. Try again.';
            resultEl.classList.add('text-xs', 'text-rose-500');
        }
        this.lookingUp = false;
    }
}));

Alpine.data('unifiedReqModal', () => ({
    // Metadata
    reqName: '',
    customerSiteId: '',
    customerName: '',
    deadline: '',
    urgency: 'normal',
    // Input mode
    inputMode: 'paste',
    rawText: '',
    // State
    parsing: false,
    saving: false,
    parseError: '',
    parts: [],
    activePartIdx: 0,
    showBulkFill: false,
    init() {
        this.addBlankPart();
    },
    focusPart(idx) {
        this.activePartIdx = idx;
    },
    get errorCount() {
        return this.parts.filter(p => p.primary_mpn && !p.manufacturer).length;
    },
    get validCount() {
        return this.parts.filter(p => p.primary_mpn && p.manufacturer).length;
    },
    get hasErrors() {
        return this.errorCount > 0;
    },
    /** Build a sub object for a substitute part. */
    _makeSub(src) {
        if (typeof src === 'string') return { mpn: src, manufacturer: '', revision: '', hardware_codes: '' };
        return {
            mpn: src?.mpn || src?.primary_mpn || '',
            manufacturer: src?.manufacturer || '',
            revision: src?.revision || '',
            hardware_codes: src?.hardware_codes || '',
        };
    },
    /** Build a part row object, optionally seeded from AI-parsed data. */
    _makePart(src) {
        const subs = (src?.substitutes || []).map(s => this._makeSub(s));
        return {
            _id: Date.now() + Math.random(),
            primary_mpn: src?.primary_mpn || '',
            manufacturer: src?.manufacturer || '',
            target_qty: src?.target_qty || 1,
            brand: src?.brand || '',
            condition: src?.condition || 'new',
            target_price: src?.target_price || '',
            customer_pn: src?.customer_pn || '',
            date_codes: src?.date_codes || '',
            packaging: src?.packaging || '',
            firmware: src?.firmware || '',
            hardware_codes: src?.hardware_codes || '',
            description: src?.description || '',
            package_type: src?.package_type || '',
            revision: src?.revision || '',
            need_by_date: src?.need_by_date || '',
            sale_notes: src?.notes || src?.sale_notes || '',
            substitutes: subs,
            showSubs: subs.length > 0,
            noteOpen: false,
        };
    },
    addBlankPart() {
        this.parts.push(this._makePart());
    },
    addSub(part) {
        const target = part || this.parts[this.activePartIdx] || this.parts[0];
        if (!target) return;
        target.substitutes.push(this._makeSub());
        target.showSubs = true;
    },
    addSubToActive() {
        this.addSub(this.parts[this.activePartIdx]);
    },
    removeSub(part, idx) {
        part.substitutes.splice(idx, 1);
        if (part.substitutes.length === 0) part.showSubs = false;
    },
    removePart(idx) {
        this.parts.splice(idx, 1);
        if (this.activePartIdx >= this.parts.length) this.activePartIdx = Math.max(0, this.parts.length - 1);
    },
    async standardizeDescription(part) {
        const raw = (part.description || '').trim();
        if (!raw || raw.length < 3) {
            // No user description — auto-generate from MPN if available
            const mpn = (part.primary_mpn || '').trim();
            if (mpn.length >= 3) {
                await this.generateDescription(part);
            }
            return;
        }
        try {
            const resp = await fetch('/api/ai/standardize-description', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    description: raw,
                    mpn: part.primary_mpn || '',
                    manufacturer: part.manufacturer || '',
                }),
            });
            if (resp.ok) {
                const data = await resp.json();
                if (data.description) part.description = data.description;
            }
        } catch (e) {
            console.warn('Description standardize failed:', e);
        }
    },
    async generateDescription(part) {
        const mpn = (part.primary_mpn || '').trim();
        if (!mpn || mpn.length < 3) return;
        try {
            const resp = await fetch('/api/ai/generate-description', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mpn: mpn,
                    manufacturer: part.manufacturer || '',
                    existing_description: part.description || '',
                }),
            });
            if (resp.ok) {
                const data = await resp.json();
                if (data.description && data.confidence >= 0.75) {
                    part.description = data.description;
                }
            }
        } catch (e) {
            console.warn('Description generate failed:', e);
        }
    },
    async parseWithAI() {
        this.parsing = true;
        this.parseError = '';
        try {
            const formData = new FormData();
            formData.append('name', this.reqName || 'Untitled');
            formData.append('raw_text', this.rawText);
            formData.append('customer_name', this.customerName || '');
            formData.append('customer_site_id', this.customerSiteId || '');
            formData.append('deadline', this.deadline || '');
            formData.append('urgency', this.urgency || 'normal');
            if (this.inputMode === 'upload' && this.$refs.fileInput?.files?.[0]) {
                formData.append('file', this.$refs.fileInput.files[0]);
            }
            const resp = await fetch('/v2/partials/requisitions/import-parse?format=json', {
                method: 'POST',
                body: formData,
            });
            if (!resp.ok) {
                this.parseError = resp.status === 401
                    ? 'Session expired. Please refresh and log in again.'
                    : `Server error (${resp.status}). Please try again.`;
                this.parsing = false;
                return;
            }
            const data = await resp.json();
            if (data.error) {
                this.parseError = data.error;
            } else {
                const parsed = (data.requirements || []).map(r => this._makePart(r));
                if (parsed.length === 0) {
                    this.parseError = 'No parts could be extracted. Try a different format.';
                } else {
                    // Remove empty rows, then append parsed parts
                    this.parts = this.parts.filter(p => p.primary_mpn.trim());
                    this.parts.push(...parsed);
                    this.showBulkFill = false;
                    this.rawText = '';
                }
                if (data.inferred_name && !this.reqName.trim()) {
                    this.reqName = data.inferred_name;
                }
                if (data.inferred_customer && !this.customerName.trim()) {
                    this.customerName = data.inferred_customer;
                }
            }
        } catch (e) {
            console.error('parseWithAI error:', e);
            this.parseError = 'Network error — check your connection and try again.';
        }
        this.parsing = false;
    },
}));

// ── quoteBuilderTab: the ONE quote builder (single-stage inline) ──
// Drives the Build-Quote tab on the requisition detail page AND the combined multi-req
// builder in the global modal (quote_builder/combined.html) — the old two-panel
// quoteBuilder modal component is deleted (Wave 3). `data` is a plain reactive object
// keyed by requirement id, seeded inline by the server template (best cost, best-offer
// id, sell seed, qty, mpn/mfr/condition per line): check a line -> sell-price seeds ->
// live margin + guardrail -> Assemble posts a QuoteBuilderLine[] payload to the
// server-rendered assemble endpoint (single or /multi).
Alpine.data('quoteBuilderTab', (reqId, hasCustomerSite, minMarginPct, quoteExists, data) => ({
  reqId,
  hasCustomerSite,
  minMarginPct: minMarginPct || 10,
  quoteExists: !!quoteExists,
  markupPct: 20,
  data: data || {},

  // ── Per-line getters (reuse the modal's margin definition) ──
  _sell(id) {
    const l = this.data[id];
    const v = parseFloat(l && l.price);
    return Number.isFinite(v) ? v : null;
  },
  marginPct(id) {
    const l = this.data[id];
    const sell = this._sell(id);
    if (!l || sell === null || sell <= 0 || l.cost === null) return null;
    return (sell - l.cost) / sell * 100;
  },
  marginClass(id) {
    const m = this.marginPct(id);
    if (m === null) return 'text-gray-300';
    if (m >= 25) return 'text-emerald-600';
    if (m >= this.minMarginPct) return 'text-amber-600';
    return 'text-rose-600';
  },
  guardrail(id) {
    const l = this.data[id];
    const sell = this._sell(id);
    if (!l || sell === null || sell <= 0 || l.cost === null) return null;
    if (sell < l.cost) return 'below cost';
    const m = (sell - l.cost) / sell * 100;
    if (m < this.minMarginPct) return 'thin margin';
    return null;
  },

  // ── Selection + blended rollup ──
  _sellOf(l) {
    const v = parseFloat(l.price);
    return Number.isFinite(v) && v > 0 ? v : null;
  },
  _selected() { return Object.values(this.data).filter(l => l.sel && this._sellOf(l) !== null); },
  anySelected() { return Object.values(this.data).some(l => l.sel); },
  get selectedCount() { return Object.values(this.data).filter(l => l.sel).length; },
  get totalSell() {
    return this._selected().reduce((sum, l) => sum + this._sellOf(l) * (l.qty || 0), 0);
  },
  get totalCost() {
    return this._selected().reduce((sum, l) => sum + (l.cost || 0) * (l.qty || 0), 0);
  },
  get blendedMargin() {
    const sell = this.totalSell;
    if (sell <= 0) return null;
    return (sell - this.totalCost) / sell * 100;
  },
  get blendedMarginClass() {
    const m = this.blendedMargin;
    if (m === null) return 'text-gray-300';
    if (m >= 25) return 'text-emerald-600';
    if (m >= this.minMarginPct) return 'text-amber-600';
    return 'text-rose-600';
  },
  get blendedWarning() {
    const m = this.blendedMargin;
    if (m === null) return null;
    if (this.totalSell < this.totalCost) return 'Blended quote is below cost.';
    if (m < this.minMarginPct) return `Blended margin ${m.toFixed(1)}% is below the ${this.minMarginPct}% floor.`;
    return null;
  },

  // ── Actions ──
  applyMarkup() {
    const factor = 1 + (this.markupPct || 0) / 100;
    Object.values(this.data).forEach(l => {
      if (l.cost !== null) l.price = (l.cost * factor).toFixed(4);
    });
  },

  // Pick WHICH offer this line uses (default = best). Sets the chosen offerId (persisted on
  // the QuoteLine, and the buy-plan default at build time) and re-points cost to that
  // offer's price so the live margin reflects the offer actually being quoted. Vendor
  // identity never leaves the builder — the customer doc strips it (quote_export_context).
  selectOffer(id, offerId) {
    const l = this.data[id];
    if (!l) return;
    const oid = parseInt(offerId, 10);
    l.offerId = Number.isFinite(oid) ? oid : null;
    const chosen = (l.offers || []).find(o => o.id === l.offerId);
    if (chosen) l.cost = chosen.cost;
  },

  payload() {
    return JSON.stringify(
      Object.entries(this.data)
        .filter(([id, l]) => l.sel && this._sellOf(l) !== null)
        .map(([id, l]) => {
          const sell = this._sellOf(l);
          const cost = l.cost || 0;
          const margin = sell > 0 ? parseFloat(((sell - cost) / sell * 100).toFixed(2)) : 0;
          return {
            requirement_id: Number(id),
            offer_id: l.offerId,
            mpn: l.mpn,
            manufacturer: l.mfr,
            qty: l.qty || 0,
            cost_price: cost,
            sell_price: sell,
            margin_pct: margin,
            condition: l.cond,
          };
        })
    );
  },
}));
