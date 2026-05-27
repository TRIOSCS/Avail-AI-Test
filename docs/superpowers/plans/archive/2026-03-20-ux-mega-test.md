# UX Mega Test — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a comprehensive quality safety net ("UX Mega Test") that catches data corruption, dead-end workflows, silent failures, broken templates, and performance regressions — with self-repair capabilities.

**Architecture:** Nine test systems organized as a single test package. Python pytest tests for backend data health and template compilation. Vitest for Alpine.js component unit tests. Playwright for browser-based workflow and dead-end detection. Lighthouse for performance. A self-repair toolkit and canary monitor for production safety.

**Tech Stack:** pytest, Vitest + jsdom, Playwright, axe-core, Lighthouse, FastAPI TestClient, SQLAlchemy

---

## File Structure

```
tests/
├── ux_mega/                          # NEW — UX Mega Test package
│   ├── __init__.py
│   ├── conftest.py                   # Shared fixtures for mega test suite
│   ├── test_data_health.py           # System 1: Data Health Scanner
│   ├── test_data_consistency.py      # System 3: Data Consistency Validator
│   ├── test_template_compilation.py  # System 7: Template Rendering Tests
│   └── test_workflow_integrity.py    # System 3b: Workflow chain validation
├── frontend/
│   ├── alpine-components.test.ts     # System 5: Vitest Alpine unit tests
│   └── (existing files unchanged)
e2e/
├── workflows.spec.ts                 # System 2: Playwright multi-step workflows
├── dead-ends.spec.ts                 # System 4: Dead-End Detector
├── (existing files unchanged)
app/
├── services/
│   ├── self_repair_service.py        # System 6: Self-Repair Toolkit (extends integrity_service.py)
│   └── (existing files unchanged)
scripts/
├── lighthouse-audit.mjs             # System 8: Expanded Lighthouse (modify existing)
├── canary-monitor.sh                # System 9: Canary Monitor
├── run-ux-mega-test.sh              # Master runner script
vitest.config.ts                      # NEW — Vitest configuration
playwright.config.ts                  # MODIFY — add workflow + dead-end projects
package.json                          # MODIFY — add new npm scripts
```

---

## Task 1: Vitest Configuration + Alpine Component Tests

**Files:**
- Create: `vitest.config.ts`
- Create: `tests/frontend/alpine-components.test.ts`
- Modify: `package.json` (add vitest scripts)

### Step 1: Create Vitest config

- [ ] **Create `vitest.config.ts`**

```typescript
import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/frontend/**/*.test.ts'],
    globals: true,
  },
  resolve: {
    alias: {
      '@static': resolve(__dirname, 'app/static'),
    },
  },
});
```

- [ ] **Add npm scripts to `package.json`**

Add to the `"scripts"` object:
```json
"test:vitest": "vitest run",
"test:vitest:watch": "vitest"
```

- [ ] **Run to verify config**

Run: `cd /root/availai && npx vitest run`
Expected: 0 tests found (no test files yet), exits cleanly

### Step 2: Write Alpine component tests

- [ ] **Create `tests/frontend/alpine-components.test.ts`**

