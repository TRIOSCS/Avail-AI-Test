/**
 * materials_filter.js — the faceted materials search Alpine.data component
 * (commodity, sub-filters, confidence groups, sourcing signals, URL-bound
 * state) plus its persistOr helper and the one-time
 * 'mat_confidence_open' localStorage key migration.
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: alpinejs ($persist when registered).
 */

import Alpine from 'alpinejs';

/* Faceted materials search — Alpine.js component.
 * Manages commodity, sub-filters, search query, pagination.
 * URL is the canonical source of truth (back button, deep links work).
 */
// $persist when the plugin is registered (browser, before Alpine.start); plain default
// otherwise (vitest mocks / plugin absent) — never throws at factory-call time.
const persistOr = (def, key) => (typeof Alpine !== 'undefined' && Alpine.$persist) ? Alpine.$persist(def).as(key) : def;

// One-time storage migration: the confidence fold default flipped false→true, but
// @alpinejs/persist writes the CURRENT value to storage on init — so every browser that
// ever loaded the page under the old `persistOr(false, 'mat_confidence_open')` carries a
// persisted `false` that would override the new default. The fold state moved to
// 'mat_confidence_open2'; drop the dead key so a revert can't resurrect it.
if (typeof localStorage !== 'undefined') localStorage.removeItem('mat_confidence_open');