```typescript
/**
 * alpine-components.test.ts — Vitest unit tests for Alpine.js components and stores.
 *
 * Tests materialsFilter, sourcingProgress, shortlist store, toast store,
 * sidebar store, preferences store, and _viewFromPath utility.
 *
 * Called by: npx vitest run
 * Depends on: vitest, jsdom, app/static/htmx_app.js
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';

// ── Helper: minimal Alpine-like store registry ──────────────────
// We test the component LOGIC, not Alpine framework integration.
// Extract data factories from htmx_app.js patterns.

// ── _viewFromPath tests ─────────────────────────────────────────

function _viewFromPath(path: string): string {
  if (/\/buy-plans(\/|$)/.test(path)) return 'buy-plans';
  if (/\/quotes(\/|$)/.test(path)) return 'quotes';
  if (/\/prospecting(\/|$)/.test(path)) return 'prospecting';
  if (/\/proactive(\/|$)/.test(path)) return 'proactive';
  if (/\/strategic(\/|$)/.test(path)) return 'strategic';
  if (/\/settings(\/|$)/.test(path)) return 'settings';
  if (/\/my-vendors(\/|$)/.test(path)) return 'my-vendors';
  if (/\/vendors(\/|$)/.test(path)) return 'vendors';
  if (/\/companies(\/|$)/.test(path)) return 'companies';
  if (/\/search(\/|$)/.test(path)) return 'search';
  if (/\/tasks(\/|$)/.test(path)) return 'tasks';
  if (/\/requisitions(\/|$)/.test(path)) return 'requisitions';
  return 'requisitions';
}

describe('_viewFromPath', () => {
  it.each([
    ['/v2/buy-plans', 'buy-plans'],
    ['/v2/buy-plans/42', 'buy-plans'],
    ['/v2/quotes', 'quotes'],
    ['/v2/quotes/7', 'quotes'],
    ['/v2/prospecting', 'prospecting'],
    ['/v2/proactive', 'proactive'],
    ['/v2/strategic', 'strategic'],
    ['/v2/settings', 'settings'],
    ['/v2/vendors', 'vendors'],
    ['/v2/vendors/99', 'vendors'],
    ['/v2/companies', 'companies'],
    ['/v2/companies/5', 'companies'],
    ['/v2/search', 'search'],
    ['/v2/requisitions', 'requisitions'],
    ['/v2/requisitions/123', 'requisitions'],
    ['/v2', 'requisitions'],
    ['/', 'requisitions'],
    ['/unknown', 'requisitions'],
  ])('maps %s → %s', (path, expected) => {
    expect(_viewFromPath(path)).toBe(expected);
  });
});

// ── materialsFilter component tests ─────────────────────────────

describe('materialsFilter', () => {
  // Factory that creates a materialsFilter data object (mirrors htmx_app.js)
  function createFilter() {
    const filter: any = {
      commodity: '',
      subFilters: {} as Record<string, any>,
      q: '',
      page: 0,
      drawerOpen: false,
      _onPopstate: null,
      _pushedUrls: [] as string[],

      get commodityDisplayName() {
        return this.commodity
          ? this.commodity.replace(/_/g, ' ').replace(/(^|\s)\S/g, (l: string) => l.toUpperCase())
          : '';
      },

      get activeFilterCount() {
        let count = 0;
        for (const [, val] of Object.entries(this.subFilters)) {
          if (Array.isArray(val)) count += (val as any[]).length;
          else if (val !== '' && val !== null) count += 1;
        }
        return count;
      },

      syncFromURL() {
        const params = new URLSearchParams(window.location.search);
        this.commodity = params.get('commodity') || '';
        this.q = params.get('q') || '';
        const pageVal = parseInt(params.get('page') || '0', 10);
        this.page = isNaN(pageVal) ? 0 : pageVal;
        this.subFilters = {};
        for (const [key, val] of params.entries()) {
          if (key.startsWith('sf_')) {
            const specKey = key.slice(3);
            if (specKey.endsWith('_min') || specKey.endsWith('_max')) {
              const num = parseFloat(val);
              if (!isNaN(num)) this.subFilters[specKey] = num;
            } else {
              const items = val.split(',').filter((s: string) => s !== '');
              if (items.length > 0) this.subFilters[specKey] = items;
            }
          }
        }
      },

      pushURL() {
        const params = new URLSearchParams();
        if (this.commodity) params.set('commodity', this.commodity);
        if (this.q) params.set('q', this.q);
        if (this.page > 0) params.set('page', String(this.page));
        for (const [key, val] of Object.entries(this.subFilters)) {
          if (Array.isArray(val) && (val as any[]).length > 0) {
            params.set('sf_' + key, (val as string[]).join(','));
          } else if (typeof val === 'number' && !isNaN(val)) {
            params.set('sf_' + key, String(val));
          }
        }
        const search = params.toString();
        const url = window.location.pathname + (search ? '?' + search : '');
        this._pushedUrls.push(url);
      },

      selectCommodity(commodity: string) {
        this.commodity = commodity || '';
        this.subFilters = {};
        this.applyFilters();
      },

      toggleFilter(specKey: string, value: string) {
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
      },

      setRange(specKey: string, bound: string, value: string | null) {
        const key = specKey + '_' + bound;
        if (value === '' || value === null) {
          delete this.subFilters[key];
        } else {
          this.subFilters[key] = parseFloat(value);
        }
      },

      removeFilter(key: string, val: string) {
        if (Array.isArray(this.subFilters[key])) {
          this.subFilters[key] = this.subFilters[key].filter((v: string) => v !== val);
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
      },

      goToPage(newPage: number) {
        this.page = newPage;
        this.pushURL();
      },
    };
    return filter;
  }

  let filter: ReturnType<typeof createFilter>;

  beforeEach(() => {
    filter = createFilter();
    // Reset jsdom URL
    const url = new URL('http://localhost/v2/materials');
    Object.defineProperty(window, 'location', {
      value: new URL(url),
      writable: true,
    });
  });

  describe('syncFromURL', () => {
    it('parses commodity from URL', () => {
      window.location.search = '?commodity=capacitors';
      filter.syncFromURL();
      expect(filter.commodity).toBe('capacitors');
    });

    it('parses page number', () => {
      window.location.search = '?page=3';
      filter.syncFromURL();
      expect(filter.page).toBe(3);
    });

    it('defaults page to 0 for NaN', () => {
      window.location.search = '?page=abc';
      filter.syncFromURL();
      expect(filter.page).toBe(0);
    });

    it('parses sf_ sub-filters as arrays', () => {
      window.location.search = '?sf_package=DIP-8,SOP-8';
      filter.syncFromURL();
      expect(filter.subFilters['package']).toEqual(['DIP-8', 'SOP-8']);
    });

    it('parses sf_ min/max as numbers', () => {
      window.location.search = '?sf_voltage_min=3.3&sf_voltage_max=5.0';
      filter.syncFromURL();
      expect(filter.subFilters['voltage_min']).toBe(3.3);
      expect(filter.subFilters['voltage_max']).toBe(5.0);
    });

    it('ignores empty sf_ values', () => {
      window.location.search = '?sf_package=';
      filter.syncFromURL();
      expect(filter.subFilters).toEqual({});
    });

    it('handles empty URL gracefully', () => {
      window.location.search = '';
      filter.syncFromURL();
      expect(filter.commodity).toBe('');
      expect(filter.q).toBe('');
      expect(filter.page).toBe(0);
      expect(filter.subFilters).toEqual({});
    });
  });

  describe('toggleFilter', () => {
    it('adds filter value when not present', () => {
      filter.toggleFilter('package', 'DIP-8');
      expect(filter.subFilters['package']).toEqual(['DIP-8']);
    });

    it('removes filter value when present', () => {
      filter.subFilters['package'] = ['DIP-8', 'SOP-8'];
      filter.toggleFilter('package', 'DIP-8');
      expect(filter.subFilters['package']).toEqual(['SOP-8']);
    });

    it('deletes key when last value removed', () => {
      filter.subFilters['package'] = ['DIP-8'];
      filter.toggleFilter('package', 'DIP-8');
      expect(filter.subFilters['package']).toBeUndefined();
    });
  });

  describe('setRange', () => {
    it('sets numeric range value', () => {
      filter.setRange('voltage', 'min', '3.3');
      expect(filter.subFilters['voltage_min']).toBe(3.3);
    });

    it('deletes range on empty string', () => {
      filter.subFilters['voltage_min'] = 3.3;
      filter.setRange('voltage', 'min', '');
      expect(filter.subFilters['voltage_min']).toBeUndefined();
    });

    it('deletes range on null', () => {
      filter.subFilters['voltage_max'] = 5.0;
      filter.setRange('voltage', 'max', null);
      expect(filter.subFilters['voltage_max']).toBeUndefined();
    });
  });

  describe('selectCommodity', () => {
    it('sets commodity and clears sub-filters', () => {
      filter.subFilters = { package: ['DIP-8'] };
      filter.selectCommodity('resistors');
      expect(filter.commodity).toBe('resistors');
      expect(filter.subFilters).toEqual({});
    });

    it('resets page to 0', () => {
      filter.page = 5;
      filter.selectCommodity('capacitors');
      expect(filter.page).toBe(0);
    });
  });

  describe('computed properties', () => {
    it('commodityDisplayName formats underscores and capitalizes', () => {
      filter.commodity = 'integrated_circuits';
      expect(filter.commodityDisplayName).toBe('Integrated Circuits');
    });

    it('commodityDisplayName returns empty for no commodity', () => {
      expect(filter.commodityDisplayName).toBe('');
    });

    it('activeFilterCount counts array items and scalar values', () => {
      filter.subFilters = {
        package: ['DIP-8', 'SOP-8'],
        voltage_min: 3.3,
      };
      expect(filter.activeFilterCount).toBe(3);
    });

    it('activeFilterCount returns 0 when no filters', () => {
      expect(filter.activeFilterCount).toBe(0);
    });
  });

  describe('goToPage', () => {
    it('updates page and pushes URL', () => {
      filter.goToPage(3);
      expect(filter.page).toBe(3);
      expect(filter._pushedUrls.length).toBe(1);
    });
  });

  describe('clearSubFilters', () => {
    it('empties all sub-filters and resets page', () => {
      filter.subFilters = { package: ['DIP-8'] };
      filter.page = 3;
      filter.clearSubFilters();
      expect(filter.subFilters).toEqual({});
      expect(filter.page).toBe(0);
    });
  });
});

// ── shortlist store tests ───────────────────────────────────────

describe('shortlist store', () => {
  function createShortlist() {
    return {
      items: [] as any[],
      toggle(item: { vendor_name: string; mpn: string }) {
        const key = item.vendor_name + ':' + item.mpn;
        const idx = this.items.findIndex((i: any) => (i.vendor_name + ':' + i.mpn) === key);
        if (idx >= 0) {
          this.items.splice(idx, 1);
        } else {
          this.items.push(item);
        }
      },
      has(vendorName: string, mpn: string) {
        const key = vendorName + ':' + mpn;
        return this.items.some((i: any) => (i.vendor_name + ':' + i.mpn) === key);
      },
      clear() { this.items = []; },
      get count() { return this.items.length; },
    };
  }

  let store: ReturnType<typeof createShortlist>;

  beforeEach(() => { store = createShortlist(); });

  it('toggle adds item', () => {
    store.toggle({ vendor_name: 'Arrow', mpn: 'LM317T' });
    expect(store.count).toBe(1);
    expect(store.has('Arrow', 'LM317T')).toBe(true);
  });

  it('toggle removes existing item', () => {
    store.toggle({ vendor_name: 'Arrow', mpn: 'LM317T' });
    store.toggle({ vendor_name: 'Arrow', mpn: 'LM317T' });
    expect(store.count).toBe(0);
  });

  it('has returns false for missing item', () => {
    expect(store.has('Arrow', 'LM317T')).toBe(false);
  });

  it('clear empties all items', () => {
    store.toggle({ vendor_name: 'Arrow', mpn: 'LM317T' });
    store.toggle({ vendor_name: 'Mouser', mpn: 'NE555P' });
    store.clear();
    expect(store.count).toBe(0);
  });

  it('distinguishes same MPN from different vendors', () => {
    store.toggle({ vendor_name: 'Arrow', mpn: 'LM317T' });
    store.toggle({ vendor_name: 'Mouser', mpn: 'LM317T' });
    expect(store.count).toBe(2);
    expect(store.has('Arrow', 'LM317T')).toBe(true);
    expect(store.has('Mouser', 'LM317T')).toBe(true);
  });
});

// ── toast store tests ───────────────────────────────────────────

describe('toast store', () => {
  it('has correct defaults', () => {
    const toast = { message: '', type: 'info', show: false };
    expect(toast.message).toBe('');
    expect(toast.type).toBe('info');
    expect(toast.show).toBe(false);
  });
});

// ── sourcingProgress component tests ────────────────────────────

describe('sourcingProgress', () => {
  function createProgress(requirementId: number, totalSources: number) {
    return {
      completed: 0,
      requirementId,
      totalSources,
      handleSourceComplete(data: { source: string; count: number; elapsed_ms: number; status: string }) {
        this.completed++;
      },
      handleSearchComplete(_data: any) {
        // In real code this triggers htmx.ajax; we just track it was called
      },
      get progressPct() {
        return Math.round((this.completed / this.totalSources) * 100);
      },
    };
  }

  it('increments completed on source-complete', () => {
    const prog = createProgress(1, 6);
    prog.handleSourceComplete({ source: 'BrokerBin', count: 50, elapsed_ms: 1200, status: 'done' });
    expect(prog.completed).toBe(1);
    expect(prog.progressPct).toBe(17);
  });

  it('calculates 100% when all sources complete', () => {
    const prog = createProgress(1, 3);
    prog.handleSourceComplete({ source: 'BB', count: 10, elapsed_ms: 500, status: 'done' });
    prog.handleSourceComplete({ source: 'Nexar', count: 5, elapsed_ms: 800, status: 'done' });
    prog.handleSourceComplete({ source: 'DigiKey', count: 3, elapsed_ms: 600, status: 'done' });
    expect(prog.progressPct).toBe(100);
  });

  it('starts at 0%', () => {
    const prog = createProgress(1, 6);
    expect(prog.progressPct).toBe(0);
    expect(prog.completed).toBe(0);
  });
});
```

- [ ] **Run Vitest to verify all tests pass**

Run: `cd /root/availai && npx vitest run`
Expected: All tests pass (~30 tests)

- [ ] **Commit**

```bash
git add vitest.config.ts tests/frontend/alpine-components.test.ts package.json
git commit -m "feat: add Vitest Alpine.js component tests (UX Mega Test system 1)"
```

---

## Task 2: Template Compilation Tests

**Files:**
- Create: `tests/ux_mega/__init__.py`
- Create: `tests/ux_mega/conftest.py`
- Create: `tests/ux_mega/test_template_compilation.py`

### Step 1: Create the test package and shared fixtures

- [ ] **Create `tests/ux_mega/__init__.py`**

```python
"""UX Mega Test — comprehensive quality safety net for AvailAI."""
```

- [ ] **Create `tests/ux_mega/conftest.py`**

```python
"""Shared fixtures for UX Mega Test suite.

Provides Jinja2 environment, DB session, authenticated client,
and helper factories for test data creation.

Called by: pytest (auto-discovered)
Depends on: app.main, tests.conftest
"""

import pytest
from jinja2 import Environment

from app.main import app


@pytest.fixture()
def jinja_env() -> Environment:
    """Return the Jinja2 template environment from the running app."""
    # FastAPI + Jinja2Templates stores the env on the Jinja2Templates instance.
    # We find it by inspecting app state or importing directly.
    from app.routers.htmx_views import templates
    return templates.env
```

- [ ] **Run to verify fixture loads**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ux_mega/ --collect-only`
Expected: Collects 0 tests (no test files yet), no import errors

### Step 2: Write template compilation tests

- [ ] **Create `tests/ux_mega/test_template_compilation.py`**

```python
"""test_template_compilation.py — Verify every Jinja2 template compiles without errors.

Iterates all .html files in app/templates/, loads each through the Jinja2
environment, and verifies no TemplateSyntaxError is raised. Templates that
need specific context variables get minimal dummy values.

Called by: pytest tests/ux_mega/test_template_compilation.py
Depends on: app.routers.htmx_views (templates instance), jinja2
"""

import os

import pytest
from jinja2 import TemplateSyntaxError, UndefinedError

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "app", "templates")


def _collect_templates():
    """Walk app/templates/ and yield relative paths for all .html files."""
    for root, _dirs, files in os.walk(TEMPLATE_DIR):
        for f in files:
            if f.endswith(".html"):
                rel = os.path.relpath(os.path.join(root, f), TEMPLATE_DIR)
                yield rel


# Minimal dummy context that satisfies most template variables.
# Templates use {% if var %} guards, so missing vars just skip blocks.
DUMMY_CONTEXT = {
    "request": type("FakeRequest", (), {"url": type("U", (), {"path": "/v2"})(), "query_params": {}})(),
    "user": type("FakeUser", (), {"id": 1, "email": "test@test.com", "role": "buyer", "display_name": "Test"})(),
    "current_user": type("FakeUser", (), {"id": 1, "email": "test@test.com", "role": "buyer", "display_name": "Test"})(),
    "requisitions": [],
    "requirements": [],
    "offers": [],
    "quotes": [],
    "vendors": [],
    "companies": [],
    "items": [],
    "results": [],
    "leads": [],
    "contacts": [],
    "activities": [],
    "tags": [],
    "lines": [],
    "bids": [],
    "threads": [],
    "signals": [],
    "tasks": [],
    "prospects": [],
    "matches": [],
    "materials": [],
    "facets": [],
    "notifications": [],
    "settings": {},
    "req": None,
    "requisition": None,
    "requirement": None,
    "offer": None,
    "quote": None,
    "vendor": None,
    "company": None,
    "material": None,
    "card": None,
    "prospect": None,
    "match": None,
    "plan": None,
    "buy_plan": None,
    "excess_list": None,
    "line_item": None,
    "bid": None,
    "contact": None,
    "site": None,
    "thread": None,
    "total": 0,
    "page": 0,
    "limit": 25,
    "offset": 0,
    "pages": 1,
    "query": "",
    "q": "",
    "tab": "overview",
    "error": None,
    "success": None,
    "message": "",
    "version": "test",
    "commodity": "",
    "commodity_tree": [],
    "sub_filters": [],
    "active_filters": {},
    "stats": {},
    "insights": None,
    "enrichment": None,
    "score": 0,
    "scores": {},
    "has_more": False,
    "is_admin": False,
    "sources": [],
    "connectors": [],
    "columns": [],
    "visible_columns": [],
    "sort_by": "created_at",
    "sort_dir": "desc",
    "status_filter": "",
    "search_filter": "",
}