Alpine.data('materialsFilter', () => ({
  commodity: '',
  subFilters: {},
  q: '',
  page: 0,
  drawerOpen: false,
  displayNames: {},
  // Data-confidence selection — the flat list of enrichment tiers sent to the backend.
  // Surfaced as 3 user-facing checkboxes (see CONFIDENCE_GROUPS). Default = all tiers on
  // (the filter only narrows; the page opens showing everything).
  statuses: ['verified', 'web_sourced', 'oem_sourced', 'ai_inferred', 'not_catalogued', 'not_found', 'unenriched'],
  // Global facets — MaterialCard columns (OR-within each).
  lifecycle: [],
  rohs: [],
  condition: [],
  hasDatasheet: false,
  // Sourcing signals (Layer-3 operational filters) — MaterialCard + vendor history.
  hasStock: false,
  hasPrice: false,
  hasCrosses: false,
  internal: 'all',            // 'all' | 'standard' | 'internal'
  searchedWithin: 'any',      // '7d' | '30d' | '90d' | 'any'
  minSearches: 0,
  _onPopstate: null,

  // ── Direction-B UI state ─────────────────────────────────────────────
  // Hoisted sub-filter UI state (fold / typeahead text) so it survives HTMX re-renders of
  // #subfilters-container on every filters-changed. Keyed by spec_key; session-scoped.
  ui: { moreOpen: false, facetExpanded: {}, facetSearch: {} },
  // Type-to-find over the category tree (client-side filter; see tree.html).
  categorySearch: '',
  // Transient "Copied" flash for the copy-link control.
  copied: false,
  // Persisted CHROME only (layout prefs); filter STATE stays URL-bound.
  recentCommodities: persistOr([], 'mat_recent_commodities'),
  moreAttrsOpen: persistOr(false, 'mat_more_attrs_open'),
  sourcingOpen: persistOr(false, 'mat_sourcing_open'),
  // Confidence fold (first filter fold) opens by default — trust is the headline
  // filter; the heavy folds (sourcing / more attributes) stay closed until opened.
  // Key is the rotated 'mat_confidence_open2' so the new open default actually reaches
  // returning users (see the legacy-key removal above persistOr's call sites).
  confidenceOpen: persistOr(true, 'mat_confidence_open2'),

  // 3 user-facing confidence groups, each expanding to a set of enrichment tiers.
  // Array order pins the visual ordering of the Data-confidence section.
  CONFIDENCE_GROUPS: [
    { key: 'trusted', label: 'Trusted', dot: 'bg-emerald-500', tiers: ['verified', 'web_sourced', 'oem_sourced'] },
    { key: 'ai_inferred', label: 'AI-inferred', dot: 'bg-amber-500', tiers: ['ai_inferred'] },
    { key: 'no_data', label: 'No data', dot: 'bg-gray-400', tiers: ['not_catalogued', 'not_found', 'unenriched'] },
  ],
  // Derived from the groups so the tier set has a single source of truth.
  get DEFAULT_STATUSES() {
    return this.CONFIDENCE_GROUPS.flatMap(g => g.tiers);
  },

  // Sourcing-signal vocabularies — the single front-end source of truth as
  // [value, label] pairs (incl. the no-op sentinel 'all'/'any'). Rendered by
  // workspace.html's x-for templates and consulted by syncFromURL + the setters.
  // Backend twin (must stay in sync): INTERNAL_FILTER_VALUES / SEARCHED_WITHIN_VALUES
  // in app/services/faceted_search_service.py — the route logs a WARNING and degrades
  // to the sentinel when the vocabularies drift.
  INTERNAL_MODES: [['all', 'All'], ['standard', 'Standard MPNs'], ['internal', 'Internal parts']],
  SEARCH_BUCKETS: [['7d', '7d'], ['30d', '30d'], ['90d', '90d'], ['any', 'Any']],

  get commodityDisplayName() {
    if (!this.commodity) return '';
    return this.displayNames[this.commodity]
      || this.commodity.replace(/_/g, ' ').replace(/(^|\s)\S/g, l => l.toUpperCase());
  },

  // True when the confidence selection is narrowed from the all-on default.
  get confidenceNarrowed() {
    return !(this.statuses.length === this.DEFAULT_STATUSES.length
      && this.DEFAULT_STATUSES.every(s => this.statuses.includes(s)));
  },

  _groupChecked(group) {
    return group.tiers.every(t => this.statuses.includes(t));
  },

  // Fully-checked confidence groups — surfaced as active chips, but only when narrowed.
  get activeConfidenceGroups() {
    if (!this.confidenceNarrowed) return [];
    return this.CONFIDENCE_GROUPS.filter(g => this._groupChecked(g));
  },

  confidenceGroupChecked(groupKey) {
    const group = this.CONFIDENCE_GROUPS.find(g => g.key === groupKey);
    return !!group && this._groupChecked(group);
  },

  toggleConfidenceGroup(groupKey) {
    const group = this.CONFIDENCE_GROUPS.find(g => g.key === groupKey);
    if (!group) return;
    if (this._groupChecked(group)) {
      this.statuses = this.statuses.filter(s => !group.tiers.includes(s));
    } else {
      for (const t of group.tiers) {
        if (!this.statuses.includes(t)) this.statuses.push(t);
      }
    }
    this.applyFilters();
  },

  // Active selections inside the "Sourcing signals" section (for its badge + chips).
  get sourcingActiveCount() {
    return (this.hasStock ? 1 : 0) + (this.hasPrice ? 1 : 0) + (this.hasCrosses ? 1 : 0)
      + (this.internal !== 'all' ? 1 : 0)
      + (this.searchedWithin !== 'any' ? 1 : 0)
      + (this.minSearches > 0 ? 1 : 0);
  },

  get activeFilterCount() {
    let count = 0;
    for (const [key, val] of Object.entries(this.subFilters)) {
      if (Array.isArray(val)) count += val.length;
      else if (val !== '' && val !== null) count += 1;
    }
    count += this.activeConfidenceGroups.length;
    count += this.lifecycle.length;
    count += this.rohs.length;
    count += this.condition.length;
    if (this.hasDatasheet) count += 1;
    count += this.sourcingActiveCount;
    return count;
  },

  // Active selections inside the collapsed "More attributes" section (for its badge).
  get attributesActiveCount() {
    return this.lifecycle.length + this.rohs.length + this.condition.length
      + (this.hasDatasheet ? 1 : 0)
      + (Array.isArray(this.subFilters.manufacturers) ? this.subFilters.manufacturers.length : 0);
  },

  // Top summary "Clear all" — resets every filter but KEEPS the selected commodity
  // (commodity is navigation, not a filter). The spec-scoped control is "Clear specs".
  clearAllFilters() {
    this.subFilters = {};
    this.lifecycle = [];
    this.rohs = [];
    this.condition = [];
    this.hasDatasheet = false;
    this.hasStock = false;
    this.hasPrice = false;
    this.hasCrosses = false;
    this.internal = 'all';
    this.searchedWithin = 'any';
    this.minSearches = 0;
    this.statuses = [...this.DEFAULT_STATUSES];
    this.q = '';
    this.ui.facetSearch = {};
    this.ui.facetExpanded = {};
    this.applyFilters();
  },

  // True when the type-to-find query matches at least one known category (else show a
  // "no matches" hint instead of a blank tree). Over displayNames — the dominant
  // gibberish/typo no-match case; a query is "" → always true.
  get anyCategoryMatches() {
    if (!this.categorySearch) return true;
    const t = this.categorySearch.toLowerCase();
    return Object.values(this.displayNames).some(n => String(n).toLowerCase().includes(t));
  },

  copyLink() {
    const url = window.location.href;
    const flash = () => { this.copied = true; setTimeout(() => { this.copied = false; }, 1500); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(url).then(flash).catch(() => window.prompt('Copy this link:', url));
    } else {
      window.prompt('Copy this link:', url);  // clipboard API unavailable (HTTP / old browser)
    }
  },

  init() {
    try { this.displayNames = JSON.parse(this.$el.dataset.displayNames || '{}'); } catch (e) { this.displayNames = {}; }
    this.syncFromURL();
    this._onPopstate = () => this.syncFromURL();
    window.addEventListener('popstate', this._onPopstate);
  },

  destroy() {
    if (this._onPopstate) {
      window.removeEventListener('popstate', this._onPopstate);
    }
  },

  syncFromURL() {
    try {
      const params = new URLSearchParams(window.location.search);
      this.commodity = params.get('commodity') || '';
      this.q = params.get('q') || '';
      // Trust ladder: explicit `statuses` CSV wins; otherwise fall back to the
      // trustworthy default. (Legacy verified_only/web_sourced links still map in.)
      const statusesParam = params.get('statuses');
      if (statusesParam !== null) {
        this.statuses = statusesParam.split(',').filter(s => s !== '');
      } else {
        const legacy = [];
        if (params.get('verified_only') === 'true') legacy.push('verified');
        if (params.get('web_sourced') === 'true') legacy.push('web_sourced');
        this.statuses = legacy.length > 0 ? legacy : [...this.DEFAULT_STATUSES];
      }
      this.lifecycle = (params.get('lifecycle') || '').split(',').filter(s => s !== '');
      this.rohs = (params.get('rohs') || '').split(',').filter(s => s !== '');
      this.condition = (params.get('condition') || '').split(',').filter(s => s !== '');
      this.hasDatasheet = params.get('has_datasheet') === 'true';
      this.hasStock = params.get('has_stock') === 'true';
      this.hasPrice = params.get('has_price') === 'true';
      this.hasCrosses = params.get('has_crosses') === 'true';
      const internalParam = params.get('internal');
      this.internal = this.INTERNAL_MODES.some(([v]) => v === internalParam) ? internalParam : 'all';
      const withinParam = params.get('searched_within');
      this.searchedWithin = this.SEARCH_BUCKETS.some(([v]) => v === withinParam) ? withinParam : 'any';
      const minSearchesVal = parseInt(params.get('min_searches') || '0', 10);
      this.minSearches = (isNaN(minSearchesVal) || minSearchesVal < 0) ? 0 : minSearchesVal;
      const pageVal = parseInt(params.get('page') || '0', 10);
      this.page = isNaN(pageVal) ? 0 : pageVal;
      this.subFilters = {};
      for (const [key, val] of params.entries()) {
        if (key.startsWith('sf_')) {
          const specKey = key.slice(3);
          try {
            if (specKey.endsWith('__vals')) {
              // Numeric common-value chips (P2): a comma-joined number list.
              // Coerce each to a number and drop NaN so the chip :class membership
              // check (which compares numbers) and the value_numeric IN predicate stay
              // numeric — string entries would silently never match.
              // Drop empty segments BEFORE coercion: Number('') === 0 (not NaN), so a
              // malformed/truncated link like "8," would otherwise inject a phantom 0.
              const nums = val.split(',').filter(s => s !== '').map(Number).filter(n => !isNaN(n));
              if (nums.length > 0) {
                this.subFilters[specKey] = nums;
              }
            } else if (specKey.endsWith('_min') || specKey.endsWith('_max')) {
              const num = parseFloat(val);
              if (!isNaN(num)) {
                this.subFilters[specKey] = num;
              }
            } else {
              const items = val.split(',').filter(s => s !== '');
              if (items.length > 0) {
                this.subFilters[specKey] = items;
              }
            }
          } catch (e) {
            // Ignore unparseable sf_ param
          }
        }
      }
    } catch (e) {
      console.warn('[materialsFilter] Broken URL — resetting filters', e);
      // Broken URL — reset to defaults
      this.commodity = '';
      this.q = '';
      this.statuses = [...this.DEFAULT_STATUSES];
      this.lifecycle = [];
      this.rohs = [];
      this.condition = [];
      this.hasDatasheet = false;
      this.hasStock = false;
      this.hasPrice = false;
      this.hasCrosses = false;
      this.internal = 'all';
      this.searchedWithin = 'any';
      this.minSearches = 0;
      this.page = 0;
      this.subFilters = {};
    }
  },

  pushURL(push = false) {
    const params = new URLSearchParams();
    if (this.commodity) params.set('commodity', this.commodity);
    if (this.q) params.set('q', this.q);
    // Persist the trust ladder only when it differs from the default set, so
    // clean URLs stay clean. An empty selection is meaningful → always written.
    if (this.confidenceNarrowed) params.set('statuses', this.statuses.join(','));
    if (this.lifecycle.length > 0) params.set('lifecycle', this.lifecycle.join(','));
    if (this.rohs.length > 0) params.set('rohs', this.rohs.join(','));
    if (this.condition.length > 0) params.set('condition', this.condition.join(','));
    if (this.hasDatasheet) params.set('has_datasheet', 'true');
    if (this.hasStock) params.set('has_stock', 'true');
    if (this.hasPrice) params.set('has_price', 'true');
    if (this.hasCrosses) params.set('has_crosses', 'true');
    if (this.internal !== 'all') params.set('internal', this.internal);
    if (this.searchedWithin !== 'any') params.set('searched_within', this.searchedWithin);
    if (this.minSearches > 0) params.set('min_searches', this.minSearches);
    if (this.page > 0) params.set('page', this.page);
    for (const [key, val] of Object.entries(this.subFilters)) {
      if (Array.isArray(val) && val.length > 0) {
        params.set('sf_' + key, val.join(','));
      } else if (typeof val === 'number' && !isNaN(val)) {
        params.set('sf_' + key, val);
      }
    }
    const search = params.toString();
    const url = window.location.pathname + (search ? '?' + search : '');
    const method = push ? 'pushState' : 'replaceState';
    history[method]({}, '', url);
  },

  selectCommodity(commodity) {
    this.commodity = commodity || '';
    this.subFilters = {};
    // Reset hoisted per-facet UI so a previous commodity's typeahead text / fold (keyed by a
    // shared spec_key like "package") can't silently filter the new commodity's facets.
    this.ui.facetSearch = {};
    this.ui.facetExpanded = {};
    this.ui.moreOpen = false;
    if (this.commodity) {
      // Most-recent-first, deduped, capped at 5 (persisted navigation history).
      const list = this.recentCommodities.filter(x => x !== this.commodity);
      list.unshift(this.commodity);
      this.recentCommodities = list.slice(0, 5);
    }
    document.body.dispatchEvent(new CustomEvent('commodity-changed'));
    this.applyFilters();
  },

  // Global-facet array toggle (lifecycle / rohs). OR-within each facet.
  toggleGlobalFacet(facet, value) {
    const arr = this[facet];
    if (!Array.isArray(arr)) return;
    const idx = arr.indexOf(value);
    if (idx >= 0) arr.splice(idx, 1);
    else arr.push(value);
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  toggleDatasheet() {
    this.hasDatasheet = !this.hasDatasheet;
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Sourcing-signal boolean toggle (hasStock / hasPrice / hasCrosses).
  toggleSourcingFlag(flag) {
    if (!['hasStock', 'hasPrice', 'hasCrosses'].includes(flag)) {
      console.warn(`materialsFilter: unknown sourcing flag ${flag}`);
      return;
    }
    this[flag] = !this[flag];
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Internal-vs-standard segmented control ('all' | 'standard' | 'internal').
  setInternal(mode) {
    this.internal = this.INTERNAL_MODES.some(([v]) => v === mode) ? mode : 'all';
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Recently-searched chips ('7d' | '30d' | '90d' | 'any'). Re-clicking the active
  // bucket resets to 'any'.
  setSearchedWithin(bucket) {
    const next = this.SEARCH_BUCKETS.some(([v]) => v === bucket) ? bucket : 'any';
    this.searchedWithin = (this.searchedWithin === next) ? 'any' : next;
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Min-searches numeric input (0 = off).
  setMinSearches(value) {
    const num = parseInt(value, 10);
    this.minSearches = (isNaN(num) || num < 0) ? 0 : num;
    if (window.innerWidth >= 1024) this.applyFilters();
  },

  // Chip removal for a global facet — always re-applies (explicit user action).
  removeGlobalFacet(facet, value) {
    const arr = this[facet];
    if (!Array.isArray(arr)) return;
    const idx = arr.indexOf(value);
    if (idx >= 0) arr.splice(idx, 1);
    this.applyFilters();
  },

  toggleFilter(specKey, value) {
    if (!this.subFilters[specKey]) {
      this.subFilters[specKey] = [value];
    } else {
      const idx = this.subFilters[specKey].indexOf(value);
      if (idx >= 0) {
        this.subFilters[specKey].splice(idx, 1);
        if (this.subFilters[specKey].length === 0) {
          delete this.subFilters[specKey];
        }
      } else {
        this.subFilters[specKey].push(value);
      }
    }
    if (window.innerWidth >= 1024) {
      this.applyFilters();
    }
  },

  // Numeric common-value chip toggle (P2). Maintains subFilters[specKey + '__vals']
  // as an array of NUMBERS — the backend predicate is value_numeric IN (...), and the
  // chip :class membership check (.includes()) compares against JS numbers. Mirrors
  // toggleFilter's add/remove + delete-when-empty shape; the value is server-rendered
  // from value_numeric (chip.value|tojson), so it is always a number.
  toggleNumericChip(specKey, value) {
    const key = specKey + '__vals';
    if (!this.subFilters[key]) {
      this.subFilters[key] = [value];
    } else {
      const idx = this.subFilters[key].indexOf(value);
      if (idx >= 0) {
        this.subFilters[key].splice(idx, 1);
        if (this.subFilters[key].length === 0) {
          delete this.subFilters[key];
        }
      } else {
        this.subFilters[key].push(value);
      }
    }
    if (window.innerWidth >= 1024) {
      this.applyFilters();
    }
  },

  setRange(specKey, bound, value) {
    const key = specKey + '_' + bound;
    if (value === '' || value === null) {
      delete this.subFilters[key];
    } else {
      this.subFilters[key] = parseFloat(value);
    }
    if (window.innerWidth >= 1024) {
      this.applyFilters();
    }
  },

  removeFilter(key, val) {
    if (Array.isArray(this.subFilters[key])) {
      this.subFilters[key] = this.subFilters[key].filter(v => v !== val);
      if (this.subFilters[key].length === 0) delete this.subFilters[key];
    } else {
      delete this.subFilters[key];
    }
    this.applyFilters();
  },

  clearSubFilters() {
    this.subFilters = {};
    this.applyFilters();
  },

  applyFilters() {
    this.page = 0;
    this.pushURL();
    document.body.dispatchEvent(new CustomEvent('filters-changed'));
  },

  goToPage(newPage) {
    this.page = newPage;
    this.pushURL(true);
    document.body.dispatchEvent(new CustomEvent('filters-changed'));
  },
}));