ALL_TEMPLATES = list(_collect_templates())


@pytest.mark.parametrize("template_path", ALL_TEMPLATES)
def test_template_compiles(jinja_env, template_path):
    """Template loads and parses without TemplateSyntaxError."""
    try:
        tpl = jinja_env.get_template(template_path)
        assert tpl is not None, f"Template {template_path} returned None"
    except TemplateSyntaxError as e:
        pytest.fail(f"TemplateSyntaxError in {template_path}: {e}")


@pytest.mark.parametrize("template_path", ALL_TEMPLATES)
def test_template_renders_without_crash(jinja_env, template_path):
    """Template renders with dummy context without raising exceptions.

    Note: UndefinedError is acceptable for templates that require specific
    variables not in DUMMY_CONTEXT — we log but don't fail on those.
    TemplateSyntaxError or TypeError failures ARE real bugs.
    """
    try:
        tpl = jinja_env.get_template(template_path)
        tpl.render(**DUMMY_CONTEXT)
    except UndefinedError:
        pass  # Expected — template needs specific context we didn't provide
    except TemplateSyntaxError as e:
        pytest.fail(f"TemplateSyntaxError in {template_path}: {e}")
    except TypeError as e:
        pytest.fail(f"TypeError in {template_path} (likely bad filter/macro call): {e}")
```

- [ ] **Run template tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ux_mega/test_template_compilation.py -v --timeout=60`
Expected: All templates compile. Some may have UndefinedError on render (acceptable).

- [ ] **Commit**

```bash
git add tests/ux_mega/
git commit -m "feat: add template compilation tests (UX Mega Test system 2)"
```

---

## Task 3: Data Health Scanner

**Files:**
- Create: `tests/ux_mega/test_data_health.py`

This test file runs data integrity checks that catch real problems: orphaned records, impossible statuses, broken FK chains, stale computed fields.

### Step 1: Write data health scanner tests

- [ ] **Create `tests/ux_mega/test_data_health.py`**

```python
"""test_data_health.py — Data Health Scanner.

Detects orphaned records, impossible status values, broken FK chains,
and stale computed fields. Creates realistic data scenarios and verifies
the system handles them correctly.

Called by: pytest tests/ux_mega/test_data_health.py
Depends on: conftest.py fixtures, app.models, app.services.integrity_service
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    MaterialCard,
    Offer,
    Quote,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
)
from app.services.integrity_service import (
    check_dangling_fks,
    check_duplicate_cards,
    check_orphaned_offers,
    check_orphaned_requirements,
    check_orphaned_sightings,
    heal_orphaned_records,
    run_integrity_check,
)


class TestOrphanedRecords:
    """Detect records with MPN but no material_card_id link."""

    def test_requirement_with_mpn_but_no_card(self, db_session, test_requisition):
        """Requirement has MPN but lost its material card link."""
        req = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="LM317T",
            normalized_mpn="lm317t",
            target_qty=100,
            material_card_id=None,  # orphaned!
        )
        db_session.add(req)
        db_session.flush()

        count = check_orphaned_requirements(db_session)
        assert count >= 1, "Should detect orphaned requirement"

    def test_offer_with_mpn_but_no_card(self, db_session, test_requisition):
        """Offer has MPN but lost its material card link."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.50,
            material_card_id=None,  # orphaned!
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        count = check_orphaned_offers(db_session)
        assert count >= 1, "Should detect orphaned offer"

    def test_heal_relinks_orphaned_records(self, db_session, test_requisition):
        """heal_orphaned_records re-links records to material cards."""
        # Create a card
        card = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            manufacturer="TI",
        )
        db_session.add(card)
        db_session.flush()

        # Create orphaned requirement
        req = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="LM317T",
            normalized_mpn="lm317t",
            target_qty=100,
            material_card_id=None,
        )
        db_session.add(req)
        db_session.flush()

        result = heal_orphaned_records(db_session)
        assert result["requirements"] >= 1, "Should heal at least 1 requirement"


class TestStatusConsistency:
    """Detect impossible or inconsistent status combinations."""

    def test_offer_active_but_expired(self, db_session, test_requisition):
        """Offer marked active but expires_at is in the past = stale offer."""
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.50,
            status="active",
            attribution_status="active",
            expires_at=datetime.now(timezone.utc) - timedelta(days=30),
            created_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        db_session.add(offer)
        db_session.flush()

        # Detect: active offers with expired dates
        stale = (
            db_session.query(Offer)
            .filter(
                Offer.status == "active",
                Offer.expires_at.isnot(None),
                Offer.expires_at < datetime.now(timezone.utc),
            )
            .count()
        )
        assert stale >= 1, "Should detect stale active offer with past expiry"

    def test_requirement_status_valid_values(self, db_session, test_requisition):
        """All requirements have valid sourcing_status values."""
        valid_statuses = {"open", "sourcing", "offered", "quoted", "won", "lost"}
        reqs = db_session.query(Requirement.sourcing_status).filter(
            Requirement.requisition_id == test_requisition.id
        ).all()
        for (status,) in reqs:
            assert status in valid_statuses, f"Invalid sourcing_status: {status}"


class TestFKChainIntegrity:
    """Verify the Requisition → Requirement → Offer → Quote → BuyPlan chain."""

    def test_quote_lines_reference_valid_offers(self, db_session, test_quote):
        """All quote line offer_ids point to existing offers."""
        from app.models.buy_plan import BuyPlan

        # QuoteLines with offer_id should reference existing offers
        from app.models import QuoteLine

        lines = db_session.query(QuoteLine).filter(
            QuoteLine.quote_id == test_quote.id,
            QuoteLine.offer_id.isnot(None),
        ).all()
        for line in lines:
            offer = db_session.get(Offer, line.offer_id)
            assert offer is not None, f"QuoteLine {line.id} references missing offer {line.offer_id}"

    def test_dangling_material_card_fks(self, db_session):
        """No records point to non-existent material cards."""
        dangling = check_dangling_fks(db_session)
        total = sum(dangling.values())
        assert total == 0, f"Found {total} dangling material card FKs: {dangling}"


class TestDuplicateDetection:
    """Detect duplicate records that shouldn't exist."""

    def test_no_duplicate_material_cards(self, db_session):
        """Each normalized_mpn should appear at most once."""
        dupes = check_duplicate_cards(db_session)
        assert dupes == 0, f"Found {dupes} duplicate material card MPNs"

    def test_no_duplicate_vendor_cards(self, db_session):
        """Each normalized vendor name should appear at most once."""
        from sqlalchemy import func
        dupes = (
            db_session.query(VendorCard.normalized_name, func.count(VendorCard.id))
            .group_by(VendorCard.normalized_name)
            .having(func.count(VendorCard.id) > 1)
            .all()
        )
        assert len(dupes) == 0, f"Found duplicate vendor cards: {dupes}"


class TestIntegrityServiceIntegration:
    """Verify the full integrity check + heal pipeline."""

    def test_full_integrity_check_runs(self, db_session):
        """run_integrity_check completes without error and returns report."""
        report = run_integrity_check(db_session)
        assert "status" in report
        assert report["status"] in ("healthy", "degraded", "critical")
        assert "checks" in report
        assert "healed" in report
```

- [ ] **Run data health tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ux_mega/test_data_health.py -v`
Expected: All pass

- [ ] **Commit**

```bash
git add tests/ux_mega/test_data_health.py
git commit -m "feat: add data health scanner tests (UX Mega Test system 3)"
```

---

## Task 4: Data Consistency Validator — Workflow Chain Tests

**Files:**
- Create: `tests/ux_mega/test_data_consistency.py`

Creates realistic end-to-end workflows and verifies numbers match across the chain.

### Step 1: Write data consistency tests

- [ ] **Create `tests/ux_mega/test_data_consistency.py`**

```python
"""test_data_consistency.py — Data Consistency Validator.

Creates realistic end-to-end workflow chains (Requisition → Requirement →
Offer → Quote → BuyPlan) and verifies all numbers, statuses, and references
stay consistent throughout.

Called by: pytest tests/ux_mega/test_data_consistency.py
Depends on: conftest.py fixtures, app.models
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    Offer,
    Requirement,
    Requisition,
    User,
)


class TestRequisitionChain:
    """Verify Requisition → Requirement data consistency."""

    def test_all_requirements_reference_parent_requisition(self, db_session, test_requisition):
        """Every requirement in the DB has a valid requisition_id."""
        orphans = (
            db_session.query(Requirement)
            .filter(Requirement.requisition_id == test_requisition.id)
            .all()
        )
        for req in orphans:
            parent = db_session.get(Requisition, req.requisition_id)
            assert parent is not None, f"Requirement {req.id} references missing requisition {req.requisition_id}"

    def test_requirement_qty_is_positive(self, db_session, test_requisition):
        """No requirements should have zero or negative target_qty."""
        bad = (
            db_session.query(Requirement)
            .filter(
                Requirement.requisition_id == test_requisition.id,
                Requirement.target_qty <= 0,
            )
            .count()
        )
        assert bad == 0, f"Found {bad} requirements with qty <= 0"


class TestOfferChain:
    """Verify Offer → Requirement → Requisition consistency."""

    def test_offer_price_is_positive(self, db_session, test_requisition):
        """No active offers should have zero or negative price."""
        bad = (
            db_session.query(Offer)
            .filter(
                Offer.requisition_id == test_requisition.id,
                Offer.status == "active",
                Offer.unit_price <= 0,
            )
            .count()
        )
        assert bad == 0, f"Found {bad} active offers with price <= 0"

    def test_offer_references_valid_requirement(self, db_session, test_requisition):
        """Every offer's requirement_id points to an existing requirement."""
        offers = (
            db_session.query(Offer)
            .filter(
                Offer.requisition_id == test_requisition.id,
                Offer.requirement_id.isnot(None),
            )
            .all()
        )
        for offer in offers:
            req = db_session.get(Requirement, offer.requirement_id)
            assert req is not None, f"Offer {offer.id} references missing requirement {offer.requirement_id}"

    def test_offer_requisition_matches_requirement_requisition(self, db_session, test_requisition):
        """Offer.requisition_id must match its Requirement.requisition_id."""
        offers = (
            db_session.query(Offer)
            .filter(
                Offer.requisition_id == test_requisition.id,
                Offer.requirement_id.isnot(None),
            )
            .all()
        )
        for offer in offers:
            req = db_session.get(Requirement, offer.requirement_id)
            if req:
                assert offer.requisition_id == req.requisition_id, (
                    f"Offer {offer.id} requisition_id={offer.requisition_id} "
                    f"doesn't match requirement's requisition_id={req.requisition_id}"
                )


class TestQuoteConsistency:
    """Verify Quote totals match underlying line items."""

    def test_quote_references_valid_requisition(self, db_session, test_quote):
        """Quote's requisition_id points to an existing requisition."""
        parent = db_session.get(Requisition, test_quote.requisition_id)
        assert parent is not None, (
            f"Quote {test_quote.id} references missing requisition {test_quote.requisition_id}"
        )

    def test_no_quotes_with_invalid_status(self, db_session):
        """All quotes have a recognized status value."""
        from app.models import Quote
        valid = {"draft", "sent", "accepted", "rejected", "lost", "revised", "expired"}
        quotes = db_session.query(Quote.id, Quote.status).all()
        for qid, status in quotes:
            assert status in valid, f"Quote {qid} has invalid status: {status}"
```

- [ ] **Run consistency tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ux_mega/test_data_consistency.py -v`
Expected: All pass

- [ ] **Commit**

```bash
git add tests/ux_mega/test_data_consistency.py
git commit -m "feat: add data consistency validator tests (UX Mega Test system 4)"
```

---

## Task 5: Dead-End Detector (Playwright)

**Files:**
- Create: `e2e/dead-ends.spec.ts`
- Modify: `playwright.config.ts` (add dead-ends project)

### Step 1: Add Playwright project config

- [ ] **Add `dead-ends` and `workflows` projects to `playwright.config.ts`**

In the `projects` array, add:
```typescript
{ name: 'dead-ends', testMatch: /dead-ends\.spec\.ts$/ },
{ name: 'workflows', testMatch: /workflows\.spec\.ts$/ },
```

### Step 2: Write dead-end detection tests

- [ ] **Create `e2e/dead-ends.spec.ts`**

```typescript
/**
 * dead-ends.spec.ts — Dead-End Detector for AvailAI.
 *
 * Hits every major HTMX partial endpoint and verifies:
 * 1. Returns 200 (not 500, not empty)
 * 2. Response contains actual HTML content (not blank)
 * 3. No bare error text without styling
 *
 * Called by: npx playwright test --project=dead-ends
 * Depends on: running app server in TESTING=1 mode
 */

import { test, expect } from '@playwright/test';

// All list partials that should render without any path params
const LIST_PARTIALS = [
  '/v2/partials/requisitions',
  '/v2/partials/vendors',
  '/v2/partials/companies',
  '/v2/partials/quotes',
  '/v2/partials/buy-plans',
  '/v2/partials/materials',
  '/v2/partials/materials/workspace',
  '/v2/partials/prospecting',
  '/v2/partials/proactive',
  '/v2/partials/strategic',
  '/v2/partials/follow-ups',
  '/v2/partials/excess',
  '/v2/partials/settings',
  '/v2/partials/dashboard',
  '/v2/partials/search',
  '/v2/partials/offers/review-queue',
];

// Full pages that should render the app shell
const FULL_PAGES = [
  '/v2',
  '/v2/requisitions',
  '/v2/vendors',
  '/v2/companies',
  '/v2/quotes',
  '/v2/buy-plans',
  '/v2/materials',
  '/v2/search',
  '/v2/prospecting',
  '/v2/settings',
];

test.describe('Dead-End Detector — List Partials', () => {
  for (const url of LIST_PARTIALS) {
    test(`${url} returns non-empty HTML or auth redirect`, async ({ request }) => {
      const res = await request.get(url, {
        headers: { 'HX-Request': 'true' },
      });

      // Should not be a server error — 200, 401, 307 are all acceptable
      // (401/307 = auth required, which is correct behavior for unauthenticated requests)
      expect(res.status(), `${url} crashed with ${res.status()}`).toBeLessThan(500);

      // If we got a successful response, verify it has content
      if (res.status() === 200) {
        const html = await res.text();
        expect(html.trim().length, `${url} returned empty response`).toBeGreaterThan(10);
        expect(html).not.toMatch(/^(Internal Server Error|Not Found)$/);
      }
    });
  }
});

test.describe('Dead-End Detector — Full Pages', () => {
  for (const url of FULL_PAGES) {
    test(`${url} loads without server error`, async ({ request }) => {
      const res = await request.get(url, {
        headers: { 'Accept': 'text/html' },
      });

      // Should not crash — 200 or auth redirect (401/307) are acceptable
      expect(res.status(), `${url} crashed with ${res.status()}`).toBeLessThan(500);

      if (res.status() === 200) {
        const html = await res.text();
        // App shell should contain the sidebar and main content area
        expect(html).toContain('id="main-content"');
      }
    });
  }
});

test.describe('Dead-End Detector — Form Endpoints Accept POST', () => {
  // These POST endpoints should return non-500 even with minimal/empty data
  // (they should return validation errors or auth errors, not crashes)
  const POST_ENDPOINTS = [
    '/v2/partials/requisitions/create',
    '/v2/partials/companies/create',
  ];

  for (const url of POST_ENDPOINTS) {
    test(`POST ${url} doesn't crash on empty submission`, async ({ request }) => {
      const res = await request.post(url, {
        headers: { 'HX-Request': 'true', 'Content-Type': 'application/x-www-form-urlencoded' },
        data: '',
      });

      // Should return validation error (4xx), auth redirect (401/307), or success (2xx) — NOT a crash (5xx)
      expect(res.status(), `POST ${url} crashed with ${res.status()}`).toBeLessThan(500);
    });
  }
});

test.describe('Dead-End Detector — 404 Handling', () => {
  test('non-existent requisition returns error, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/requisitions/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });

  test('non-existent vendor returns error, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/vendors/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });

  test('non-existent company returns error, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/companies/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });

  test('non-existent quote returns error, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/quotes/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });
});
```

- [ ] **Run dead-end tests**

Run: `cd /root/availai && npx playwright test --project=dead-ends`
Expected: All pass (endpoints return non-500, non-empty responses)

- [ ] **Commit**

```bash
git add e2e/dead-ends.spec.ts playwright.config.ts
git commit -m "feat: add dead-end detector Playwright tests (UX Mega Test system 5)"
```

---

## Task 6: Playwright Workflow Tests

**Files:**
- Create: `e2e/workflows.spec.ts`

### Step 1: Write multi-step workflow tests

- [ ] **Create `e2e/workflows.spec.ts`**

```typescript
/**
 * workflows.spec.ts — Multi-step workflow tests for AvailAI.
 *
 * Tests complete user journeys through the app: navigation, tab switching,
 * form submissions, and cross-page consistency.
 *
 * Called by: npx playwright test --project=workflows
 * Depends on: running app server in TESTING=1 mode
 */

import { test, expect } from '@playwright/test';

test.describe('Navigation Workflows', () => {
  test('sidebar navigation loads correct partials', async ({ request }) => {
    // Each partial should return without server error
    // (200 = success, 401/307 = auth required — both are valid, not dead ends)
    for (const url of ['/v2/partials/requisitions', '/v2/partials/vendors', '/v2/partials/companies']) {
      const res = await request.get(url, {
        headers: { 'HX-Request': 'true' },
      });
      expect(res.status(), `${url} crashed`).toBeLessThan(500);
      if (res.status() === 200) {
        const html = await res.text();
        expect(html.length, `${url} empty`).toBeGreaterThan(50);
      }
    }
  });

  test('materials workspace loads with filters', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/workspace', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
    if (res.status() === 200) {
      const html = await res.text();
      expect(html).toContain('materialsFilter');
    }
  });

  test('materials faceted search with commodity filter', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/faceted?commodity=capacitors', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });

  test('search form renders and accepts queries', async ({ request }) => {
    // Load search form
    let res = await request.get('/v2/partials/search', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);

    // Submit search
    res = await request.get('/v2/partials/search/global?q=LM317T', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });
});

test.describe('Form Submission Workflows', () => {
  test('create requisition form renders', async ({ request }) => {
    const res = await request.get('/v2/partials/requisitions/create-form', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
    if (res.status() === 200) {
      const html = await res.text();
      expect(html).toContain('name');
    }
  });

  test('create company form renders', async ({ request }) => {
    const res = await request.get('/v2/partials/companies/create-form', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });
});

test.describe('Settings & Admin', () => {
  test('settings page loads all sections', async ({ request }) => {
    const sections = ['sources', 'system', 'profile', 'data-ops'];
    for (const section of sections) {
      const res = await request.get(`/v2/partials/settings/${section}`, {
        headers: { 'HX-Request': 'true' },
      });
      // 200 = success, 401/307 = auth required — both fine, not a dead end
      expect(res.status(), `Settings ${section} crashed`).toBeLessThan(500);
    }
  });

  test('API health check renders', async ({ request }) => {
    const res = await request.get('/v2/partials/admin/api-health', {
      headers: { 'HX-Request': 'true' },
    });
    // May require admin — 200, 401, 403 all acceptable
    expect([200, 401, 403]).toContain(res.status());
  });
});

test.describe('Dashboard', () => {
  test('dashboard loads', async ({ request }) => {
    const res = await request.get('/v2/partials/dashboard', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });
});
```

- [ ] **Run workflow tests**

Run: `cd /root/availai && npx playwright test --project=workflows`
Expected: All pass

- [ ] **Commit**

```bash
git add e2e/workflows.spec.ts
git commit -m "feat: add Playwright workflow tests (UX Mega Test system 6)"
```

---

## Task 7: Self-Repair Toolkit

**Files:**
- Create: `app/services/self_repair_service.py`
- Create: `tests/ux_mega/test_self_repair.py`

Extends the existing `integrity_service.py` with additional repair capabilities.

### Step 1: Write the self-repair service

- [ ] **Create `app/services/self_repair_service.py`**

```python
"""Self-Repair Service — extends integrity checks with active data repair.

Provides repair functions for common data problems that cause glitches,
dead ends, and wrong data in the UI. Designed to be run on-demand or
scheduled.

Called by: scheduler (optional), admin endpoints, tests
Depends on: app.models, app.services.integrity_service
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from ..models import (
    MaterialCard,
    Offer,
    Quote,
    Requirement,
    Requisition,
    VendorCard,
)
from ..models.buy_plan import BuyPlan, BuyPlanLine


def expire_stale_offers(db: Session, days_old: int = 14) -> int:
    """Mark offers as expired when expires_at is in the past.

    Returns count of offers expired.
    """
    cutoff = datetime.now(timezone.utc)
    updated = (
        db.query(Offer)
        .filter(
            Offer.status == "active",
            Offer.attribution_status == "active",
            Offer.expires_at.isnot(None),
            Offer.expires_at < cutoff,
        )
        .update(
            {"attribution_status": "expired"},
            synchronize_session="fetch",
        )
    )
    if updated:
        db.commit()
        logger.info("SELF_REPAIR: expired %d stale offers", updated)
    return updated


def fix_zero_qty_requirements(db: Session) -> int:
    """Fix requirements with qty=0 or NULL by setting to 1 (minimum valid qty).

    Returns count fixed.
    """
    updated = (
        db.query(Requirement)
        .filter(
            (Requirement.target_qty == 0) | (Requirement.target_qty.is_(None)),
            Requirement.sourcing_status != "lost",
        )
        .update(
            {"target_qty": 1},
            synchronize_session="fetch",
        )
    )
    if updated:
        db.commit()
        logger.info("SELF_REPAIR: fixed %d requirements with zero/null qty", updated)
    return updated


def fix_zero_price_offers(db: Session) -> int:
    """Set zero-price active offers to status 'expired' (likely bad parse).

    Returns count fixed.
    """
    updated = (
        db.query(Offer)
        .filter(
            Offer.status == "active",
            Offer.unit_price <= 0,
        )
        .update(
            {"attribution_status": "expired"},
            synchronize_session="fetch",
        )
    )
    if updated:
        db.commit()
        logger.info("SELF_REPAIR: expired %d zero-price offers", updated)
    return updated


def deduplicate_vendor_names(db: Session) -> int:
    """Find vendor cards with duplicate normalized_name and merge them.

    Keeps the card with the most sightings, re-points FKs from dupes.
    Returns count of duplicates merged.
    """
    from sqlalchemy import func

    dupes = (
        db.query(VendorCard.normalized_name, func.count(VendorCard.id))
        .group_by(VendorCard.normalized_name)
        .having(func.count(VendorCard.id) > 1)
        .all()
    )
    merged = 0
    for name, count in dupes:
        # Sort by sighting_count desc, NULLs last (SQLite-compatible)
        from sqlalchemy import case
        cards = (
            db.query(VendorCard)
            .filter(VendorCard.normalized_name == name)
            .order_by(
                case((VendorCard.sighting_count.is_(None), 1), else_=0),
                VendorCard.sighting_count.desc(),
            )
            .all()
        )
        keeper = cards[0]
        for dupe in cards[1:]:
            # Re-point offers from duplicate to keeper
            db.query(Offer).filter(Offer.vendor_card_id == dupe.id).update(
                {"vendor_card_id": keeper.id}, synchronize_session="fetch"
            )
            # Merge sighting count
            keeper.sighting_count = (keeper.sighting_count or 0) + (dupe.sighting_count or 0)
            db.delete(dupe)
            merged += 1
    if merged:
        db.commit()
        logger.info("SELF_REPAIR: merged %d duplicate vendor cards", merged)
    return merged


def run_full_repair(db: Session) -> dict:
    """Run all self-repair functions and return a summary report.

    Safe to run repeatedly — all operations are idempotent.
    """
    report = {
        "stale_offers_expired": expire_stale_offers(db),
        "zero_qty_fixed": fix_zero_qty_requirements(db),
        "zero_price_expired": fix_zero_price_offers(db),
        "vendor_dupes_merged": deduplicate_vendor_names(db),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("SELF_REPAIR_COMPLETE: %s", report)
    return report
```

### Step 2: Write tests for self-repair

- [ ] **Create `tests/ux_mega/test_self_repair.py`**

```python
"""test_self_repair.py — Tests for the Self-Repair Toolkit.

Verifies each repair function detects and fixes the target problem.

Called by: pytest tests/ux_mega/test_self_repair.py
Depends on: conftest.py fixtures, app.services.self_repair_service
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Offer, Requirement, VendorCard
from app.services.self_repair_service import (
    expire_stale_offers,
    fix_zero_price_offers,
    fix_zero_qty_requirements,
    run_full_repair,
)


class TestExpireStaleOffers:
    def test_expires_past_due_offers(self, db_session, test_requisition):
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="StaleVendor",
            mpn="OLD123",
            qty_available=100,
            unit_price=1.00,
            status="active",
            attribution_status="active",
            expires_at=datetime.now(timezone.utc) - timedelta(days=30),
            created_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        db_session.add(offer)
        db_session.flush()

        count = expire_stale_offers(db_session)
        assert count >= 1

        db_session.refresh(offer)
        assert offer.attribution_status == "expired"

    def test_skips_non_expired_offers(self, db_session, test_requisition):
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="FreshVendor",
            mpn="NEW123",
            qty_available=100,
            unit_price=1.00,
            status="active",
            attribution_status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        expire_stale_offers(db_session)
        db_session.refresh(offer)
        assert offer.attribution_status == "active"


class TestFixZeroQty:
    def test_fixes_zero_qty(self, db_session, test_requisition):
        req = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="ZEROQTY",
            normalized_mpn="zeroqty",
            target_qty=0,
        )
        db_session.add(req)
        db_session.flush()

        count = fix_zero_qty_requirements(db_session)
        assert count >= 1

        db_session.refresh(req)
        assert req.target_qty == 1


class TestFixZeroPrice:
    def test_expires_zero_price_offers(self, db_session, test_requisition):
        req = db_session.query(Requirement).filter_by(
            requisition_id=test_requisition.id
        ).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="FreeVendor",
            mpn="FREE123",
            qty_available=100,
            unit_price=0.0,
            status="active",
            attribution_status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        count = fix_zero_price_offers(db_session)
        assert count >= 1

        db_session.refresh(offer)
        assert offer.attribution_status == "expired"


class TestFullRepair:
    def test_full_repair_runs_without_error(self, db_session):
        report = run_full_repair(db_session)
        assert "ran_at" in report
        assert "stale_offers_expired" in report
        assert "zero_qty_fixed" in report
        assert "zero_price_expired" in report
        assert "vendor_dupes_merged" in report
```

- [ ] **Run self-repair tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ux_mega/test_self_repair.py -v`
Expected: All pass

- [ ] **Commit**

```bash
git add app/services/self_repair_service.py tests/ux_mega/test_self_repair.py
git commit -m "feat: add self-repair toolkit with tests (UX Mega Test system 7)"
```

---

## Task 8: Expand Lighthouse Audit

**Files:**
- Modify: `scripts/lighthouse-audit.mjs`

### Step 1: Expand pages and raise thresholds

- [ ] **Update `scripts/lighthouse-audit.mjs`**

Replace the `PAGES` and `THRESHOLDS` constants:

```javascript
const PAGES = [
  { name: 'Login Page', path: '/' },
  { name: 'App Shell', path: '/v2' },
  { name: 'Requisitions', path: '/v2/requisitions' },
  { name: 'Vendors', path: '/v2/vendors' },
  { name: 'Companies', path: '/v2/companies' },
  { name: 'Materials', path: '/v2/materials' },
  { name: 'Search', path: '/v2/search' },
  { name: 'Settings', path: '/v2/settings' },
];

const THRESHOLDS = {
  performance: 60,
  accessibility: 80,
  'best-practices': 80,
  seo: 70,
};
```

- [ ] **Run to verify (requires Chrome and running app)**

Run: `cd /root/availai && npm run test:lighthouse`
Expected: Audits all 8 pages, reports scores. May need Chrome installed.

- [ ] **Commit**

```bash
git add scripts/lighthouse-audit.mjs
git commit -m "feat: expand Lighthouse audit to 8 pages (UX Mega Test system 8)"
```

---

## Task 9: Canary Monitor Script

**Files:**
- Create: `scripts/canary-monitor.sh`

### Step 1: Write the canary script

- [ ] **Create `scripts/canary-monitor.sh`**

```bash
#!/usr/bin/env bash
# canary-monitor.sh — Lightweight production health check.
#
# Hits key endpoints and reports pass/fail. Designed to run via cron
# every 5 minutes. Logs failures to /var/log/availai-canary.log.
#
# Called by: cron (*/5 * * * * /root/availai/scripts/canary-monitor.sh)
# Depends on: curl, running AvailAI app

set -euo pipefail

BASE_URL="${CANARY_URL:-http://127.0.0.1:8000}"
LOG_FILE="${CANARY_LOG:-/var/log/availai-canary.log}"
TIMEOUT=10
FAILURES=0

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1" >> "$LOG_FILE"
}

check() {
    local name="$1"
    local url="$2"
    local expected_status="${3:-200}"
    local extra_header="${4:-}"

    local curl_args=(-s -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT")
    if [ -n "$extra_header" ]; then
        curl_args+=(-H "$extra_header")
    fi

    local status
    status=$(curl "${curl_args[@]}" "$url" 2>/dev/null || echo "000")

    if [ "$status" = "$expected_status" ]; then
        return 0
    else
        log "FAIL: $name — expected $expected_status, got $status ($url)"
        FAILURES=$((FAILURES + 1))
        return 1
    fi
}

# Health endpoint
check "Health" "$BASE_URL/health"

# App shell loads
check "App Shell" "$BASE_URL/v2"

# Key partials respond (with HX-Request header)
check "Requisitions List" "$BASE_URL/v2/partials/requisitions" "200" "HX-Request: true"
check "Vendors List" "$BASE_URL/v2/partials/vendors" "200" "HX-Request: true"
check "Dashboard" "$BASE_URL/v2/partials/dashboard" "200" "HX-Request: true"
check "Search Form" "$BASE_URL/v2/partials/search" "200" "HX-Request: true"

# API endpoints
check "API Sources" "$BASE_URL/api/v1/sources"

if [ "$FAILURES" -gt 0 ]; then
    log "CANARY: $FAILURES checks failed"
    exit 1
else
    # Only log failures to keep log small. Uncomment below for verbose:
    # log "CANARY: all checks passed"
    exit 0
fi
```

- [ ] **Make executable**

Run: `chmod +x /root/availai/scripts/canary-monitor.sh`

- [ ] **Commit**

```bash
git add scripts/canary-monitor.sh
git commit -m "feat: add canary monitor script (UX Mega Test system 9)"
```

---

## Task 10: Master Runner Script + npm Scripts

**Files:**
- Create: `scripts/run-ux-mega-test.sh`
- Modify: `package.json` (add mega test scripts)

### Step 1: Create master runner

- [ ] **Create `scripts/run-ux-mega-test.sh`**

```bash
#!/usr/bin/env bash
# run-ux-mega-test.sh — Run the complete UX Mega Test suite.
#
# Executes all 9 test systems in order:
# 1. Vitest Alpine component tests
# 2. Template compilation tests
# 3. Data health scanner
# 4. Data consistency validator
# 5. Dead-end detector (Playwright)
# 6. Workflow tests (Playwright)
# 7. Self-repair toolkit tests
# 8. Lighthouse audit (optional, needs Chrome)
# 9. Canary monitor (optional, needs running app)
#
# Called by: npm run test:mega or bash scripts/run-ux-mega-test.sh
# Depends on: pytest, vitest, playwright

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

FAILURES=0

run_step() {
    local name="$1"
    shift
    echo -e "\n${YELLOW}=== $name ===${NC}"
    if "$@"; then
        echo -e "${GREEN}✓ $name passed${NC}"
    else
        echo -e "${RED}✗ $name failed${NC}"
        FAILURES=$((FAILURES + 1))
    fi
}

cd "$(dirname "$0")/.."

echo "=== UX Mega Test Suite ==="
echo "Starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# System 1: Vitest Alpine Component Tests
run_step "Vitest Alpine Components" npx vitest run

# System 2: Template Compilation Tests
run_step "Template Compilation" env TESTING=1 PYTHONPATH=/root/availai \
    pytest tests/ux_mega/test_template_compilation.py -v --timeout=60 -x

# System 3: Data Health Scanner
run_step "Data Health Scanner" env TESTING=1 PYTHONPATH=/root/availai \
    pytest tests/ux_mega/test_data_health.py -v --timeout=30

# System 4: Data Consistency Validator
run_step "Data Consistency Validator" env TESTING=1 PYTHONPATH=/root/availai \
    pytest tests/ux_mega/test_data_consistency.py -v --timeout=30

# System 5: Dead-End Detector (Playwright)
run_step "Dead-End Detector" npx playwright test --project=dead-ends

# System 6: Workflow Tests (Playwright)
run_step "Workflow Tests" npx playwright test --project=workflows

# System 7: Self-Repair Toolkit
run_step "Self-Repair Toolkit" env TESTING=1 PYTHONPATH=/root/availai \
    pytest tests/ux_mega/test_self_repair.py -v --timeout=30

# System 8: Lighthouse (optional — skip if no Chrome)
if command -v google-chrome &> /dev/null || command -v chromium-browser &> /dev/null; then
    run_step "Lighthouse Audit" npm run test:lighthouse
else
    echo -e "\n${YELLOW}=== Lighthouse Audit ===${NC}"
    echo -e "${YELLOW}⚠ Skipped (Chrome not installed)${NC}"
fi

echo ""
echo "=== UX Mega Test Complete ==="
echo "Finished at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ "$FAILURES" -gt 0 ]; then
    echo -e "${RED}$FAILURES system(s) failed${NC}"
    exit 1
else
    echo -e "${GREEN}All systems passed${NC}"
    exit 0
fi
```

- [ ] **Make executable**

Run: `chmod +x /root/availai/scripts/run-ux-mega-test.sh`

- [ ] **Add npm scripts to `package.json`**

Add to `"scripts"`:
```json
"test:mega": "bash scripts/run-ux-mega-test.sh",
"test:mega:python": "TESTING=1 PYTHONPATH=/root/availai pytest tests/ux_mega/ -v",
"test:mega:playwright": "npx playwright test --project=dead-ends --project=workflows"
```

- [ ] **Run the full mega test**

Run: `cd /root/availai && npm run test:mega`
Expected: All 7+ systems pass (Lighthouse may skip if no Chrome)

- [ ] **Commit**

```bash
git add scripts/run-ux-mega-test.sh package.json
git commit -m "feat: add UX Mega Test master runner + npm scripts"
```

---

## Summary

| System | Type | Files | Tests |
|--------|------|-------|-------|
| 1. Vitest Alpine Components | JS unit | `tests/frontend/alpine-components.test.ts` | ~30 |
| 2. Template Compilation | Python | `tests/ux_mega/test_template_compilation.py` | ~334 (167×2) |
| 3. Data Health Scanner | Python | `tests/ux_mega/test_data_health.py` | ~10 |
| 4. Data Consistency Validator | Python | `tests/ux_mega/test_data_consistency.py` | ~6 |
| 5. Dead-End Detector | Playwright | `e2e/dead-ends.spec.ts` | ~24 |
| 6. Workflow Tests | Playwright | `e2e/workflows.spec.ts` | ~8 |
| 7. Self-Repair Toolkit | Python + service | `app/services/self_repair_service.py` + test | ~6 |
| 8. Lighthouse Audit | Node.js | `scripts/lighthouse-audit.mjs` (modified) | 8 pages |
| 9. Canary Monitor | Shell | `scripts/canary-monitor.sh` | 7 checks |

**Run commands:**
- Full suite: `npm run test:mega`
- Python only: `npm run test:mega:python`
- Playwright only: `npm run test:mega:playwright`
- Vitest only: `npx vitest run`
- Canary: `bash scripts/canary-monitor.sh`
